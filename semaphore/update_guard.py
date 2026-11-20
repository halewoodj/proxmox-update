#!/usr/bin/env python3
"""Read-only, fail-closed checks for each cluster update boundary."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time


class ClusterPending(ValueError):
    """Read-only waiting is allowed; this is never permission to mutate a node."""


WATCHDOG_PROBE = r'''
def watchdog_probe(proc=pathlib.Path('/proc'), run=pathlib.Path('/run'), mux_pid=None):
    if mux_pid is None:
        mux_pid=int(read(['systemctl','show','watchdog-mux','-p','MainPID','--value']))
    if mux_pid < 1 or (proc/str(mux_pid)/'comm').read_text().strip() != 'watchdog-mux':
        raise RuntimeError('Cannot identify the running watchdog multiplexer')
    sockets=set()
    for fd in (proc/str(mux_pid)/'fd').iterdir():
        link=str(fd.readlink())
        if link.startswith('socket:[') and link.endswith(']'):
            sockets.add(link[8:-1])
    rows=[line.split() for line in (proc/'net/unix').read_text().splitlines()[1:]]
    rows=[r for r in rows if len(r)==8 and r[7]==str(run/'watchdog-mux.sock')]
    if not rows or any(r[6] not in sockets for r in rows):
        raise RuntimeError('Watchdog socket ownership changed or cannot be verified')
    listeners=[r for r in rows if r[3]=='00010000' and r[4]=='0001' and r[5]=='01']
    if len(listeners)!=1:
        raise RuntimeError('Cannot verify the watchdog listener')
    # Accepted server sockets retain the listener's pathname in /proc/net/unix.
    # Count every non-listener, including unknown states, as an active client.
    return {'watchdog_active':(run/'watchdog-mux.active').exists() or len(rows)>1,
            'watchdog_verified':True}
'''

SNAPSHOT = r'''
import fcntl,json,pathlib,subprocess,socket,time
def read(args):
    p=subprocess.run(args,capture_output=True,text=True,timeout=45)
    if p.returncode:raise RuntimeError(' '.join(args)+': '+p.stderr.strip())
    return p.stdout.strip()
''' + WATCHDOG_PROBE + r'''
for name in ['lock','lock-frontend']:
    with open('/var/lib/dpkg/'+name,'rb') as f:
        fcntl.lockf(f,fcntl.LOCK_SH|fcntl.LOCK_NB)
        fcntl.lockf(f,fcntl.LOCK_UN)
print(json.dumps({
    'hostname':socket.gethostname().split('.')[0],
    'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
    'members':read(['pvecm','nodes']),
    'quorum':read(['pvecm','status']),
    'ceph':json.loads(read(['ceph','status','--format','json'])),
    'audit':read(['dpkg','--audit']),
    'ha':read(['ha-manager','status']),
    'ha_status':json.loads(read(['pvesh','get','/cluster/ha/status/current','--output-format','json'])),
    'now':time.time(),
    'crm_active':read(['systemctl','show','pve-ha-crm','-p','ActiveState','--value']),
    'crm_unit':read(['systemctl','show','pve-ha-crm','-p','UnitFileState','--value']),
    'resources':json.loads(read(['pvesh','get','/cluster/resources','--output-format','json'])),
    'ha_config':json.loads(read(['pvesh','get','/cluster/ha/resources','--output-format','json'])),
    'manager':json.loads(pathlib.Path('/etc/pve/ha/manager_status').read_text()),
    'active_tasks':json.loads(read(['pvesh','get','/nodes/'+socket.gethostname().split('.')[0]+'/tasks','--source','active','--output-format','json'])),
    **watchdog_probe(),
    'crm_commands':pathlib.Path('/etc/pve/ha/crm_commands').read_text() if pathlib.Path('/etc/pve/ha/crm_commands').exists() else '',
}))
'''


def cluster_baseline(snapshots):
    return {n:{'boot_id':d['boot_id'],'ceph_election_epoch':d['ceph']['election_epoch']}
            for n,d in snapshots.items()}


def maintenance_requested(data, node):
    return bool(data['manager'].get('node_request', {}).get(node, {}).get('maintenance'))


def lrm_maintenance(data, node):
    return bool(re.search(r'^lrm '+re.escape(node)+
                          r' \(maintenance(?: mode)?, watchdog (?:active|standby|released|closed), ',
                          data['ha'], re.MULTILINE))


def verify_selection(nodes, selected, snapshots):
    if not selected or len(selected)!=len(set(selected)) or not set(selected)<=set(nodes):
        raise ValueError('Selected nodes must be distinct approved inventory members')
    held={node for node in nodes for data in snapshots.values()
          if maintenance_requested(data,node) or lrm_maintenance(data,node)}
    blocked=[node for node in selected if held-{node}]
    if blocked:
        raise ValueError('Selected rollout blocked: existing HA maintenance on '+', '.join(sorted(held))+
                         ' prevents processing '+', '.join(blocked)+
                         '. Existing maintenance will be preserved; inspect and explicitly resolve the hold '
                         'before a wider rollout. No node jobs have started.')


def verify_cluster(expected, snapshots, baseline=None):
    if set(snapshots) != set(expected):
        raise ValueError('Missing node health evidence')
    epochs={d['ceph']['election_epoch'] for d in snapshots.values()}
    if any(type(epoch) is not int or epoch<1 for epoch in epochs):
        raise ValueError('Ceph monitor election evidence is missing or invalid')
    pending=[]
    advanced=[]
    for node, data in snapshots.items():
        if data['hostname'] != node:
            raise ValueError('Unexpected SSH hostname for ' + node)
        if not re.fullmatch(r'[0-9a-f-]{36}', data['boot_id']):
            raise ValueError('Invalid boot ID on ' + node)
        if baseline is not None:
            original=baseline.get(node)
            if not isinstance(original,dict) or 'ceph_election_epoch' not in original:
                raise ValueError('Unsupported health baseline; inspect before retrying')
            if original.get('boot_id') != data['boot_id']:
                raise ValueError('Unexpected reboot on ' + node + '; stop and investigate')
        members = [line.split()[2] for line in data['members'].splitlines()
                   if len(line.split()) >= 3 and line.split()[0].isdigit()]
        if len(members) != len(expected) or set(members) != set(expected):
            raise ValueError('Cluster membership incomplete on ' + node)
        if not re.search(r'^\s*Quorate:\s*Yes\s*$', data['quorum'], re.MULTILINE):
            raise ValueError('No quorum on ' + node)
        ceph = data['ceph']
        health=ceph['health']
        if health.get('mutes'):
            raise ValueError('Ceph health checks muted on '+node)
        if health['status'] != 'HEALTH_OK':
            checks=set(health.get('checks',{}))
            mons=ceph.get('monmap',{}).get('num_mons',0)
            quorum=ceph.get('quorum_names',[])
            if (health['status']=='HEALTH_WARN' and checks
                    and checks <= {'MON_DOWN','SLOW_OPS'} and type(mons) is int and mons>0
                    and len(set(quorum))==len(quorum) and len(quorum)>=mons//2+1):
                pending.append(node+': '+', '.join(sorted(checks)))
            else:
                raise ValueError('Ceph unhealthy on '+node+': '+json.dumps(health,sort_keys=True))
        if baseline is not None:
            epoch=original['ceph_election_epoch']
            if type(epoch) is not int or epoch<1 or ceph['election_epoch']<epoch:
                raise ValueError('Ceph election epoch regressed or baseline invalid on '+node)
            if epoch != ceph['election_epoch']:advanced.append(node)
        osds = ceph['osdmap']
        if not (osds['num_osds'] == osds['num_up_osds'] == osds['num_in_osds']):
            raise ValueError('Ceph has missing OSDs on ' + node)
        pgmap = ceph['pgmap']
        # Snapshot cleanup keeps PGs active and fully replicated. In particular,
        # scheduled backups can leave snaptrim flags while HEALTH_OK is retained.
        # See Ceph's pg-states documentation; snaptrim_error is not allowed.
        safe_states = {'active', 'clean', 'scrubbing', 'deep', 'snaptrim', 'snaptrim_wait'}
        if pgmap['num_pgs'] < 1 or sum(s['count'] for s in pgmap['pgs_by_state']
                                     if {'active','clean'} <= set(s['state_name'].split('+'))
                                     <= safe_states) != pgmap['num_pgs']:
            raise ValueError('Ceph placement groups are not all active+clean with supported background states on '
                             +node+': '+json.dumps(pgmap,sort_keys=True))
        if data['audit']:
            raise ValueError('Unfinished package configuration on ' + node + ': ' + data['audit'])
        daemons = [r for r in data['ha_status'] if r['type'] in ['master','lrm']]
        if len(daemons) != len(expected)+1:
            raise ValueError('Incomplete HA daemon status on ' + node)
        for row in daemons:
            if not -5 <= data['now']-row['timestamp'] <= 30:
                raise ValueError('Stale HA heartbeat on ' + row['node'])
    if len(epochs)!=1:
        pending.append('observers disagree on Ceph monitor election evidence')
    if pending:
        raise ClusterPending('Waiting for Ceph to recover fully: '+'; '.join(pending))
    return bool(advanced)


def verify_target(node, snapshots):
    # Maintenance appears before migration workers finish. The CRM watchdog is
    # independent of the LRM: an empty LRM does not make the HA master safe.
    target = snapshots[node]
    if target['crm_active'] != 'inactive' or target['crm_unit'] not in ['masked','masked-runtime']:
        raise ValueError(node + ' CRM must be gracefully stopped and temporarily masked on the evacuated node')
    if not target.get('watchdog_verified', False):
        raise ValueError(node + ' watchdog connection evidence is missing')
    if target.get('watchdog_active', True):
        raise ValueError(node + ' watchdog multiplexer still has active clients')
    for observer, data in snapshots.items():
        if not data['manager'].get('node_request',{}).get(node,{}).get('maintenance'):
            raise ValueError(node + ' HA maintenance request is not retained on ' + observer)
        if data['crm_commands'].strip():
            raise ValueError('Pending HA commands on ' + observer + '; wait before installing')
        ha = data['ha']
        if not ha.startswith('quorum OK\n'):
            raise ValueError('HA status incomplete on ' + observer)
        if re.search(r'^master ' + re.escape(node) + r'\s', ha, re.MULTILINE):
            raise ValueError(node + ' is the HA master; hand over the CRM before updating')
        match = re.search(r'^lrm ' + re.escape(node) + r' \(([^\n]+)\)$', ha, re.MULTILINE)
        # This installed HA version infers "watchdog active" from the maintenance
        # state even after the LRM closes its connection. Actual release is proved
        # above from the multiplexer sockets and active-client marker.
        if not match or not re.match(r'maintenance(?: mode)?, watchdog (?:active|standby|released|closed), ', match[1]):
            raise ValueError(node + ' must finish HA maintenance before updating')
        if any(r.get('node') == node and r.get('type') in ['qemu','lxc']
               and r.get('status') != 'stopped' for r in data['resources']):
            raise ValueError(node + ' still has running or unknown-state guests; evacuate them first')
        for line in ha.splitlines():
            if line.startswith('service ') and not re.search(r', (?:started|stopped|disabled)\)$', line):
                raise ValueError('HA service is not stable: ' + line)


def snapshot_once(node, *, env=None):
    command = shlex.join(['python3', '-c', SNAPSHOT])
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                             '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
                             'root@'+node, command], capture_output=True, text=True, timeout=240,env=env)
    if result.returncode:
        raise ValueError('Cannot verify ' + node + ': ' + result.stderr.strip())
    return node, json.loads(result.stdout)


def snapshot(node, *, read=None, env=None, sleep=time.sleep, clock=time.monotonic,
             lock_timeout=600, read_attempts=3):
    deadline=clock()+lock_timeout
    failures=0
    waiting=False
    while True:
        try:return (read or (lambda n:snapshot_once(n,env=env)))(node)
        except (ValueError,subprocess.TimeoutExpired) as error:
            detail=str(error)
            locked=all(t in detail for t in ['fcntl.lockf','BlockingIOError','Resource temporarily unavailable'])
            if locked:
                if clock()>=deadline:
                    raise ValueError('Timed out waiting for existing dpkg activity on '+node) from error
                if not waiting:
                    print('['+node+'] Waiting for existing dpkg activity to release package locks...',flush=True)
                    waiting=True
            else:
                transient=isinstance(error,subprocess.TimeoutExpired) or any(
                    t in detail for t in ['subprocess.TimeoutExpired','timed out','Connection reset by peer','Connection closed by'])
                failures+=1
                if not transient or failures>=read_attempts:raise
                print('['+node+'] Transient read failure; retry '+str(failures)+'/'+str(read_attempts-1)+': '+detail.splitlines()[-1],flush=True)
            sleep(5)


def collect_snapshots(nodes, *, env=None, read=None, clock=time.monotonic,
                      max_age=30, timeout=600):
    deadline=clock()+timeout
    def timed_read(node):
        started=clock()
        name,data=(read or (lambda n:snapshot(n,env=env)))(node)
        return name,data,started
    while True:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes),8)) as pool:
            rows=list(pool.map(timed_read,nodes))
        now=clock()
        if all(now-started<=max_age for _,_,started in rows):
            return {name:data for name,data,_ in rows}
        if now>=deadline:
            raise ValueError('Timed out collecting fresh cluster observations after package/read waits')
        print('Refreshing every node after a long package/read wait; earlier observations discarded.',flush=True)


def read_verified(nodes, baseline=None, *, collect=None, confirmed_epoch=None,
                  timeout=300, sleep=time.sleep, clock=time.monotonic):
    deadline=clock()+timeout
    recovering=False
    healthy_epoch=None
    healthy_count=0
    last=None
    while True:
        data=(collect or (lambda:collect_snapshots(nodes)))()
        try:
            advanced=verify_cluster(nodes,data,baseline)
        except ClusterPending as error:
            recovering=True;healthy_count=0
            if str(error)!=last:print(str(error),flush=True);last=str(error)
        else:
            epoch=next(iter(data.values()))['ceph']['election_epoch']
            settling=advanced and epoch!=confirmed_epoch
            if epoch!=healthy_epoch:healthy_count=0
            healthy_epoch=epoch;healthy_count+=1
            if not (recovering or settling) or healthy_count>=2:
                if recovering or settling:
                    print('Ceph fully healthy and stable at election epoch '+str(epoch)+
                          '; unchanged boot IDs and all cluster checks verified.',flush=True)
                return data
        if clock()>=deadline:
            raise ValueError('Timed out waiting for full, stable cluster health; no further node actions allowed')
        sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--target')
    parser.add_argument('--selected', help='Check the entire selected rollout before starting any node')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    nodes = args.nodes.split(',')
    if len(nodes) != len(set(nodes)) or not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', n) for n in nodes):
        raise ValueError('Invalid node inventory')
    if args.target is not None and args.target not in nodes:
        raise ValueError('Target outside approved inventory')
    if args.apply and not args.target:
        raise ValueError('Apply checks require a target node')
    path = Path(args.state)
    baseline = json.loads(path.read_text()) if path.exists() else None
    snapshots=read_verified(nodes,baseline)
    if args.selected is not None:
        verify_selection(nodes,args.selected.split(','),snapshots)
    if args.apply:
        verify_target(args.target, snapshots)
    if baseline is None:
        path.write_text(json.dumps(cluster_baseline(snapshots))+'\n')
        path.chmod(0o600)
    print('Cluster health, package state, unchanged boot IDs and consistent Ceph quorum verified.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print('Update safety gate: ' + str(error), file=sys.stderr)
        sys.exit(1)
