"""Report bridge for verifier sessions whose filesystems are mounted read-only."""

import ctypes
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from uuid import uuid4

from .protocol import ContractError


ADDRESS = 'TEAM_RUNNER_CASCADE_REPORT_SOCKET'
MAX_MESSAGE = 256 * 1024
ASSIGNMENT_WAIT = 95


def _read(stream):
    with stream.makefile('rb') as body:
        raw = body.readline(MAX_MESSAGE + 1)
    if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
        raise ValueError('invalid cascade report frame')
    return json.loads(raw)


def _write(stream, value):
    raw = json.dumps(value, ensure_ascii=False).encode() + b'\n'
    if len(raw) > MAX_MESSAGE:
        raise ValueError('cascade report is too large')
    stream.sendall(raw)


def submit(execution, feature, dispatch, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(165)
        address = os.environ[ADDRESS]
        stream.connect(address if address.startswith('/') else '\0' + address)
        _write(stream, {'execution': execution, 'feature': feature, 'dispatch': dispatch, 'payload': payload})
        response = _read(stream)
    if 'error' in response:
        raise ContractError('cascade_report_failed', response['error'])
    return response['result']


def _peer_is_child(stream, ancestor):
    credentials = stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    peer, uid, _ = struct.unpack('3i', credentials)
    if uid != os.geteuid() or not ancestor:
        return False
    for _ in range(128):
        if peer == ancestor:
            return True
        if peer < 2:
            return False
        try:
            stat = Path(f'/proc/{peer}/stat').read_text()
            peer = int(stat.rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def _wait_for_assignment(session, feature, role, terminal):
    deadline = time.monotonic() + ASSIGNMENT_WAIT
    while time.monotonic() < deadline:
        state = session.read()
        assignment = state['features'].get(feature, {}).get('roles', {}).get(role, {})
        if (state['status'] != 'active' or assignment.get('dispatch')
                or assignment.get('terminal') != terminal):
            return
        time.sleep(.2)


def _handle(stream, ancestor, execution, feature, *, container=False):
    from .cascade import _report
    from .cascade_orca import OrcaBackend
    from .cascade_store import Session
    from .catalog import Catalog

    if not _peer_is_child(stream, ancestor):
        raise ValueError('report caller is outside this verifier process')
    request = _read(stream)
    if (not isinstance(request, dict) or request.get('execution') != execution
            or request.get('feature') != feature or not isinstance(request.get('dispatch'), str)):
        raise ValueError('report does not belong to this verifier session')
    session = Session.open(Catalog(), execution, OrcaBackend())
    if container:
        # A fast verifier may report while the coordinator is still receiving
        # worker-start. Wait for its exact published assignment without taking
        # the effect lock or asking Orca to deliver the prompt again.
        _wait_for_assignment(session, feature, os.environ['TEAM_RUNNER_CASCADE_ROLE'],
                             os.environ.get('ORCA_TERMINAL_HANDLE'))
    return _report(session, feature, request['dispatch'], request['payload'])


def _serve(address, argv, environment, execution, feature, container_pid=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(address)
        listener.listen(2)
        listener.settimeout(.25)
        child = subprocess.Popen(argv, env=environment, start_new_session=container_pid is not None)
        try:
            while child.poll() is None:
                try:
                    stream, _ = listener.accept()
                except TimeoutError:
                    continue
                with stream:
                    stream.settimeout(4)
                    try:
                        ancestor = container_pid() if container_pid else child.pid
                        result = {'result': _handle(stream, ancestor, execution, feature,
                                                    container=container_pid is not None)}
                    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
                        result = {'error': str(exc)}
                    try:
                        _write(stream, result)
                    except (OSError, ValueError):
                        # A caller that disconnected may resubmit the exact same report.
                        continue
            return child.returncode
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def _container(execution, feature, spec):
    from . import cascade_container

    guard = spec['guard']
    bridge = Path(guard['bridge'])
    provider = spec['provider']
    if spec.get('interactive', True):
        # Orca 1.4.265 derives the agent kind from the foreground process.
        # Keep the launcher in that group and the Docker client outside it.
        if ctypes.CDLL(None, use_errno=True).prctl(15, b'codex-container', 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'cannot name the container Codex launcher')
    bridge.mkdir(mode=0o700)
    try:
        (bridge / 'config.toml').write_text(provider['config'], encoding='utf-8')
        environment = {provider['credential']: os.environ[provider['credential']],
                       'TEAM_RUNNER_CASCADE_EXECUTION': execution,
                       'TEAM_RUNNER_CASCADE_FEATURE': feature}
        # The broker still retains its original Orca pane identity. Docker receives
        # only the credential required by the selected model provider.
        argv = cascade_container.docker_argv(guard, bridge, spec['codex_argv'],
                                             credential=provider['credential'],
                                             interactive=spec.get('interactive', True))
        with cascade_container.provider_proxy(bridge / 'provider.sock', provider['port']):
            return _serve(str(bridge / 'report.sock'), argv, environment, execution, feature,
                          lambda: cascade_container.container_pid(guard))
    finally:
        try:
            cascade_container.reap(guard)
        finally:
            shutil.rmtree(bridge)


def _termination(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    execution, feature, separator, *argv = sys.argv[1:]
    if separator == '--container' and not sys.executable:
        sys.executable = os.readlink('/proc/self/exe')
    for signum in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(signum, _termination)
    if separator == '--container' and len(argv) == 1:
        return _container(execution, feature, json.loads(argv[0]))
    if separator != '--' or not argv:
        raise ValueError('verifier broker requires a protected command')
    name = 'team-runner-cascade-' + uuid4().hex
    return _serve('\0' + name, argv, {**os.environ, ADDRESS: name}, execution, feature)


if __name__ == '__main__':
    raise SystemExit(main())
