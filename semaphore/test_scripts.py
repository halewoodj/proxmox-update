"""Exercise upstream script failure paths using fake SSH only."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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
 installed=os.path.exists(os.environ['TEST_FIXTURE']+'.installed.'+node)
 if os.environ.get('TEST_POST_REBOOT') and installed: data['boot_id']='ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee'
 if os.environ.get('TEST_POST_CEPH') and installed:data['ceph']['health']['status']='HEALTH_WARN'
 if os.environ.get('TEST_POST_ELECTION') and any(os.path.exists(os.environ['TEST_FIXTURE']+'.installed.'+n) for n in ['NODE-1','NODE-2']):data['ceph']['election_epoch']+=2
 if os.path.exists(os.environ['TEST_FIXTURE']+'.handoff'):
  counter=os.environ['TEST_FIXTURE']+'.handoff.'+node
  count=int(open(counter).read())+1 if os.path.exists(counter) else 1
  open(counter,'w').write(str(count))
  if count<=3:
   state='error' if os.environ.get('TEST_HANDOFF_ERROR') and count==2 else ('starting' if count==3 else 'migrate')
   data['ha']=data['ha'].replace('NODE-2, started','NODE-2, '+state)
   data['active_tasks']=[{'type':'qmstart' if state=='starting' else 'qmigrate','id':'100'}]
 print(json.dumps(data))
elif command.startswith('timeout --preserve-status 90s '):
 args=tokens[3:]
 if os.environ.get('TEST_CANCEL') and args[0]=='ha-manager':
  import time;time.sleep(20)
 if os.environ.get('TEST_PREP_FAIL') and args[0]=='ha-manager':
  print('injected evacuation refusal',file=sys.stderr);sys.exit(1)
 fixture=json.load(open(os.environ['TEST_FIXTURE']))
 if args[0]=='ha-manager':
  enable=args[-2]=='enable'
  for d in fixture.values():
   d['manager']={'node_request':{node:{'maintenance':1}}} if enable else {}
   old,new=('active, watchdog active','maintenance, watchdog standby') if enable else ('maintenance, watchdog standby','active, watchdog active')
   d['ha']=d['ha'].replace('lrm '+node+' ('+old,'lrm '+node+' ('+new)
 elif args[1]=='stop':
  fixture[node].update(crm_active='inactive',watchdog_active=False)
  peer=next(n for n in fixture if n!=node)
  for d in fixture.values():
   d['ha']=d['ha'].replace('master '+node+' ','master '+peer+' ')
   for row in d['ha_status']:
    if row['type']=='master' and row['node']==node:row['node']=peer
 elif args[1]=='mask':fixture[node]['crm_unit']='masked-runtime'
 elif args[1]=='unmask':fixture[node]['crm_unit']='enabled'
 elif args[1]=='start':fixture[node]['crm_active']='active'
 with open(os.environ['TEST_FIXTURE'],'w') as f:json.dump(fixture,f)
elif command=='true' or command.startswith('command -v '):
 if command=='true' and node=='NODE-2' and os.environ.get('TEST_HANDOFF'):
  open(os.environ['TEST_FIXTURE']+'.handoff','w').close()
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
elif '-s dist-upgrade' in command:
 if not os.environ.get('TEST_NO_CHANGES'):print('Inst example [1] (2 repo [amd64])')
 if os.environ.get('TEST_REMOVAL'):print('Remv proxmox-ve [9.2]')
elif '--no-remove dist-upgrade' in command:
 if os.environ.get('TEST_UPGRADE_FAIL'):
  print('dpkg exact failure diagnostic',file=sys.stderr);sys.exit(100)
 open(os.environ['TEST_FIXTURE']+'.installed.'+node,'w').close()
elif command.startswith('dpkg-query -W '):
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
            fixture=tmp/'fixture.json';data=healthy()
            if extra.get('TEST_AUTO') or not extra.get('TEST_APPLY'):
                for d in data.values():
                    d.update(manager={},active_tasks=[],ha_config=[{'sid':'vm:100'}],crm_commands='')
                    d.update(crm_active='active',crm_unit='enabled',watchdog_active=True)
                    d['ha']=d['ha'].replace('maintenance, watchdog standby','active, watchdog active')
                    d['resources']=[{'type':'node','node':n,'maxmem':64*1024**3,'mem':8*1024**3} for n in data]
                data['NODE-1'].update(crm_active='active',crm_unit='enabled',watchdog_active=True)
            if extra.get('TEST_EXISTING_MAINT'):
                for d in data.values():
                    d['manager']={'node_request':{'NODE-1':{'maintenance':1}}}
                    d['ha']=d['ha'].replace('lrm NODE-1 (active, watchdog active','lrm NODE-1 (maintenance mode, watchdog standby')
            fixture.write_text(json.dumps(data))
            nodes=tmp/'nodes.jsonl'
            env=os.environ.copy()
            env.update(PATH=str(tmp)+os.pathsep+env['PATH'],TEST_COMMAND_LOG=str(log),
                       TEST_BASH=BASH,TERM='dumb',TEST_FIXTURE=str(fixture),TEST_NODE_LOG=str(nodes),**extra)
            args=[BASH,str(ROOT/filename),'--cluster-host','root@NODE-1',
                  '--nodes',extra.get('TEST_SELECTED','NODE-1,NODE-2')]
            if not extra.get('TEST_APPLY'):args.append('--dry-run')
            if extra.get('TEST_PARALLEL'):args.extend(['--jobs','2'])
            if extra.get('TEST_AUTO'):args.append('--auto-maintenance')
            if filename=='rolling-reboot.sh': args.extend(['--yes','--direct-node-ssh','--ceph-timeout','1'])
            if extra.get('TEST_CANCEL'):
                process=subprocess.Popen(args,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    deadline=time.monotonic()+15
                    while time.monotonic()<deadline:
                        if log.exists() and 'node-maintenance enable' in log.read_text():break
                        time.sleep(.05)
                    else:self.fail('Maintenance preparation did not start')
                    process.send_signal(signal.SIGTERM)
                    stdout,stderr=process.communicate(timeout=10)
                    result=subprocess.CompletedProcess(args,process.returncode,stdout,stderr)
                finally:
                    if process.poll() is None:process.kill();process.communicate()
            else:
                result=subprocess.run(args,env=env,capture_output=True,text=True,timeout=30)
            commands=[json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            self.node_commands=[json.loads(line) for line in nodes.read_text().splitlines()] if nodes.exists() else []
            # The script deliberately retains evidence. Test fixtures can be removed.
            match=re.search(r'Diagnostic logs and summaries: (.+)',result.stdout)
            if match:shutil.rmtree(match[1],ignore_errors=True)
            return result, commands

    @unittest.skipUnless((ROOT/'update.sh').exists() and BASH_MAJOR>=4,'requires updater and Bash 4+')
    def test_failed_apt_update_returns_failure(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',TEST_APT_FAIL='1')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertIn('apt-get update failed',result.stdout)
        self.assertFalse(any('dist-upgrade' in c for c in commands))
        self.assertIn('repository download diagnostic',result.stderr)
        self.assertFalse(any(x['node']=='NODE-2' and 'APT::Update::Error-Mode=any update' in x['command'] for x in self.node_commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_upgrade_failure_stops_before_second_node_and_prints_diagnostic(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',TEST_UPGRADE_FAIL='1')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertIn('dpkg exact failure diagnostic',result.stderr)
        self.assertIn('Skipped after an earlier node failed',result.stdout)
        self.assertFalse(any(x['node']=='NODE-2' and 'apt-get' in x['command'] for x in self.node_commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_armed_ha_blocks_package_changes(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_SELECTED='NODE-1',TEST_ARMED='1')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertFalse(any('--no-remove dist-upgrade' in c for c in commands))
        self.assertFalse(any('APT::Update::Error-Mode=any update' in c for c in commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_reboot_or_ceph_warning_after_success_stops_remaining_nodes(self):
        for scenario in ['TEST_POST_REBOOT','TEST_POST_CEPH','TEST_POST_ELECTION']:
            with self.subTest(scenario=scenario):
                result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',**{scenario:'1'})
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                self.assertFalse(any(x['node']=='NODE-2' and 'apt-get' in x['command'] for x in self.node_commands))
                if scenario=='TEST_POST_ELECTION':
                    self.assertIn('election changed',result.stdout+result.stderr)

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_removal_and_parallel_apply_are_rejected(self):
        for scenario in ['TEST_REMOVAL','TEST_PARALLEL']:
            with self.subTest(scenario=scenario):
                result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',**{scenario:'1'})
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                self.assertFalse(any('--no-remove dist-upgrade' in c for c in commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_ready_node_updates_without_automatic_cleanup(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_SELECTED='NODE-1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(sum('--no-remove dist-upgrade' in c for c in commands),1)
        self.assertFalse(any('autoremove' in c or 'apt-get clean' in c for c in commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_existing_maintenance_blocks_whole_rollout_without_node_changes(self):
        for mode in [{},{'TEST_APPLY':'1'}]:
            with self.subTest(mode=mode):
                result,commands=self.run_script('update.sh',TEST_AUTO='1',TEST_EXISTING_MAINT='1',**mode)
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                self.assertIn('Selected rollout blocked: existing HA maintenance on NODE-1',result.stdout+result.stderr)
                self.assertIn('no node jobs started',result.stdout+result.stderr)
                self.assertNotIn('Starting Node Jobs',result.stdout)
                self.assertFalse(any('apt-get' in c or c.startswith('timeout --preserve-status 90s ') for c in commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_single_held_node_remains_scoped_and_preserves_its_hold(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',TEST_EXISTING_MAINT='1',TEST_SELECTED='NODE-1',TEST_NO_CHANGES='1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse(any('node-maintenance disable' in c or 'node-maintenance enable' in c for c in commands))
        self.assertFalse(any(r['node']=='NODE-2' and 'apt-get' in r['command'] for r in self.node_commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_automatic_apply_prepares_before_install_and_restores_after_verification(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',TEST_SELECTED='NODE-1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        indices={key:next(i for i,c in enumerate(commands) if key in c) for key in [
            'node-maintenance enable','systemctl stop','systemctl mask','APT::Update::Error-Mode=any update','--no-remove dist-upgrade',
            'dpkg-query -W','systemctl unmask','systemctl start','node-maintenance disable']}
        self.assertEqual(list(indices.values()),sorted(indices.values()))
        self.assertTrue(all('Dir::Cache::pkgcache=' in c and 'Dir::Cache::srcpkgcache=' in c
                            for c in commands if 'apt-get' in c))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_no_package_changes_still_protects_refresh_and_restores(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',TEST_SELECTED='NODE-1',TEST_NO_CHANGES='1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse(any('--no-remove dist-upgrade' in c for c in commands))
        keys=['systemctl mask','APT::Update::Error-Mode=any update','systemctl unmask','node-maintenance disable']
        indices=[next(i for i,c in enumerate(commands) if key in c) for key in keys]
        self.assertEqual(indices,sorted(indices))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_next_node_waits_for_late_failback_and_completes_both_nodes(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',TEST_HANDOFF='1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('[NODE-2] HA is settling:',result.stdout)
        self.assertIn('starting',result.stdout)
        self.assertEqual(sum('--no-remove dist-upgrade' in c for c in commands),2)
        self.assertTrue(any(r['node']=='NODE-2' and '--no-remove dist-upgrade' in r['command'] for r in self.node_commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_next_node_stops_if_pending_failback_becomes_an_error(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',TEST_HANDOFF='1',TEST_HANDOFF_ERROR='1')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertIn('Unsafe HA service state',result.stdout+result.stderr)
        self.assertFalse(any(r['node']=='NODE-2' and ('apt-get' in r['command'] or 'node-maintenance enable' in r['command']) for r in self.node_commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_automatic_preparation_or_package_failure_never_restores_or_continues(self):
        for scenario in ['TEST_PREP_FAIL','TEST_APT_FAIL','TEST_UPGRADE_FAIL']:
            with self.subTest(scenario=scenario):
                result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',**{scenario:'1'})
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                if scenario=='TEST_PREP_FAIL':self.assertFalse(any('apt-get' in c for c in commands))
                self.assertFalse(any('node-maintenance disable' in c or 'systemctl unmask' in c for c in commands))
                self.assertFalse(any(x['node']=='NODE-2' and 'apt-get' in x['command'] for x in self.node_commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_automatic_preview_only_prints_maintenance_plan(self):
        result,commands=self.run_script('update.sh',TEST_AUTO='1',TEST_SELECTED='NODE-1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('Plan only',result.stdout)
        self.assertFalse(any('node-maintenance' in c or 'systemctl mask' in c or '--no-remove dist-upgrade' in c for c in commands))
        self.assertFalse(any('APT::Update::Error-Mode=any update' in c for c in commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_cancelling_preparation_never_starts_install_or_next_node(self):
        result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',TEST_CANCEL='1')
        self.assertEqual(result.returncode,130,result.stdout+result.stderr)
        self.assertFalse(any('--no-remove dist-upgrade' in c or 'node-maintenance disable' in c for c in commands))
        self.assertFalse(any(x['node']=='NODE-2' and 'apt-get' in x['command'] for x in self.node_commands))

    @unittest.skipUnless(BASH_MAJOR>=4,'requires Bash 4+')
    def test_missing_worker_summary_and_failed_verification_stop_queue(self):
        for scenario in ['TEST_LOST_WORKER','TEST_BAD_VERSION']:
            with self.subTest(scenario=scenario):
                result,commands=self.run_script('update.sh',TEST_APPLY='1',TEST_AUTO='1',**{scenario:'1'})
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                self.assertFalse(any(x['node']=='NODE-2' and 'apt-get' in x['command'] for x in self.node_commands))

    @unittest.skipUnless((ROOT/'update.sh').exists() and BASH_MAJOR>=4,'requires updater and Bash 4+')
    def test_updater_preview_does_not_apply_package_changes(self):
        result,commands=self.run_script('update.sh')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(sum('-s dist-upgrade' in c for c in commands),2)
        self.assertFalse(any('APT::Update::Error-Mode=any update' in c for c in commands))
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
