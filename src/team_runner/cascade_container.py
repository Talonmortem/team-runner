"""Local Docker confinement for Orca verifier sessions."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import socket
import socketserver
import subprocess
import sys
from threading import Thread

from . import cascade_verification, verification


DOCKER_SOCKET = '/var/run/docker.sock'
BRIDGE = Path('/run/team-runner-cascade')
PACKAGE = Path('/opt/team-runner-src')
CODEX = '/usr/local/bin/codex'
PYTHON = '/usr/bin/python3'
PROVIDER_PORT = 2455
_IMAGE = re.compile(r'sha256:[0-9a-f]{64}\Z')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z')
_VARIABLE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_PASSTHROUGH = ('TEAM_RUNNER_CASCADE_EXECUTION', 'TEAM_RUNNER_CASCADE_FEATURE')


def docker(*args):
    binary = shutil.which('docker', path='/usr/bin:/bin')
    if sys.platform != 'linux' or not binary or not Path(DOCKER_SOCKET).is_socket():
        raise ValueError('cascade verifier requires local Linux Docker on ' + DOCKER_SOCKET)
    return ['/usr/bin/env', '-u', 'DOCKER_HOST', '-u', 'DOCKER_CONTEXT',
            binary, '--host', 'unix://' + DOCKER_SOCKET, *args]


def _inspect(kind, name):
    try:
        result = subprocess.run(docker(kind, 'inspect', '--', name),
                                capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f'cascade verifier Docker {kind} is unavailable: {name}') from exc
    data = json.loads(result.stdout)
    if len(data) != 1:
        raise ValueError(f'ambiguous Docker {kind}: {name}')
    return data[0]


def resolve_image(image):
    """Resolve an existing image and reject anonymous writable image volumes."""
    if not isinstance(image, str) or not image or image.startswith('-') or any(c.isspace() for c in image):
        raise ValueError('cascade verifier requires a local Docker image')
    data = _inspect('image', image)
    if (data.get('Os') != 'linux' or not _IMAGE.fullmatch(data.get('Id', ''))
            or (data.get('Config') or {}).get('Volumes')):
        raise ValueError('cascade verifier image must be Linux with an immutable ID and no volumes')
    return data['Id']


def image_version(image):
    result = subprocess.run(docker(
        'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--user', f'{os.geteuid()}:{os.getegid()}', '--entrypoint', CODEX, image, '--version',
    ), capture_output=True, check=True, text=True, timeout=15)
    return result.stdout.strip()


def _overlap(first, second):
    return first.is_relative_to(second) or second.is_relative_to(first)


def guard(candidate, artifacts, image, name, *, check_links=True):
    protected, writable = cascade_verification.mount_paths(candidate, [artifacts], check_links=check_links)
    if not _IMAGE.fullmatch(image) or not _NAME.fullmatch(name):
        raise ValueError('invalid cascade verifier container identity')
    if os.geteuid() == 0 or os.getegid() == 0:
        raise ValueError('cascade verifier requires a non-root local user and group')
    reserved = (BRIDGE, PACKAGE, Path('/etc'), Path('/usr'), Path('/bin'), Path('/lib'),
                Path('/proc'), Path('/sys'), Path('/dev'))
    for path in (*protected, *writable):
        path = Path(path)
        if (any(_overlap(path, root) for root in reserved) or path in (Path('/tmp'), Path('/run'))
                or path.is_relative_to(Path('/tmp') / name)):
            raise ValueError('cascade input overlaps a reserved container path: ' + str(path))
    return {'kind': 'docker', 'image': image, 'name': name, 'bridge': str(Path('/tmp') / name),
            'candidate': candidate['digest'], 'checkout': candidate['checkout'],
            'extra': verification.digest(candidate['extra']), 'authority': str(Path(candidate['manifest']).parent),
            'protected': protected, 'writable_roots': writable, 'uid': os.geteuid(), 'gid': os.getegid()}


def _mounts(config, bridge):
    inputs = list(dict.fromkeys(config['protected']))
    # Parent binds already make nested input trees read-only, including submounts.
    mounts = [(p, p, False) for p in inputs
              if not any(other != p and Path(p).is_relative_to(other) for other in inputs)]
    mounts += [(path, path, True) for path in config['writable_roots']]
    mounts += [(str(Path(__file__).resolve().parents[1]), str(PACKAGE), False),
               (str(bridge), str(BRIDGE), False)]
    return mounts


def _bind(src, dest, writable):
    if any(char in src or char in dest for char in (',', '"', '\n', '\r')):
        raise ValueError('unsupported Docker bind path')
    mode = 'bind-recursive=disabled' if writable else 'readonly,bind-recursive=readonly'
    return f'type=bind,source={src},target={dest},bind-propagation=rprivate,{mode}'


def docker_argv(config, bridge, argv, *, credential=None, interactive=True):
    """Use only the declared binds and an environment independent of the host."""
    if not argv or argv[0] != CODEX or str(bridge) != config['bridge']:
        raise ValueError('invalid cascade verifier Docker launch')
    if credential and (not _VARIABLE.fullmatch(credential) or credential in {
        'PATH', 'HOME', 'PYTHONPATH', 'CODEX_HOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH',
    } or credential.startswith(('DOCKER_', 'ORCA_', 'TEAM_RUNNER_', 'PYTHON', 'GIT_'))):
        raise ValueError('invalid verifier provider credential name')
    args = docker('run', '--rm', '--pull', 'never', '--init', '--name', config['name'],
                  '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                  '--security-opt', 'no-new-privileges', '--user', f"{config['uid']}:{config['gid']}",
                  '--tmpfs', '/tmp:rw,nosuid,nodev,mode=1777', '--workdir', config['checkout'],
                  '--entrypoint', PYTHON, '--no-healthcheck')
    if interactive:
        args += ['-i', '-t']
    for src, dest, writable in _mounts(config, bridge):
        args += ['--mount', _bind(src, dest, writable)]
    artifacts, = config['writable_roots']
    home = Path(artifacts) / '.home'
    safe_env = {
        'HOME': str(home), 'CODEX_HOME': str(home / '.codex'),
        'CARGO_HOME': str(home / '.cargo'),
        'CARGO_TARGET_DIR': str(Path(artifacts) / 'cargo-target'),
        'PYTHONPATH': str(PACKAGE), 'PYTHONDONTWRITEBYTECODE': '1', 'TERM': 'xterm-256color',
        'TEAM_RUNNER_CASCADE_PROTECTED': json.dumps(config['protected'] + [str(BRIDGE), str(PACKAGE)]),
        'TEAM_RUNNER_ATTEMPT': config['authority'],
        'TEAM_RUNNER_CHECKOUT': config['checkout'],
        'TEAM_RUNNER_MATERIALS': str(Path(config['authority']) / 'materials'),
        'TEAM_RUNNER_CASCADE_ROLE': Path(artifacts).name, 'TEAM_RUNNER_CASCADE_ARTIFACTS': artifacts,
        'TEAM_RUNNER_CASCADE_REPORT_SOCKET': str(BRIDGE / 'report.sock'),
    }
    for key, value in safe_env.items():
        args += ['--env', f'{key}={value}']
    for key in (*_PASSTHROUGH, *([credential] if credential else [])):
        args += ['--env', key]
    return [*args, config['image'], '-m', __name__, 'container', *argv]


def _runtime(config, *, running=True):
    data = _inspect('container', config['name'])
    status = data.get('State', {})
    host = data.get('HostConfig', {})
    container = data.get('Config', {})
    expected = set(_mounts(config, config['bridge']))
    actual = {(m.get('Source'), m.get('Destination'), m.get('RW'))
              for m in data.get('Mounts', []) if m.get('Type') == 'bind'}
    if (data.get('Name') != '/' + config['name'] or data.get('Image') != config['image']
            or (running and (status.get('Running') is not True or status.get('Pid', 0) <= 1))
            or host.get('NetworkMode') != 'none' or host.get('ReadonlyRootfs') is not True
            or host.get('Privileged') is not False or host.get('CapAdd')
            or host.get('CapDrop') != ['ALL'] or host.get('SecurityOpt') != ['no-new-privileges']
            or host.get('PidMode') or host.get('IpcMode') == 'host' or host.get('Devices')
            or container.get('User') != f"{config['uid']}:{config['gid']}"
            or container.get('WorkingDir') != config['checkout']
            or container.get('Entrypoint') != [PYTHON]
            or actual != expected or len(actual) != len(data.get('Mounts', []))):
        raise ValueError('cascade verifier Docker isolation is unconfirmed')
    return data


def container_pid(config):
    """Bind request identity to the current exact verifier container init PID."""
    return _runtime(config)['State']['Pid']


def absent(config):
    result = subprocess.run(docker(
        'container', 'ls', '--all', '--filter', 'name=^/' + config['name'] + '$', '--format', '{{.Names}}',
    ), capture_output=True, check=True, text=True, timeout=10)
    return not result.stdout.strip()


def reap(config):
    """Remove only the frozen verifier container, with a repeatable absent receipt."""
    if not absent(config):
        try:
            container = _runtime(config, running=False)['Id']
            subprocess.run(docker('container', 'rm', '--force', '--', container),
                           capture_output=True, check=True, timeout=15)
        except (ValueError, OSError, subprocess.SubprocessError):
            if not absent(config):
                raise
    if not absent(config):
        raise ValueError('verifier container exit is unconfirmed')
    return {'name': config['name'], 'absent': True}


_ATTEST = """
import json
import os
from pathlib import Path
import sys

protected, writable, checkout, argv = json.loads(sys.argv[1])
for path in ('/', *protected):
    if not os.statvfs(path).f_flag & os.ST_RDONLY:
        raise SystemExit('expected read-only mount')
for path in writable:
    if os.statvfs(path).f_flag & os.ST_RDONLY:
        raise SystemExit('expected writable artifact mount')
for process in Path('/proc').iterdir():
    if not process.name.isdecimal():
        continue
    try:
        args = (process / 'cmdline').read_bytes().rstrip(b'\\0').split(b'\\0')
        if (args == list(map(os.fsencode, argv)) and os.readlink(process / 'exe') == argv[0]
                and os.readlink(process / 'cwd') == checkout):
            status = dict(line.split(':', 1) for line in (process / 'status').read_text().splitlines())
            if (int(status.get('CapEff', '1'), 16) == 0 and status.get('NoNewPrivs', '').strip() == '1'
                    and os.readlink(process / 'ns/mnt') == os.readlink('/proc/self/ns/mnt')):
                raise SystemExit(0)
    except OSError:
        continue
raise SystemExit('verifier Codex process unavailable')
"""


def ready(_terminal_pid, launch):
    """Attest both Docker configuration and the actual running Codex process."""
    config = launch['guard']
    argv = launch['codex_argv']
    try:
        data = _runtime(config)
        if data.get('Args') != ['-m', __name__, 'container', *argv]:
            return False
        probe = json.dumps([config['protected'] + [str(PACKAGE), str(BRIDGE)],
                            config['writable_roots'], config['checkout'], argv])
        subprocess.run(docker('exec', data['Id'], PYTHON, '-I', '-c', _ATTEST, probe),
                       check=True, capture_output=True, timeout=10)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return False
    return True


def _relay(incoming, outgoing):
    """Bidirectional stream forwarding, including half-close and streamed replies."""
    with selectors.DefaultSelector() as selector:
        selector.register(incoming, selectors.EVENT_READ, outgoing)
        selector.register(outgoing, selectors.EVENT_READ, incoming)
        while selector.get_map():
            for key, _ in selector.select():
                data = key.fileobj.recv(65536)
                if data:
                    key.data.sendall(data)
                else:
                    selector.unregister(key.fileobj)
                    key.data.shutdown(socket.SHUT_WR)


class _Proxy:
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, connect):
        self.connect = connect
        super().__init__(address, _Forward)


class _UnixProxy(_Proxy, socketserver.ThreadingUnixStreamServer):
    pass


class _LoopbackProxy(_Proxy, socketserver.ThreadingTCPServer):
    pass


class _Forward(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with self.server.connect() as peer:
                _relay(self.request, peer)
        except OSError:
            return


@contextmanager
def provider_proxy(path, port):
    """Relay a private Unix socket to just this one loopback provider port."""
    path = Path(path)
    if type(port) is not int or not 1 <= port <= 65535 or path.parent.stat().st_mode & 0o077:
        raise ValueError('invalid cascade verifier provider bridge')
    with _UnixProxy(str(path), lambda: socket.create_connection(('127.0.0.1', port))) as proxy:
        path.chmod(0o600)
        thread = Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            proxy.shutdown()
            thread.join()
            path.unlink()


def _provider_socket():
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(BRIDGE / 'provider.sock'))
    except OSError:
        sock.close()
        raise
    return sock


def _container_entry(argv):
    # These checks run before the model can invoke any tool in this container.
    for path in ('/', *json.loads(os.environ['TEAM_RUNNER_CASCADE_PROTECTED'])):
        if not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise ValueError('verifier input mount is writable: ' + str(path))
    home = Path(os.environ['CODEX_HOME'])
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg = home / 'config.toml'
    cfg.write_bytes((BRIDGE / cfg.name).read_bytes())
    with _LoopbackProxy(('127.0.0.1', PROVIDER_PORT), _provider_socket) as proxy:
        # The listener is bound before Codex begins. Docker tears down the relay
        # in the private PID namespace when the interactive Codex process exits.
        if os.fork() == 0:
            proxy.serve_forever()
            os._exit(0)
    os.execv(argv[0], argv)


if __name__ == '__main__':
    mode, *command = sys.argv[1:]
    if mode != 'container' or not command or command[0] != CODEX:
        raise ValueError('invalid container entrypoint')
    _container_entry(command)
