"""Lease, ownership and termination-proof regressions; never invokes APT."""
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import remote_apt as remote

TOKEN = 'a'*32


class RemoteAptTests(unittest.TestCase):
    def lease_case(self, payload, *, close=False):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        if payload:os.write(write, payload)
        if close:os.close(write)
        else:self.addCleanup(os.close, write)
        return remote.Lease(read,TOKEN)

    def test_disconnect_revokes_even_a_previously_valid_lease(self):
        lease=self.lease_case(('H '+TOKEN+'\n').encode(),close=True)
        self.assertTrue(lease.poll(0))
        self.assertFalse(lease.poll(0))

    def test_missing_or_wrong_heartbeat_never_authorizes_start(self):
        for payload in [b'',b'H wrong\n',b'garbage\n']:
            with self.subTest(payload=payload[:30]):
                lease=self.lease_case(payload,close=True)
                while len(lease.buffer)<=8192:
                    if not lease.poll(0):break
                else:self.fail('Malformed lease accepted')
                self.assertFalse(lease.seen)

    @patch.object(remote.select,'select',return_value=([0],[],[]))
    @patch.object(remote.os,'read',side_effect=[b'x'*4096,b'x'*4096,b'x'])
    def test_oversized_unterminated_lease_is_rejected(self,read,select):
        lease=remote.Lease(0,TOKEN)
        self.assertTrue(lease.poll(0));self.assertTrue(lease.poll(0))
        self.assertFalse(lease.poll(0));self.assertFalse(lease.seen)

    def test_silent_open_connection_expires(self):
        read,write=os.pipe();self.addCleanup(os.close,read);self.addCleanup(os.close,write)
        now=[100.0];lease=remote.Lease(read,TOKEN,clock=lambda:now[0])
        os.write(write,('H '+TOKEN+'\n').encode());self.assertTrue(lease.poll(0))
        now[0]+=remote.LEASE_SECONDS+1
        self.assertFalse(lease.poll(0))

    def test_agent_does_not_start_command_after_buffered_heartbeat_then_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'must-not-start'
            code="import remote_apt as r,sys; r.own_group=lambda token:None;sys.exit(r.agent("+repr(TOKEN)+",30,"+repr('touch '+shlex.quote(str(marker)))+"))"
            result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).parent,
                input=('H '+TOKEN+'\n').encode(),capture_output=True,timeout=5)
            self.assertEqual(result.returncode,130,result.stderr)
            self.assertFalse(marker.exists())

    def test_service_owns_all_sessions_and_preserves_literal_command(self):
        command="printf '%s' '$(must_not_execute) $HOME \"x\"'"
        args=shlex.split(remote.service_command('pass',TOKEN,60,command))
        self.assertIn('--property=KillMode=control-group',args)
        self.assertIn('--property=KillSignal=SIGTERM',args)
        self.assertIn('--property=SendSIGKILL=no',args)
        self.assertIn('--property=TimeoutStopSec=infinity',args)
        self.assertIn('--expand-environment=no',args)
        self.assertEqual(base64.b64decode(args[-1]).decode(),command)
        self.assertIn('--pipe',args)
        self.assertIn('--wait',args)

    def test_invalid_unit_identity_cannot_address_other_services(self):
        for token in ['', '../pve-ha-crm', 'x'*32, 'a'*31+';']:
            with self.assertRaises(ValueError):remote.unit_name(token)

    def test_descendant_cgroups_are_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'child').mkdir()
            (root/'cgroup.procs').write_text('10\n20\n')
            (root/'child/cgroup.procs').write_text('30\n')
            self.assertEqual(remote.group_pids(root),{10,20,30})

    @patch.object(remote.subprocess,'run')
    def test_stop_refuses_unowned_unit(self,run):
        run.return_value=subprocess.CompletedProcess([],0,'LoadState=loaded\nDescription=Someone else\n','')
        with self.assertRaisesRegex(ValueError,'ownership'):remote.inspect_unit(TOKEN,stop=True)
        self.assertEqual(run.call_count,1)

    @patch.object(remote,'group_pids',return_value={77})
    @patch.object(remote.subprocess,'run')
    def test_not_found_with_leftover_cgroup_is_not_stopped(self,run,pids):
        run.return_value=subprocess.CompletedProcess([],0,'LoadState=not-found\n','')
        result=remote.inspect_unit(TOKEN)
        self.assertFalse(result['quiescent']);self.assertEqual(result['pids'],[77])

    @patch.object(remote.subprocess,'run')
    def test_invalid_quiescence_type_is_rejected(self,run):
        run.return_value=subprocess.CompletedProcess([],0,json.dumps({
            'unit':remote.unit_name(TOKEN),'pids':[],'quiescent':'false'}),'')
        with self.assertRaisesRegex(ValueError,'termination evidence'):
            remote.probe('NODE-1','source',TOKEN)

    @patch.object(remote,'probe')
    def test_cancellation_waits_for_escaped_descendant_and_transport_exit(self,probe):
        transport=Mock();transport.poll.side_effect=[None,130]
        probe.side_effect=[{'quiescent':False,'pids':[22]}, {'quiescent':True,'pids':[]}]
        result=remote.verify_end('NODE-1','source',TOKEN,transport,True,sleep=lambda _:None)
        self.assertTrue(result['quiescent'])
        self.assertEqual(probe.call_count,2)
        self.assertTrue(all(c.kwargs['stop'] for c in probe.call_args_list))

    @patch.object(remote,'probe')
    def test_not_found_during_start_is_not_termination_proof(self,probe):
        probe.return_value={'quiescent':True,'pids':[]}
        transport=Mock();transport.poll.return_value=None
        clock=iter([0,0,remote.VERIFY_SECONDS+1])
        result=remote.verify_end('NODE-1','source',TOKEN,transport,True,
            clock=lambda:next(clock),sleep=lambda _:None)
        self.assertFalse(result['quiescent'])

    @patch.object(remote,'probe')
    def test_live_process_or_unreachable_host_is_never_reported_stopped(self,probe):
        for response in [{'quiescent':True,'pids':[123]},ValueError('network unavailable')]:
            with self.subTest(response=response):
                if isinstance(response,Exception):probe.side_effect=response
                else:probe.side_effect=None;probe.return_value=response
                transport=Mock();transport.poll.return_value=130
                clock=iter([0,remote.VERIFY_SECONDS+1])
                result=remote.verify_end('NODE-1','source',TOKEN,transport,True,
                    clock=lambda:next(clock),sleep=lambda _:None)
                self.assertFalse(result['quiescent'])


if __name__=='__main__':unittest.main()
