#!/usr/bin/env python3
"""Run package writes in a leased systemd unit and prove remote termination."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import subprocess
import sys
import time
import uuid

LEASE_SECONDS = 15
VERIFY_SECONDS = 120


def unit_name(token):
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        raise ValueError('Invalid remote operation identity')
    return 'pve-update-'+token+'.service'


def group_pids(group):
    return {int(pid) for path in group.rglob('cgroup.procs')
            for pid in path.read_text().split()}


def own_group(token):
    rows = Path('/proc/self/cgroup').read_text().splitlines()
    paths = [row[3:] for row in rows if row.startswith('0::')]
    if len(paths) != 1 or not paths[0].endswith('/'+unit_name(token)):
        raise ValueError('Package supervisor is outside its owned cgroup')
    group = Path('/sys/fs/cgroup') / paths[0].lstrip('/')
    if os.getpid() not in group_pids(group):
        raise ValueError('Cannot verify package supervisor membership')
    return group


class Lease:
    def __init__(self, fd, token, clock=time.monotonic):
        self.fd, self.token, self.clock = fd, token, clock
        self.last = clock()
        self.buffer = b''
        self.seen = False

    def poll(self, wait=0.5):
        if select.select([self.fd], [], [], wait)[0]:
            chunk = os.read(self.fd, 4096)
            if not chunk:
                return False
            self.buffer += chunk
            if len(self.buffer) > 8192:
                return False
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if line != ('H '+self.token).encode():
                    return False
                self.last, self.seen = self.clock(), True
        return self.clock()-self.last < LEASE_SECONDS


def agent(token, timeout, command):
    # PID 1 owns the entire cgroup. Exiting this supervisor, including on EOF,
    # makes systemd stop ALL descendants, even dpkg's separate process session.
    # No SIGKILL is used: blocked I/O may keep the unit deactivating, which the
    # controller must report as unverified rather than calling it stopped.
    group = own_group(token)
    cancelled = []
    def stop(signum, frame):
        cancelled.append(signum)
    for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
        signal.signal(sig, stop)
    lease = Lease(sys.stdin.fileno(), token)
    while not lease.seen:
        if cancelled or not lease.poll():
            return 130
    # Drain a queued EOF before allowing any package operation to start.
    if cancelled or not lease.poll(0):
        return 130
    child = subprocess.Popen(['bash', '-lc', command], stdin=subprocess.DEVNULL,
                             start_new_session=True)
    deadline = time.monotonic()+timeout
    while True:
        if cancelled or not lease.poll():
            return 130
        if time.monotonic() >= deadline:
            return 124
        result = child.poll()
        if result is not None and group_pids(group) <= {os.getpid()}:
            return result if result >= 0 else 128-result


def inspect_unit(token, stop=False):
    unit = unit_name(token)
    result = subprocess.run(['systemctl', 'show', unit,
        '--property=LoadState,ActiveState,SubState,Result,ExecMainStatus,MainPID,ControlGroup,Description'],
        capture_output=True, text=True, timeout=15)
    props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if props.get('LoadState') == 'not-found':
        pids = sorted(group_pids(Path('/sys/fs/cgroup/system.slice')/unit))
        return {'unit':unit, 'properties':props, 'pids':pids, 'quiescent':not pids}
    if result.returncode or props.get('Description') != 'Proxmox updater '+token:
        raise ValueError('Cannot verify remote unit ownership: '+unit)
    group = props.get('ControlGroup')
    if group and not group.endswith('/'+unit):
        raise ValueError('Unexpected package control group')
    if stop:
        subprocess.run(['systemctl', 'stop', '--no-block', unit],
                       check=True, capture_output=True, text=True, timeout=15)
        return inspect_unit(token)
    pids = sorted(group_pids(Path('/sys/fs/cgroup')/group.lstrip('/'))) if group else []
    quiet = not pids and props.get('MainPID') == '0' and (
        props.get('ActiveState') in ['inactive', 'failed'] or props.get('SubState') == 'exited')
    return {'unit':unit, 'properties':props, 'pids':pids, 'quiescent':quiet}


def encoded_command(source, args):
    code = 'import base64;exec(base64.b64decode('+repr(base64.b64encode(source.encode()).decode())+'))'
    return shlex.join(['python3', '-c', code, *args])


def service_command(source, token, timeout, command):
    encoded = base64.b64encode(command.encode()).decode()
    code = 'import base64;exec(base64.b64decode('+repr(base64.b64encode(source.encode()).decode())+'))'
    return shlex.join(['systemd-run', '--quiet', '--pipe', '--wait', '--service-type=exec',
        '--expand-environment=no', '--unit='+unit_name(token),
        '--description=Proxmox updater '+token,
        '--property=KillMode=control-group', '--property=KillSignal=SIGTERM',
        '--property=SendSIGKILL=no', '--property=TimeoutStopSec=infinity',
        '--property=RuntimeMaxSec='+str(timeout+LEASE_SECONDS),
        '--', 'python3', '-c', code, '--agent', token, str(timeout), encoded])


def ssh_args(node, command):
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
            'root@'+node, command]


def probe(node, source, token, stop=False):
    command = encoded_command(source, ['--inspect', token, *(['--stop'] if stop else [])])
    result = subprocess.run(ssh_args(node, command), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=35)
    if result.returncode:
        raise ValueError('Remote stop/status unavailable: '+result.stderr.strip())
    data = json.loads(result.stdout)
    if (data.get('unit') != unit_name(token) or not isinstance(data.get('pids'), list)
            or type(data.get('quiescent')) is not bool):
        raise ValueError('Invalid remote termination evidence')
    return data


def verify_end(node, source, token, transport, cancel, *, clock=time.monotonic, sleep=time.sleep):
    deadline = clock()+VERIFY_SECONDS
    last = {'quiescent':False, 'error':'No remote termination evidence'}
    while True:
        try:
            last = probe(node, source, token, stop=cancel)
            # A not-yet-created unit is not proof while the start request lives.
            if transport.poll() is not None and last['quiescent'] and not last['pids']:
                return last
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            last = {'quiescent':False, 'error':str(error)}
        if clock() >= deadline:
            return dict(last, quiescent=False, error='Remote termination not verified; inspect owned unit')
        sleep(1)


def controller(node, timeout, directory, command):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', node) or timeout < 1:
        raise ValueError('Invalid remote package target or timeout')
    source = Path(__file__).read_text()
    token = uuid.uuid4().hex
    path = Path(directory)/(node+'.apt.'+token+'.json')
    state = {'node':node, 'unit':unit_name(token), 'phase':'planned', 'timeout':timeout,
             'quiescent':False}
    def record(phase, **data):
        state.update(phase=phase, **data)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, indent=2)+'\n'); tmp.chmod(0o600); tmp.replace(path)
    record('start_requested')
    interrupted = []

    def stop(signum, frame):
        interrupted.append(signum)
    previous = {sig:signal.signal(sig, stop) for sig in [signal.SIGTERM,signal.SIGINT,signal.SIGHUP]}
    parent = os.getppid()
    transport = None
    cancelled = False
    try:
        if interrupted:
            record('quiescent', quiescent=True, never_started=True)
            return 130
        transport = subprocess.Popen(ssh_args(node, service_command(source,token,timeout,command)),
            stdin=subprocess.PIPE, start_new_session=True, bufsize=0)
        record('active', transport_pid=transport.pid)
        os.set_blocking(transport.stdin.fileno(), False)
        deadline = time.monotonic()+timeout+LEASE_SECONDS+30
        while transport.poll() is None:
            if interrupted or os.getppid() != parent or time.monotonic() >= deadline:
                cancelled = True
                break
            try:
                os.write(transport.stdin.fileno(), ('H '+token+'\n').encode())
            except BrokenPipeError:
                # A normally finishing SSH may close stdin just before waitpid
                # observes exit. Verify its result before classifying this race.
                break
            except BlockingIOError:
                cancelled = True
                break
            time.sleep(.5)
        transport.stdin.close()  # EOF revokes the lease even if the helper dies.
        cancel = cancelled or transport.poll() not in [None, 0]
        record('cancelling' if cancel else 'verifying', transport_status=transport.poll())
        evidence = verify_end(node, source, token, transport, cancel)
        record('quiescent' if evidence['quiescent'] else 'unverified',
               transport_status=transport.poll(), cancelled=cancel or bool(interrupted), **evidence)
        if not evidence['quiescent']:
            print('Remote package work NOT verified stopped: '+node+' '+state['unit']+
                  '; preserve maintenance and inspect '+str(path), file=sys.stderr, flush=True)
            return 1
        result = transport.poll()
        if cancelled or interrupted:
            return 130
        return result if result is not None and result >= 0 else 1
    finally:
        if transport is not None:
            if transport.stdin and not transport.stdin.closed:
                transport.stdin.close()
            # The owned remote unit/lease, not SSH process lifetime, proves stop.
            if transport.poll() is None:
                transport.terminate()
                try:transport.wait(timeout=5)
                except subprocess.TimeoutExpired:pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    os.umask(0o077)
    if sys.argv[1:2] == ['--agent']:
        return agent(sys.argv[2], int(sys.argv[3]), base64.b64decode(sys.argv[4]).decode())
    if sys.argv[1:2] == ['--inspect']:
        print(json.dumps(inspect_unit(sys.argv[2], stop='--stop' in sys.argv[3:])))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True)
    parser.add_argument('--timeout', type=int, required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('command', nargs=1)
    args = parser.parse_args()
    return controller(args.node,args.timeout,args.directory,args.command[0])


if __name__ == '__main__':
    try:sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('Remote package operation stopped: '+str(error),file=sys.stderr)
        sys.exit(1)
