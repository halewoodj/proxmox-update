#!/usr/bin/env python3
"""Run the repository's cluster maintenance script from Semaphore."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

PROGRAM = 'update.sh'
ROOT = Path(__file__).resolve().parents[1]


def inventory(raw):
    nodes = json.loads(raw)
    if not isinstance(nodes, dict) or len(nodes) < 2:
        raise ValueError('PVE_NODES_JSON must map at least two node names to IP addresses')
    for name, address in nodes.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
            raise ValueError('Invalid inventory node name')
        ipaddress.IPv4Address(address)
    if len(set(nodes.values())) != len(nodes):
        raise ValueError('Each inventory node must have its own address')
    return nodes


def selected_nodes(nodes, selection):
    selected = selection.split(',') if selection else list(nodes)
    if not selected or len(set(selected)) != len(selected) or any(n not in nodes for n in selected):
        raise ValueError('Selected nodes must be distinct members of PVE_NODES_JSON')
    return selected


def prepare_ssh(directory, nodes, private_key, known_hosts):
    if not private_key.strip() or not known_hosts.strip():
        raise ValueError('PVE_SSH_PRIVATE_KEY and PVE_KNOWN_HOSTS are required')
    binary = shutil.which('ssh')
    if not binary:
        raise ValueError('OpenSSH is required on the Semaphore runner')
    key_path = directory / 'id_ed25519'
    key_path.write_text(private_key.strip() + '\n')
    key_path.chmod(0o600)
    hosts_path = directory / 'known_hosts'
    hosts_path.write_text(known_hosts.strip() + '\n')
    hosts_path.chmod(0o600)
    config_path = directory / 'ssh_config'
    config = []
    for name, address in nodes.items():
        config.extend([f'Host {name}', f'  HostName {address}', f'  HostKeyAlias {name}'])
    config.extend([
        'Host *', '  User root', f'  IdentityFile {key_path}',
        f'  UserKnownHostsFile {hosts_path}', '  GlobalKnownHostsFile /dev/null',
        '  StrictHostKeyChecking yes', '  HostKeyAlgorithms ssh-ed25519',
        '  BatchMode yes', '  IdentitiesOnly yes', '  IdentityAgent none',
        '  PasswordAuthentication no', '  KbdInteractiveAuthentication no',
        '  ForwardAgent no', '  ClearAllForwardings yes', '  RequestTTY no',
        '  ConnectTimeout 10', '  ConnectionAttempts 1', '  LogLevel ERROR',
        '  ServerAliveInterval 15', '  ServerAliveCountMax 4',
    ])
    config_path.write_text('\n'.join(config) + '\n')
    bin_dir = directory / 'bin'
    bin_dir.mkdir(mode=0o700)
    wrapper = bin_dir / 'ssh'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(binary) + ' -F ' + shlex.quote(str(config_path)) + ' "$@"\n')
    wrapper.chmod(0o700)
    env = os.environ.copy()
    env.pop('PVE_SSH_PRIVATE_KEY', None)
    env['PATH'] = str(bin_dir) + os.pathsep + env.get('PATH', '')
    env['TERM'] = 'dumb'
    return env


def read_remote(node, command, env):
    result = subprocess.run(['ssh', 'root@' + node, command], env=env,
                            text=True, stdout=subprocess.PIPE, timeout=150, check=True)
    return result.stdout.strip()


def preflight(nodes, control, env):
    for node in nodes:
        if read_remote(node, 'hostname -s', env) != node:
            raise ValueError('SSH hostname does not match inventory: ' + node)
        print('SSH verified: ' + node, flush=True)
    if PROGRAM=='update.sh':
        # Also works when a test/embedding loader invokes this file via runpy.
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from update_guard import read_verified, collect_snapshots
        read_verified(list(nodes),collect=lambda:collect_snapshots(list(nodes),env=env))
        print('Approved cluster membership, quorum and Ceph HEALTH_OK verified.',flush=True)
        return
    members = read_remote(control, 'pvecm nodes', env)
    detected = [line.split()[2] for line in members.splitlines()
                if len(line.split()) >= 3 and line.split()[0].isdigit()]
    if len(detected) != len(nodes) or set(detected) != set(nodes):
        raise ValueError('Cluster membership differs from the approved inventory; refusing maintenance')
    quorum = read_remote(control, 'pvecm status', env)
    if not re.search(r'^\s*Quorate:\s*Yes\s*$', quorum, re.MULTILINE):
        raise ValueError('Cluster does not have quorum; refusing maintenance')
    health = read_remote(control, 'ceph health', env)
    if health.split(maxsplit=1)[0:1] != ['HEALTH_OK']:
        raise ValueError('Ceph must be HEALTH_OK before maintenance: ' + health)
    print('Approved cluster membership, quorum and Ceph HEALTH_OK verified.', flush=True)


def script_args(mode, control, selected):
    args = ['bash', str(ROOT / PROGRAM), '--cluster-host', 'root@' + control,
            '--nodes', ','.join(selected)]
    if PROGRAM == 'update.sh':
        args.extend(['--jobs', '1', '--auto-maintenance'])
    else:
        args.extend(['--direct-node-ssh', '--yes'])
    if mode == 'preview':
        args.append('--dry-run')
    return args


def run_script(argv,env,lock_fd):
    process=None
    interrupted=[]
    def stop(signum,frame):
        if not interrupted:interrupted.append(time.monotonic())
        if process is not None:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
    previous={sig:signal.signal(sig,stop) for sig in [signal.SIGINT,signal.SIGTERM,signal.SIGHUP]}
    try:
        # Semaphore signals the runner PID. A separate group lets the runner
        # forward that cancellation to Bash workers, helpers and SSH children.
        # Keep the maintenance lock in the worker too: killing only the runner
        # must not permit a second job while the original worker is still alive.
        guarded=[sys.executable,str(ROOT/'semaphore/parent_guard.py'),str(os.getpid()),*argv]
        process=subprocess.Popen(guarded,env=env,start_new_session=True,pass_fds=(lock_fd,))
        if interrupted:stop(signal.SIGTERM,None)
        while True:
            try:
                result=process.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                # Package I/O may be uninterruptible. Keep the lock and SSH
                # material while helpers verify the owned remote unit; killing
                # them after an arbitrary grace period would erase that proof.
                continue
        if interrupted:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            print('Runner cancelled; no further nodes will start. Inspect retained state and remote APT before retrying.',file=sys.stderr)
            return 130
        return result
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['preview', 'apply'])
    parser.add_argument('--nodes', help='Optional comma-separated subset of the configured inventory')
    args = parser.parse_args()
    os.umask(0o077)
    nodes = inventory(os.environ['PVE_NODES_JSON'])
    selected = selected_nodes(nodes, args.nodes)
    control = os.environ.get('PVE_CLUSTER_HOST', next(iter(nodes)))
    if control not in nodes:
        raise ValueError('PVE_CLUSTER_HOST must be an inventory node')
    private_key = os.environ.pop('PVE_SSH_PRIVATE_KEY', '')
    known_hosts = os.environ['PVE_KNOWN_HOSTS']
    # Both repositories use the same lock on the same Semaphore runner.
    with open('/tmp/proxmox-cluster-maintenance.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another Proxmox maintenance task is already running') from None
        with tempfile.TemporaryDirectory(prefix='pve-maintenance-') as tmp:
            env = prepare_ssh(Path(tmp), nodes, private_key, known_hosts)
            del private_key
            preflight(nodes, control, env)
            print(f'Mode: {args.mode}; selected nodes: {", ".join(selected)}', flush=True)
            if PROGRAM == 'update.sh' and args.mode == 'preview':
                print('Preview simulates upgrades using existing APT lists; metadata is not refreshed and may be stale.', flush=True)
                print('Automatic HA maintenance is planned only; preview does not migrate guests or change services.', flush=True)
            elif PROGRAM == 'update.sh':
                print('Apply prepares one HA node at a time, then restores it after verification. Container migration can restart containers.', flush=True)
            return run_script(script_args(args.mode, control, selected),env,lock.fileno())


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print('Maintenance stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
