#!/usr/bin/env python3
"""Prepare and restore one HA node; never installs packages or reboots a host."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

from update_guard import snapshot, verify_cluster, verify_target, maintenance_requested, lrm_maintenance, read_verified, collect_snapshots


class MaintenanceInterrupted(Exception):
    pass


class MaintenancePending(ValueError):
    """Normal HA motion may finish; this is not permission to change the node."""


def remote(node, args):
    command = shlex.join(['timeout', '--preserve-status', '90s', *args])
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                             'root@'+node, command], capture_output=True, text=True, timeout=110)
    if result.returncode:
        raise ValueError(node+': '+shlex.join(args)+' failed: '+result.stderr.strip())
    if result.stdout.strip():
        print(result.stdout.strip(), flush=True)


def running(data, node):
    return [r for r in data['resources'] if r.get('node') == node
            and r.get('type') in ['qemu','lxc'] and r.get('status') != 'stopped']


def service_transitions(data):
    pending=[]
    for line in data['ha'].splitlines():
        if not line.startswith('service '):continue
        match=re.fullmatch(r'service (\S+) \(([^,]+), ([^)]+)\)',line)
        if not match:raise ValueError('Unrecognized HA service status on '+data['hostname']+': '+line)
        state=match[3]
        if state in ['started','stopped','disabled']:continue
        # ha-manager displays get_verbose_service_state(), not just CRM FSM
        # names: a successful migration can display "starting" until its running
        # flag arrives. These are pending observations, never permission to act.
        if state not in ['migrate','relocate','request_start','request_start_balance','request_stop',
                         'starting','stopping','queued','deleting']:
            raise ValueError('Unsafe HA service state on '+data['hostname']+': '+line)
        pending.append(line)
    return pending


def stable(data):
    return not service_transitions(data)


def active_work(data):
    # Open consoles are observational. Unknown and guest/storage task types block.
    return [t for t in data['active_tasks'] if t.get('type') not in
            ['vncshell','vncproxy','spiceproxy']]


def check_plan(nodes, target, snapshots):
    data = snapshots[target]
    pending=[]
    if data['manager'].get('disarm'):
        raise ValueError('HA stack is disarmed or changing its armed state')
    for observer in snapshots.values():
        if maintenance_requested(observer,target) != maintenance_requested(data,target):
            raise ValueError('Observers disagree on maintenance ownership; wait before preparing '+target)
        if observer['crm_commands'].strip():
            raise ValueError('HA commands are pending; wait before maintenance')
        for node in nodes:
            if node != target and (maintenance_requested(observer, node)
                                    or lrm_maintenance(observer, node)):
                raise ValueError(node+' already has HA maintenance; prepare only one node at a time')
        pending.extend(service_transitions(observer))
        tasks=active_work(observer)
        managed={r['sid'] for r in observer['ha_config']}
        startup_tasks={'qmstart':'vm:','qmstop':'vm:','qmshutdown':'vm:',
                       'vzstart':'ct:','vzstop':'ct:','vzshutdown':'ct:'}
        def settling_task(task):
            kind=task.get('type')
            return kind in ['qmigrate','vzmigrate','vzdump'] or (kind in startup_tasks
                    and startup_tasks[kind]+str(task.get('id','')) in managed)
        if any(not settling_task(t) for t in tasks):
            raise ValueError('Active Proxmox tasks on '+observer['hostname']+'; wait before maintenance')
        pending.extend('Proxmox task '+str(t.get('type'))+
                       (' '+str(t['id']) if t.get('id') else '')+
                       ' on '+observer['hostname'] for t in tasks)
    if pending:
        raise MaintenancePending('HA is settling: '+'; '.join(sorted(set(pending))))
    if data['crm_active'] not in ['active','inactive']:
        raise ValueError(target+' CRM is changing state')
    if data['crm_unit'] not in ['enabled','disabled','static','masked','masked-runtime']:
        raise ValueError(target+' CRM has unsupported unit state '+data['crm_unit'])
    if data['crm_active'] == 'active' and data['crm_unit'] in ['masked','masked-runtime']:
        raise ValueError(target+' CRM is active with a pre-existing mask; inspect before automatic maintenance')
    guests = running(data, target)
    managed = {r['sid'] for r in data['ha_config']}
    for guest in guests:
        sid = ('vm:' if guest['type'] == 'qemu' else 'ct:')+str(guest['vmid'])
        if sid not in managed:
            raise ValueError(sid+' is running outside HA; migrate it manually before automatic preparation')
    node_rows = {r['node']:r for r in data['resources'] if r.get('type') == 'node'}
    if set(node_rows) != set(nodes):
        raise ValueError('Missing node capacity evidence')
    free = [max(0, int(node_rows[n]['maxmem'])-int(node_rows[n]['mem'])-2*1024**3)
            for n in nodes if n != target]
    needed = sum(int(g['maxmem']) for g in guests)
    if needed > sum(free) or any(int(g['maxmem']) > max(free) for g in guests):
        raise ValueError('Insufficient peer memory headroom for '+target+' evacuation')
    return guests


class Maintenance:
    def __init__(self, nodes, target, path, baseline, *, collect=None, execute=remote,
                 sleep=time.sleep, clock=time.monotonic, timeout=1800):
        self.nodes, self.target = nodes, target
        self.path, self.baseline = Path(path), baseline
        self.collect, self.execute = collect, execute
        self.sleep, self.clock, self.timeout = sleep, clock, timeout
        self.confirmed_epoch=None

    def read(self):
        def collect():
            if self.collect:data=self.collect()
            else:data=collect_snapshots(self.nodes)
            for observer in data.values():service_transitions(observer)
            return data
        data=read_verified(self.nodes,self.baseline,collect=collect,
                           confirmed_epoch=self.confirmed_epoch,timeout=min(self.timeout,300),
                           sleep=self.sleep,clock=self.clock)
        self.confirmed_epoch=next(iter(data.values()))['ceph']['election_epoch']
        return data

    def record(self, state, phase):
        state['phase'] = phase
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, indent=2)+'\n')
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def wait(self, description, predicate, *, consecutive=1):
        print('['+self.target+'] Waiting for '+description+'...', flush=True)
        deadline = self.clock()+self.timeout
        ready_count=0
        while True:
            data = self.read()
            if predicate(data):
                ready_count+=1
                if ready_count>=consecutive:return data
            else:
                ready_count=0
            if self.clock() >= deadline:
                raise ValueError('Timed out waiting for '+description+' on '+self.target)
            if not ready_count:self.sleep(5)

    def wait_ready(self):
        last_pending=None
        def ready(data):
            nonlocal last_pending
            try:
                check_plan(self.nodes,self.target,data)
                return True
            except MaintenancePending as error:
                if str(error)!=last_pending:
                    print('['+self.target+'] '+str(error)+'; waiting before changing this node.',flush=True)
                    last_pending=str(error)
                return False
        return self.wait('HA readiness before preparation',ready,consecutive=2)

    def prepare(self):
        if self.path.exists():
            raise ValueError('Maintenance state already exists; inspect '+str(self.path))
        data = self.wait_ready()
        original = data[self.target]
        state = {'node':self.target, 'baseline':self.baseline,
                 'original_crm_active':original['crm_active'],
                 'original_crm_unit':original['crm_unit'],
                 'original_maintenance':maintenance_requested(original,self.target),
                 'maintenance_owned':False, 'mask_owned':False, 'crm_stop_owned':False}
        self.record(state, 'planned')
        if not state['original_maintenance']:
            state['maintenance_owned'] = True
            self.record(state, 'requesting_maintenance')
            self.execute(self.target, ['ha-manager','crm-command','node-maintenance','enable',self.target])
        data = self.wait('HA evacuation and stable services',
                         lambda d: all(maintenance_requested(v,self.target)
                                       and lrm_maintenance(v,self.target) and not running(v,self.target)
                                       and stable(v) and not active_work(v) for v in d.values()))
        self.record(state, 'evacuated')
        if state['original_crm_active'] == 'active':
            state['crm_stop_owned'] = True
            self.record(state, 'stopping_crm')
            self.execute(self.target, ['systemctl','stop','pve-ha-crm'])
        self.wait('CRM handover and all local watchdog clients released',
                  lambda d: d[self.target]['crm_active'] == 'inactive'
                  and d[self.target].get('watchdog_verified',False)
                  and not d[self.target]['watchdog_active']
                  and all(not re.search(r'^master '+re.escape(self.target)+r'\s',v['ha'],re.MULTILINE)
                          for v in d.values()))
        if state['original_crm_unit'] not in ['masked','masked-runtime']:
            state['mask_owned'] = True
            self.record(state, 'masking_crm')
            self.execute(self.target, ['systemctl','mask','--runtime','pve-ha-crm'])
        verify_target(self.target, self.read())
        self.record(state, 'prepared')
        print('['+self.target+'] Maintenance prepared; recovery state: '+str(self.path), flush=True)

    def plan(self):
        guests = check_plan(self.nodes,self.target,self.wait_ready())
        print('['+self.target+'] Plan only: HA evacuates '+str(len(guests))+
              ' running guest(s), releases LRM watchdog, hands over/stops CRM, '
              'temporarily masks CRM; settings restored after successful package verification. '
              'No maintenance changes made.', flush=True)

    def restore(self):
        state = json.loads(self.path.read_text())
        if state['node'] != self.target or state['baseline'] != self.baseline or state['phase'] != 'prepared':
            raise ValueError('Maintenance state is not a completed preparation for this run')
        verify_target(self.target, self.read())
        if state['mask_owned']:
            if self.read()[self.target]['crm_unit'] != 'masked-runtime':
                raise ValueError('CRM mask changed externally; preserve it and inspect '+str(self.path))
            self.record(state, 'unmasking_crm')
            self.execute(self.target, ['systemctl','unmask','--runtime','pve-ha-crm'])
            if self.read()[self.target]['crm_unit'] != state['original_crm_unit']:
                raise ValueError('Underlying CRM unit state changed; inspect '+str(self.path))
        if state['crm_stop_owned']:
            self.record(state, 'starting_crm')
            self.execute(self.target, ['systemctl','start','pve-ha-crm'])
            self.wait('CRM service recovery', lambda d:d[self.target]['crm_active'] == 'active')
        if state['maintenance_owned']:
            self.record(state, 'releasing_maintenance')
            self.execute(self.target, ['ha-manager','crm-command','node-maintenance','disable',self.target])
            self.wait('HA maintenance exit and stable services',
                      lambda d:all(not maintenance_requested(v,self.target)
                                   and not lrm_maintenance(v,self.target) and stable(v)
                                   and not active_work(v) for v in d.values()),consecutive=2)
        self.read()
        self.record(state, 'restored')
        print('['+self.target+'] Original HA/CRM settings restored; cluster verified.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan','prepare','restore'])
    parser.add_argument('--nodes', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    nodes = args.nodes.split(',')
    if len(nodes)<2 or len(nodes)!=len(set(nodes)) or args.target not in nodes or not all(
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',n) for n in nodes):
        raise ValueError('Invalid maintenance node inventory')
    if args.timeout < 1:
        raise ValueError('Maintenance timeout must be positive')
    baseline = json.loads(Path(args.baseline).read_text())
    if set(baseline) != set(nodes):
        raise ValueError('Boot baseline differs from approved inventory')
    helper = Maintenance(nodes,args.target,args.state,baseline,timeout=args.timeout)
    getattr(helper,args.action)()


def interrupted(signum, frame):
    # Selectors/subprocess can retry InterruptedError. Use a distinct exception
    # so an in-flight SSH child is terminated and preparation cannot continue.
    raise MaintenanceInterrupted('Controller interrupted; retain maintenance state and inspect before retrying')


if __name__ == '__main__':
    for sig in [signal.SIGINT,signal.SIGTERM,signal.SIGHUP]:
        signal.signal(sig,interrupted)
    try:
        main()
    except MaintenanceInterrupted as error:
        print(str(error),file=sys.stderr)
        sys.exit(130)
    except (ValueError,KeyError,TypeError,OSError,subprocess.SubprocessError) as error:
        print('Maintenance stopped: '+str(error)+
              '. No automatic failure rollback; inspect the saved state before releasing maintenance.',file=sys.stderr)
        sys.exit(1)
