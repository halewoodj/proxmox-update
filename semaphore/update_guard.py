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


def verify_cluster(expected, snapshots, baseline=None):
    if set(snapshots) != set(expected):
        raise ValueError('Missing node health evidence')
    for node, data in snapshots.items():
        if data['hostname'] != node:
            raise ValueError('Unexpected SSH hostname for ' + node)
        if not re.fullmatch(r'[0-9a-f-]{36}', data['boot_id']):
            raise ValueError('Invalid boot ID on ' + node)
        if baseline is not None and baseline.get(node) != data['boot_id']:
            raise ValueError('Unexpected reboot on ' + node + '; stop and investigate')
        members = [line.split()[2] for line in data['members'].splitlines()
                   if len(line.split()) >= 3 and line.split()[0].isdigit()]
        if len(members) != len(expected) or set(members) != set(expected):
            raise ValueError('Cluster membership incomplete on ' + node)
        if not re.search(r'^\s*Quorate:\s*Yes\s*$', data['quorum'], re.MULTILINE):
            raise ValueError('No quorum on ' + node)
        ceph = data['ceph']
        if ceph['health']['status'] != 'HEALTH_OK' or ceph['health'].get('mutes'):
            raise ValueError('Ceph unhealthy or health checks muted on ' + node + ': '
                             + json.dumps(ceph['health'],sort_keys=True))
        osds = ceph['osdmap']
        if not (osds['num_osds'] == osds['num_up_osds'] == osds['num_in_osds']):
            raise ValueError('Ceph has missing OSDs on ' + node)
        pgmap = ceph['pgmap']
        safe_states = {'active', 'clean', 'scrubbing', 'deep'}
        if pgmap['num_pgs'] < 1 or sum(s['count'] for s in pgmap['pgs_by_state']
                                     if {'active','clean'} <= set(s['state_name'].split('+'))
                                     <= safe_states) != pgmap['num_pgs']:
            raise ValueError('Ceph placement groups are not all active+clean on ' + node)
        if data['audit']:
            raise ValueError('Unfinished package configuration on ' + node + ': ' + data['audit'])
        daemons = [r for r in data['ha_status'] if r['type'] in ['master','lrm']]
        if len(daemons) != len(expected)+1:
            raise ValueError('Incomplete HA daemon status on ' + node)
        for row in daemons:
            if not -5 <= data['now']-row['timestamp'] <= 30:
                raise ValueError('Stale HA heartbeat on ' + row['node'])


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


def snapshot(node):
    command = shlex.join(['python3', '-c', SNAPSHOT])
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                             '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
                             'root@'+node, command], capture_output=True, text=True, timeout=240)
    if result.returncode:
        raise ValueError('Cannot verify ' + node + ': ' + result.stderr.strip())
    return node, json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--target')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    nodes = args.nodes.split(',')
    if len(nodes) != len(set(nodes)) or not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', n) for n in nodes):
        raise ValueError('Invalid node inventory')
    if args.target is not None and args.target not in nodes:
        raise ValueError('Target outside approved inventory')
    if args.apply and not args.target:
        raise ValueError('Apply checks require a target node')
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes), 8)) as pool:
        snapshots = dict(pool.map(snapshot, nodes))
    path = Path(args.state)
    baseline = json.loads(path.read_text()) if path.exists() else None
    verify_cluster(nodes, snapshots, baseline)
    if args.apply:
        verify_target(args.target, snapshots)
    if baseline is None:
        path.write_text(json.dumps({n:d['boot_id'] for n,d in snapshots.items()})+'\n')
        path.chmod(0o600)
    print('Cluster health, package state and unchanged boot IDs verified.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print('Update safety gate: ' + str(error), file=sys.stderr)
        sys.exit(1)
