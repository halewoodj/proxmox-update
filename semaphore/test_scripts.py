"""Exercise upstream script failure paths using fake SSH only."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from test_update_guard import healthy

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
VERSION = subprocess.check_output([BASH,'--version'],text=True)
BASH_MAJOR = int(re.search(r'version (\d+)',VERSION).group(1))

SSH = '''#!/usr/bin/env python3
import json,os,shlex,subprocess,sys,signal
node=next(a for a in sys.argv if a.startswith('root@')).split('@')[1]
command=sys.argv[-1]
tokens=shlex.split(command)
if 'bash' in tokens and '-lc' in tokens: command=tokens[-1]
with open(os.environ['TEST_COMMAND_LOG'],'a') as f: f.write(json.dumps(command)+'\\n')
with open(os.environ['TEST_NODE_LOG'],'a') as f: f.write(json.dumps({'node':node,'command':command})+'\\n')
if command.startswith('python3 -c '):
 data=json.load(open(os.environ['TEST_FIXTURE']))[node]
 counter=os.environ['TEST_FIXTURE']+'.'+node
 count=int(open(counter).read())+1 if os.path.exists(counter) else 1
 open(counter,'w').write(str(count))
 if os.environ.get('TEST_ARMED'): data['crm_active']='active'
 if os.environ.get('TEST_POST_REBOOT') and count>=3: data['boot_id']='ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee'
 if os.environ.get('TEST_POST_CEPH') and count>=3:data['ceph']['health']['status']='HEALTH_WARN'
 print(json.dumps(data))
elif command=='true' or command.startswith('command -v '):
 if command=='true' and os.environ.get('TEST_LOST_WORKER'):
  os.kill(os.getppid(),signal.SIGKILL)
elif 'pvecm nodes' in command: print('1 1 NODE-1 (local)\\n2 1 NODE-2')
elif command.startswith('hostname'): print('NODE-1')
elif command=='pvecm status': print('Quorate: Yes')
elif command=='ceph health': print('HEALTH_OK')
elif 'qm list' in command:
 sys.exit(subprocess.call([os.environ['TEST_BASH'],'-c',command]))
elif 'APT::Update::Error-Mode=any update' in command:
 if os.environ.get('TEST_APT_FAIL'):
  print('repository download diagnostic',file=sys.stderr);sys.exit(100)
elif 'apt-get -s dist-upgrade' in command:
 print('Inst example [1] (2 repo [amd64])')
 if os.environ.get('TEST_REMOVAL'):print('Remv proxmox-ve [9.2]')
elif '--no-remove dist-upgrade' in command:
 if os.environ.get('TEST_UPGRADE_FAIL'):
  print('dpkg exact failure diagnostic',file=sys.stderr);sys.exit(100)
elif command=='pveversion':
 if os.environ.get('TEST_BAD_VERSION'):sys.exit(255)
 print('pve-manager/test')
elif command=='uname -r': print('6.8.1-pve')
elif 'vmlinuz-' in command: print('6.8.2-pve')
elif command=='[ -f /var/run/reboot-required ]': sys.exit(1)
else:
 print('Forbidden fake SSH command: '+command,file=sys.stderr)
 sys.exit(91)
'''


class ScriptTests(unittest.TestCase):
    def run_script(self, filename, **extra):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp)
            path=tmp/'ssh'
            path.write_text(SSH.replace('#!/usr/bin/env python3','#!'+sys.executable))
            path.chmod(0o700)
            for command, kind in [('qm','qemu'),('pct','lxc')]:
                p=tmp/command
                p.write_text('#!/bin/sh\nif [ "$TEST_LIST_FAILURE" = "'+kind+'" ]; then exit 1; fi\nexit 0\n')
                p.chmod(0o700)
            log=tmp/'commands.jsonl'
            fixture=tmp/'fixture.json';fixture.write_text(json.dumps(healthy()))
            nodes=tmp/'nodes.jsonl'
            env=os.environ.copy()
            env.update(PATH=str(tmp)+os.pathsep+env['PATH'],TEST_COMMAND_LOG=str(log),
                       TEST_BASH=BASH,TERM='dumb',TEST_FIXTURE=str(fixture),TEST_NODE_LOG=str(nodes),**extra)
            args=[BASH,str(ROOT/filename),'--cluster-host','root@NODE-1',
                  '--nodes',extra.get('TEST_SELECTED','NODE-1,NODE-2')]
            if not extra.get('TEST_APPLY'):args.append('--dry-run')
            if extra.get('TEST_PARALLEL'):args.extend(['--jobs','2'])
            if filename=='rolling-reboot.sh': args.extend(['--yes','--direct-node-ssh','--ceph-timeout','1'])
            result=subprocess.run(args,env=env,capture_output=True,text=True,timeout=30)
            commands=[json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            self.node_commands=[json.loads(line) for line in nodes.read_text().splitlines()] if nodes.exists() else []
            # The script deliberately retains evidence. Test fixtures can be removed.
            match=re.search(r'Diagnostic logs and summaries: (.+)',result.stdout)
            if match:shutil.rmtree(match[1],ignore_errors=True)
            return result, commands

    @unittest.skipUnless((ROOT/'update.sh').exists() and BASH_MAJOR>=4,'requires updater and Bash 4+')
    def test_failed_apt_update_returns_failure(self):
        result,commands=self.run_script('update.sh',TEST_APT_FAIL='1')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertIn('apt-get update failed',result.stdout)
        self.assertFalse(any('dist-upgrade' in c for c in commands))
        self.assertIn('repository download diagnostic',result.stderr)
        self.assertFalse(any(x['node']=='NODE-2' and 'APT::Update::Error-Mode=any update' in x['command'] for x in self.node_commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_upgrade_failure_stops_before_second_node_and_prints_diagnostic(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_UPGRADE_FAIL='1')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertIn('dpkg exact failure diagnostic',result.stderr)
        self.assertIn('Skipped after an earlier node failed',result.stdout)
        self.assertFalse(any(x['node']=='NODE-2' and 'apt-get' in x['command'] for x in self.node_commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_armed_ha_blocks_package_changes(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_ARMED='1')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertFalse(any('--no-remove dist-upgrade' in c for c in commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_reboot_or_ceph_warning_after_success_stops_remaining_nodes(self):
        for scenario in ['TEST_POST_REBOOT','TEST_POST_CEPH']:
            with self.subTest(scenario=scenario):
                result,commands=self.run_script('update.sh',TEST_APPLY='1',**{scenario:'1'})
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                self.assertFalse(any(x['node']=='NODE-2' and 'apt-get' in x['command'] for x in self.node_commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_removal_and_parallel_apply_are_rejected(self):
        for scenario in ['TEST_REMOVAL','TEST_PARALLEL']:
            with self.subTest(scenario=scenario):
                result,commands=self.run_script('update.sh',TEST_APPLY='1',**{scenario:'1'})
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                self.assertFalse(any('--no-remove dist-upgrade' in c for c in commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_ready_node_updates_without_automatic_cleanup(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_SELECTED='NODE-1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(sum('--no-remove dist-upgrade' in c for c in commands),1)
        self.assertFalse(any('autoremove' in c or 'apt-get clean' in c for c in commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_missing_worker_summary_and_failed_verification_stop_queue(self):
        for scenario in ['TEST_LOST_WORKER','TEST_BAD_VERSION']:
            with self.subTest(scenario=scenario):
                result,commands=self.run_script('update.sh',TEST_APPLY='1',**{scenario:'1'})
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                self.assertFalse(any(x['node']=='NODE-2' and 'apt-get' in x['command'] for x in self.node_commands))

    @unittest.skipUnless((ROOT/'update.sh').exists() and BASH_MAJOR>=4,'requires updater and Bash 4+')
    def test_updater_preview_does_not_apply_package_changes(self):
        result,commands=self.run_script('update.sh')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(sum('apt-get -s dist-upgrade' in c for c in commands),2)
        self.assertFalse(any('-y dist-upgrade' in c or 'autoremove' in c or 'apt-get clean' in c for c in commands))

    @unittest.skipUnless(not (ROOT/'update.sh').exists(),'standalone reboot repository')
    def test_failed_qm_list_stops_before_reboot(self):
        result,commands=self.run_script('rolling-reboot.sh',TEST_LIST_FAILURE='qemu')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertIn('Could not list guests',result.stdout+result.stderr)
        self.assertFalse(any('systemctl reboot' in c or ' migrate ' in c for c in commands))

    @unittest.skipUnless(not (ROOT/'update.sh').exists(),'standalone reboot repository')
    def test_failed_pct_list_stops_before_reboot(self):
        result,commands=self.run_script('rolling-reboot.sh',TEST_LIST_FAILURE='lxc')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertIn('Could not list guests',result.stdout+result.stderr)
        self.assertFalse(any('systemctl reboot' in c or ' migrate ' in c for c in commands))


if __name__=='__main__': unittest.main()
