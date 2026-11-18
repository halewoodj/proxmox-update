#!/usr/bin/env python3
"""Explicit operator rollout: finish APT through acknowledged boot-storage stalls.

This entry point is separate from the normal updater. It still evacuates one
node, releases watchdog clients, forbids removals and verifies complete recovery
before restoring that node or starting another. It never reboots a host.
"""
import argparse
import concurrent.futures
import copy
import fcntl
import json
import inspect
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

import run
from maintenance import Maintenance, MaintenanceInterrupted, service_transitions
from update_guard import (snapshot, cluster_baseline, verify_cluster,
                          verify_selection, verify_target)

ROOT = Path(__file__).resolve().parents[1]
APT = ('DEBIAN_FRONTEND=noninteractive nice -n 19 ionice -c 3 apt-get '
       '-o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= '
       '-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold ')
current_package = None
cancelled = False


def stop(signum, frame):
    global cancelled
    cancelled = True
    if current_package is not None:
        current_package.terminate()
    else:
        raise MaintenanceInterrupted('Operator rollout interrupted; preserve maintenance state')


def collect(nodes):
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as pool:
        return dict(pool.map(snapshot, nodes))


def verify_recovery(nodes, data, boots, repair=None):
    """Accept a new election epoch only with complete, healthy observations."""
    checked = copy.deepcopy(data)
    for node, row in checked.items():
        if row['boot_id'] != boots[node]:
            raise ValueError('Unexpected reboot on '+node)
        if row['audit'] and repair and node == repair[0]:
            # Exact audit, not arbitrary broken dependency or dpkg overrides.
            allowed = {repair[1]} if isinstance(repair[1],str) else set(repair[1])
            trigger_packages = {'dbus','man-db','procps','systemd','pve-manager','pve-ha-manager'}
            found = False
            for paragraph in row['audit'].split('\n\n'):
                packages = {line.split()[0] for line in paragraph.splitlines() if line.startswith(' ')}
                if not packages:continue
                found = True
                permitted = trigger_packages if paragraph.startswith(
                    'The following packages have been triggered, but the trigger processing') else allowed
                if not packages <= permitted:
                    raise ValueError('Unreviewed package damage on '+node+': '+row['audit'])
            if not found:
                raise ValueError('Unrecognized package audit on '+node)
            row['audit'] = ''
    baseline = cluster_baseline(checked)
    verify_cluster(nodes, checked, baseline)
    for row in checked.values():
        service_transitions(row)
    return baseline


def resume_state(path, target, data, boots):
    state = json.loads(path.read_text())
    if (state['node'] != target or state['phase'] != 'prepared'
            or state['original_crm_active'] != 'active'
            or state['original_crm_unit'] != 'enabled'
            or state['original_maintenance'] is not False
            or not all(state[k] is True for k in
                       ['maintenance_owned','mask_owned','crm_stop_owned'])):
        raise ValueError('Resume state is not the reviewed updater-owned preparation')
    if set(state['baseline']) != set(boots) or any(
            state['baseline'][n]['boot_id'] != boots[n] for n in boots):
        raise ValueError('Resume state boot identities changed')
    verify_selection(list(boots), [target], data)
    verify_target(target, data)
    return state


def plan_packages(output, *, complete=True):
    if re.search(r'^Remv ', output, re.MULTILINE):
        raise ValueError('Package removals require a separate review')
    if complete and re.search(r'\d+ not upgraded', output) and not re.search(r'\b0 not upgraded', output):
        raise ValueError('APT is keeping packages back; inspect the plan')
    return [line.split()[1] for line in output.splitlines() if line.startswith('Inst ')]


def recover_cached_archive(package, root=Path('/var/cache/apt/archives'),
                           query=subprocess.check_output, execute=subprocess.run):
    """Use the already APT-downloaded archive to replay dpkg's interrupted unpack."""
    status = query(['dpkg-query','-W','-f=${db:Status-Abbrev}',package],text=True).strip()
    if 'R' not in status:
        print('Interrupted unpack already replayed; proceed to dependency repair.',flush=True)
        return
    installed = query(['dpkg-query','-W','-f=${Version}',package],text=True).strip()
    selected = None
    selected_version = installed
    for archive in root.glob(package+'_*.deb'):
        if query(['dpkg-deb','-f',str(archive),'Package'],text=True).strip() != package:
            raise ValueError('Cached archive package identity differs')
        version = query(['dpkg-deb','-f',str(archive),'Version'],text=True).strip()
        if execute(['dpkg','--compare-versions',version,'ge',selected_version]).returncode == 0:
            selected, selected_version = archive, version
    if selected is None:
        raise ValueError('No suitable already-downloaded recovery archive; refusing a downgrade')
    print('Recovering interrupted unpack with '+str(selected)+' version '+selected_version,flush=True)
    execute(['dpkg','--unpack',str(selected)],check=True)


def archive_recovery_command(package):
    code = 'import subprocess\nfrom pathlib import Path\n'+inspect.getsource(recover_cached_archive)
    packages = [package] if isinstance(package,str) else package
    code += '\nfor package in '+repr(packages)+':\n recover_cached_archive(package)\n'
    return shlex.join(['nice','-n','19','ionice','-c','3','python3','-c',code])


def package(node, directory, label, command, timeout=7200):
    global current_package
    if cancelled:
        raise MaintenanceInterrupted('Rollout cancelled before package operation')
    print('['+node+'] '+label, flush=True)
    log = directory/(node+'.'+label.replace(' ', '-')+'.log')
    argv = [sys.executable, str(ROOT/'semaphore/parent_guard.py'), str(os.getpid()),
            sys.executable, str(ROOT/'semaphore/remote_apt.py'), '--node', node,
            '--timeout', str(timeout), '--directory', str(directory), '--', command]
    with log.open('w') as out:
        current_package = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT)
        try:
            result = current_package.wait()
        finally:
            current_package = None
    output = log.read_text()
    print(output, flush=True)
    if result or cancelled:
        raise ValueError(label+' failed or was cancelled on '+node+'; retained log '+str(log))
    return output


def recovered(nodes, boots, directory, label, repair=None, timeout=900):
    deadline = time.monotonic()+timeout
    last = None
    while True:
        try:
            data = collect(nodes)
            baseline = verify_recovery(nodes, data, boots, repair)
            (directory/(label+'.snapshots.json')).write_text(json.dumps(data, indent=2)+'\n')
            return data, baseline
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            if str(error) != last:
                print('Waiting for full cluster recovery: '+str(error), flush=True)
                last = str(error)
            if time.monotonic() >= deadline:
                raise ValueError('Cluster recovery timed out; preserve the current HA hold') from error
            time.sleep(5)


def rebase_owned(helper, data, baseline):
    state = json.loads(helper.path.read_text())
    verify_target(helper.target, data)
    if state['phase'] != 'prepared':
        raise ValueError('Maintenance ownership changed')
    state.setdefault('operator_previous_baselines', []).append(state['baseline'])
    state['baseline'] = baseline
    helper.baseline = baseline
    helper.record(state, 'prepared')


def worker(args):
    for sig in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]:
        signal.signal(sig, stop)
    nodes = run.inventory(os.environ['PVE_NODES_JSON'])
    selected = run.selected_nodes(nodes, args.nodes)
    if args.resume_node not in selected:
        raise ValueError('Resume node must be selected')
    data = collect(nodes)
    boots = {n:d['boot_id'] for n,d in data.items()}
    repair = (args.resume_node, args.repair_package)
    baseline = verify_recovery(nodes, data, boots, repair)
    state_path = Path(args.resume_state)
    resume_state(state_path, args.resume_node, data, boots)
    directory = Path(tempfile.mkdtemp(prefix='proxmox-operator-update-'))
    directory.chmod(0o700)
    print('Operator diagnostics: '+str(directory), flush=True)
    print('Acknowledged storage stalls: APT may finish while the protected node monitor is slow. '
          'Full cluster health and clean package state are required between nodes. No host reboot.', flush=True)
    selected.remove(args.resume_node)
    selected.insert(0, args.resume_node)
    for node in selected:
        print('OPERATOR TARGET='+node, flush=True)
        if node == args.resume_node:
            helper = Maintenance(list(nodes), node, state_path, baseline)
            rebase_owned(helper, data, baseline)
        else:
            data, baseline = recovered(nodes, boots, directory, node+'-before')
            verify_selection(list(nodes), [node], data)
            helper = Maintenance(list(nodes), node, directory/(node+'.maintenance.json'), baseline)
            helper.prepare()
        verify_target(node, helper.read() if node != args.resume_node else data)
        print('OPERATOR PROTECTED='+node, flush=True)
        package(node, directory, 'Refresh APT metadata', APT+'-o APT::Update::Error-Mode=any update', 1800)
        if node == args.resume_node:
            package(node, directory, 'Recover interrupted dpkg unpack',
                    archive_recovery_command(args.repair_package))
            fix = '--fix-broken --no-remove install'
            plan_packages(package(node, directory, 'Simulate package recovery', APT+'-s '+fix, 1800), complete=False)
            package(node, directory, 'Recover interrupted package', APT+'-y '+fix)
            data, baseline = recovered(nodes, boots, directory, node+'-repaired')
            rebase_owned(helper, data, baseline)
        plan = plan_packages(package(node, directory, 'Simulate full upgrade', APT+'-s dist-upgrade', 1800))
        (directory/(node+'.plan.json')).write_text(json.dumps(plan)+'\n')
        print('['+node+'] Pending packages: '+str(len(plan))+': '+', '.join(plan), flush=True)
        if plan:
            package(node, directory, 'Install all pending updates', APT+'-y --no-remove dist-upgrade')
        pending = plan_packages(package(node, directory, 'Verify no pending updates', APT+'-s dist-upgrade', 1800))
        if pending:
            raise ValueError('Packages still pending on '+node+': '+', '.join(pending))
        data, baseline = recovered(nodes, boots, directory, node+'-updated')
        rebase_owned(helper, data, baseline)
        print('OPERATOR RESTORING='+node, flush=True)
        helper.restore()
        data, baseline = recovered(nodes, boots, directory, node+'-restored')
        print('OPERATOR RESTORED='+node, flush=True)
    print('All selected nodes updated; audits clean, full Ceph health verified, owned HA/CRM settings restored. '
          'Installed kernels may require a separate rolling reboot.', flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--acknowledge-storage-stalls', action='store_true', required=True)
    parser.add_argument('--resume-node', required=True)
    parser.add_argument('--resume-state', required=True)
    parser.add_argument('--repair-package', action='append', required=True,
                        help='Repeat for reviewed packages in the interrupted update plan')
    parser.add_argument('--nodes', help='Remaining comma-separated rollout nodes')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if any(not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+',p) for p in args.repair_package):
        raise ValueError('Invalid recovery package name')
    os.umask(0o077)
    if args.worker:
        return worker(args)
    nodes = run.inventory(os.environ['PVE_NODES_JSON'])
    if args.resume_node not in nodes:
        raise ValueError('Resume node outside inventory')
    private_key = os.environ.pop('PVE_SSH_PRIVATE_KEY', '')
    with open('/tmp/proxmox-cluster-maintenance.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='pve-operator-ssh-') as tmp:
            env = run.prepare_ssh(Path(tmp), nodes, private_key, os.environ['PVE_KNOWN_HOSTS'])
            del private_key
            return run.run_script([sys.executable, __file__, '--worker', *sys.argv[1:]], env, lock.fileno())


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (MaintenanceInterrupted, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print('Operator update stopped: '+str(error)+'. Preserve retained maintenance state.', file=sys.stderr)
        sys.exit(1)
