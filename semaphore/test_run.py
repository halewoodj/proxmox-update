"""Safety checks run with fake SSH and Bash; no network or package operations."""
import json
import fcntl
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
import unittest
from test_update_guard import healthy

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'semaphore/run.py'

FAKE_SSH = '''#!/usr/bin/env python3
import os,sys,json
args=sys.argv[1:]
dest=next(a for a in args if a.startswith('root@'))
command=args[-1]
node=dest.split('@')[1]
if command=='hostname -s': print(node)
elif command.startswith('python3 -c '):
 data=json.loads(os.environ['TEST_CLUSTER_JSON'])[node]
 if os.environ.get('TEST_EXTRA_MEMBER'):data['members']+='\\n3 1 NODE-3'
 if os.environ.get('TEST_QUORUM'):data['quorum']='Quorate: '+os.environ['TEST_QUORUM']
 if os.environ.get('TEST_HEALTH'):
  data['ceph']['health'].update(status=os.environ['TEST_HEALTH'].split()[0],checks={'TEST_WARNING':{}})
 print(json.dumps(data))
elif command=='pvecm nodes':
 print('1 1 NODE-1 (local)\\n2 1 NODE-2')
 if os.environ.get('TEST_EXTRA_MEMBER'): print('3 1 NODE-3')
elif command=='pvecm status': print('Quorate: '+os.environ.get('TEST_QUORUM','Yes'))
elif command=='ceph health': print(os.environ.get('TEST_HEALTH','HEALTH_OK'))
else: sys.exit(90)
'''
FAKE_BASH = '''#!/usr/bin/env python3
import os,sys,json,signal,time,subprocess,pathlib
assert 'PVE_SSH_PRIVATE_KEY' not in os.environ
if os.environ.get('TEST_WAIT'):
 pathlib.Path(os.environ['TEST_PID']).write_text(str(os.getpid()))
 child_code="import signal,time,pathlib,sys; signal.signal(signal.SIGTERM,lambda s,f:(pathlib.Path(sys.argv[1]).write_text('stopped'),sys.exit(0))); pathlib.Path(sys.argv[2]).write_text('ready'); time.sleep(30)"
 child=subprocess.Popen([sys.executable,'-c',child_code,os.environ['TEST_CHILD_STOPPED'],os.environ['TEST_CHILD_READY']])
 while not pathlib.Path(os.environ['TEST_CHILD_READY']).exists():time.sleep(.02)
 def stopped(s,f):
  if not pathlib.Path(os.environ['TEST_CHILD_STOPPED']).exists():child.terminate()
  child.wait(timeout=5)
  pathlib.Path(os.environ['TEST_STOPPED']).write_text(str(pathlib.Path(os.environ['PATH'].split(os.pathsep)[0]).parent.joinpath('id_ed25519').exists()))
  sys.exit(130)
 signal.signal(signal.SIGTERM,stopped)
with open(os.environ['TEST_INVOCATION'],'w') as f: json.dump(sys.argv[1:],f)
if os.environ.get('TEST_WAIT'):time.sleep(30)
sys.exit(int(os.environ.get('TEST_SCRIPT_RC','0')))
'''

# Keep tests independent of the production maintenance lock on a live runner.
DRIVER = '''import builtins,os,runpy,sys
real_open=builtins.open
def test_open(path,*args,**kwargs):
 if path=='/tmp/proxmox-cluster-maintenance.lock':path=os.environ['TEST_LOCK_PATH']
 return real_open(path,*args,**kwargs)
builtins.open=test_open
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
'''


class RunnerTests(unittest.TestCase):
    def run_case(self, *args, **extra):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name, source in [('ssh', FAKE_SSH), ('bash', FAKE_BASH)]:
                p = tmp / name
                p.write_text(source.replace('#!/usr/bin/env python3', '#!' + sys.executable))
                p.chmod(0o700)
            invocation = tmp / 'invocation.json'
            env = os.environ.copy()
            env.update(PATH=str(tmp) + os.pathsep + env.get('PATH', ''),
                       PVE_NODES_JSON=json.dumps({'NODE-1':'192.0.2.1','NODE-2':'192.0.2.2'}),
                       PVE_CLUSTER_HOST='NODE-1', PVE_KNOWN_HOSTS='NODE-1 ssh-ed25519 fake',
                       PVE_SSH_PRIVATE_KEY='SECRET-MARKER', TEST_INVOCATION=str(invocation),
                       TEST_CLUSTER_JSON=json.dumps(healthy()),
                       TEST_LOCK_PATH=str(tmp/'maintenance.lock'), **extra)
            command=[sys.executable, '-c', DRIVER, str(RUNNER), *args]
            if extra.get('TEST_WAIT'):
                env.update(TEST_CHILD_STOPPED=str(tmp/'child-stopped'),TEST_CHILD_READY=str(tmp/'child-ready'),TEST_STOPPED=str(tmp/'stopped'),TEST_PID=str(tmp/'pid'))
                process=subprocess.Popen(command,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    deadline=time.monotonic()+10
                    while not invocation.exists() and time.monotonic()<deadline:time.sleep(.02)
                    self.assertTrue(invocation.exists(),'Runner did not start its child')
                    if extra.get('TEST_FORCE'):
                        process.kill();process.wait(timeout=5)
                        if not sys.platform.startswith('linux'):
                            with (tmp/'maintenance.lock').open('a') as lock:
                                with self.assertRaises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            os.killpg(int((tmp/'pid').read_text()),signal.SIGTERM)
                    else:
                        process.send_signal(signal.SIGTERM)
                    stdout,stderr=process.communicate(timeout=10)
                    result=subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
                    self.assertTrue((tmp/'child-stopped').exists(),'Cancellation did not reach the grandchild')
                    self.assertEqual((tmp/'stopped').read_text(),'True','SSH files removed before the child exited')
                finally:
                    if process.poll() is None:process.kill();process.wait()
                    if (tmp/'pid').exists():
                        try:os.killpg(int((tmp/'pid').read_text()),signal.SIGKILL)
                        except ProcessLookupError:pass
            else:
                result = subprocess.run(command,env=env,text=True,capture_output=True,timeout=10)
            self.assertNotIn('SECRET-MARKER',result.stdout + result.stderr)
            return result, json.loads(invocation.read_text()) if invocation.exists() else None

    def test_preview_is_dry_run_and_scoped(self):
        result, args = self.run_case('preview')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--dry-run', args)
        self.assertEqual(args[args.index('--nodes')+1], 'NODE-1,NODE-2')
        self.assertEqual(args[args.index('--cluster-host')+1], 'root@NODE-1')
        if (ROOT / 'update.sh').exists():
            self.assertEqual(args[args.index('--jobs')+1], '1')
            self.assertIn('--auto-maintenance',args)
        else:
            self.assertIn('--direct-node-ssh', args)
            self.assertIn('--yes', args)
            self.assertNotIn('--skip-ceph-check', args)

    def test_apply_has_explicit_mode(self):
        result, args = self.run_case('apply', '--nodes', 'NODE-2')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('--dry-run',args)
        self.assertEqual(args[args.index('--nodes')+1], 'NODE-2')

    def test_extra_cluster_member_blocks_maintenance(self):
        result, args = self.run_case('preview', TEST_EXTRA_MEMBER='1')
        self.assertNotEqual(result.returncode,0)
        self.assertIsNone(args)

    def test_no_quorum_blocks_maintenance(self):
        result, args = self.run_case('apply', TEST_QUORUM='No')
        self.assertNotEqual(result.returncode,0)
        self.assertIsNone(args)

    def test_ceph_warning_blocks_maintenance(self):
        result, args = self.run_case('apply', TEST_HEALTH='HEALTH_WARN testing')
        self.assertNotEqual(result.returncode,0)
        self.assertIsNone(args)

    def test_out_of_scope_selection_blocks_maintenance(self):
        result, args = self.run_case('apply','--nodes','NODE-3')
        self.assertNotEqual(result.returncode,0)
        self.assertIsNone(args)

    def test_script_failure_is_reported(self):
        result, args = self.run_case('preview',TEST_SCRIPT_RC='42')
        self.assertEqual(result.returncode,42)
        self.assertIsNotNone(args)

    def test_runner_cancellation_reaches_child_and_grandchild_before_cleanup(self):
        result,args=self.run_case('apply','--nodes','NODE-1',TEST_WAIT='1')
        self.assertEqual(result.returncode,130,result.stderr)
        self.assertIsNotNone(args)

    def test_killed_runner_signals_linux_worker_and_preserves_credentials_until_exit(self):
        result,args=self.run_case('apply','--nodes','NODE-1',TEST_WAIT='1',TEST_FORCE='1')
        self.assertEqual(result.returncode,-signal.SIGKILL,result.stderr)
        self.assertIsNotNone(args)


if __name__ == '__main__':
    unittest.main()
