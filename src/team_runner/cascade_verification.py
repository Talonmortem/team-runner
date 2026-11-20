"""Immutable candidates and protected evidence for an Orca cascade."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

from . import runner_operations, verification
from .runner_store import atomic_json, read_json

REPORT_ROLES = ('coordinator', 'implementer', 'tester', 'reviewer')
CHECK_ROLES = ('tester', 'reviewer')
ASSIGNMENT_KEYS = ('execution', 'attempt', 'role', 'checkout', 'run', 'task', 'dispatch')
_HEX_GIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_SCOPE_BLOCK = re.compile(r'^\s*```cascade-verification[ \t]*\n(.*?)^\s*```[ \t]*$', re.M | re.S)
_ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z_0-9]*\Z')


def _check_scope(coverage):
    if not isinstance(coverage, dict) or set(coverage) != {'extra_inputs', 'checks'}:
        raise ValueError('cascade verification requires explicit extra_inputs and checks')
    extra = coverage['extra_inputs']
    if not isinstance(extra, list) or any(not isinstance(p, str) or not p.strip() or '\0' in p for p in extra):
        raise ValueError('extra_inputs must list paths')
    checks = coverage['checks']
    if not isinstance(checks, list) or not checks:
        raise ValueError('cascade requires at least one frozen verification command')
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'argv', 'cwd', 'toolchain', 'environment'}:
            raise ValueError('check requires argv, cwd, toolchain and environment')
        argv, cwd, toolchain, environment = (check[k] for k in ('argv', 'cwd', 'toolchain', 'environment'))
        if (not isinstance(argv, list) or not argv or
                any(not isinstance(v, str) or '\0' in v for v in argv) or not argv[0]):
            raise ValueError('check argv must contain an executable and text arguments')
        if not isinstance(cwd, str) or not cwd or '\0' in cwd or Path(cwd).is_absolute() or '..' in Path(cwd).parts:
            raise ValueError('check cwd must be relative to the checkout')
        if not isinstance(toolchain, str) or not toolchain.strip():
            raise ValueError('check requires an explicit toolchain')
        if (not isinstance(environment, dict) or
                any(not isinstance(k, str) or not _ENV_NAME.fullmatch(k) or not isinstance(v, str) or '\0' in v
                    for k, v in environment.items())):
            raise ValueError('check environment must contain explicitly named text variables')
    return coverage


def coverage_from_card(markdown):
    """Read the verification inputs declaration from the frozen item card."""
    blocks = _SCOPE_BLOCK.findall(markdown)
    if len(blocks) != 1:
        raise ValueError('frozen card requires one cascade-verification JSON block')
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('duplicate key in cascade verification declaration')
        return result
    return _check_scope(json.loads(blocks[0], object_pairs_hook=unique))


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _git(checkout, *args):
    return subprocess.run(['git', '-C', str(checkout), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True, timeout=30).stdout


def _clean_head(checkout):
    if Path(os.fsdecode(_git(checkout, 'rev-parse', '--show-toplevel').rstrip(b'\n'))).resolve() != checkout:
        raise ValueError('verification checkout differs from Git root')
    head = _git(checkout, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
    if _git(checkout, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignore-submodules=none'):
        raise ValueError('candidate requires a clean Git index and worktree')
    return head


def _ancestor(checkout, base_sha, head):
    if not isinstance(base_sha, str) or not _HEX_GIT.fullmatch(base_sha):
        raise ValueError('exact base commit SHA required')
    try:
        _git(checkout, 'merge-base', '--is-ancestor', base_sha, head)
    except subprocess.CalledProcessError as exc:
        raise ValueError('candidate does not contain the frozen base commit') from exc


def _overlap(a, b):
    return a.is_relative_to(b) or b.is_relative_to(a)


def _extra_entries(root):
    entries = {}
    pending = [root]
    while pending:
        path = pending.pop()
        name = str(path.relative_to(root))
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            entries[name] = {'kind': 'directory'}
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(mode):
            entries[name] = {'kind': 'file', 'sha256': _hash(path), 'executable': bool(mode & 0o111)}
        else:
            raise ValueError('extra inputs require regular files and real directories: ' + str(path))
    return entries


def _git_dirs(checkout):
    args = ('rev-parse', '--path-format=absolute', '--absolute-git-dir', '--git-common-dir')
    return sorted({str(Path(os.fsdecode(path)).resolve(strict=True))
                   for path in _git(checkout, *args).splitlines()})


def _extra_inventory(checkout, extra, tracked, git_dirs):
    roots = []
    for name in extra:
        path = Path(os.path.abspath(checkout / name))
        if path.resolve(strict=True) != path:
            raise ValueError('symlink in extra verification input: ' + str(path))
        if any(_overlap(path, other) for other in roots):
            raise ValueError('overlapping extra verification inputs: ' + str(path))
        if (_overlap(path, checkout / '.git') or path == checkout
                or any(_overlap(path, Path(p)) for p in git_dirs) or
                any(_overlap(path, checkout / name) for name in tracked)):
            raise ValueError('extra input overlaps protected Git inputs: ' + str(path))
        if any(_overlap(path, Path(root)) for root in ('/dev', '/proc', '/sys')):
            raise ValueError('extra input requires a filesystem that can be bound read-only: ' + str(path))
        roots.append(path)
    ignored = _git(checkout, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard')
    for name in ignored.split(b'\0'):
        if name and not any((checkout / os.fsdecode(name)).is_relative_to(root) for root in roots):
            raise ValueError('ignored checkout input has no frozen coverage: ' + os.fsdecode(name))
    return {str(path): _extra_entries(path) for path in roots}


def freeze_candidate(checkout, base_sha, directory, *, coverage):
    """Create the regular manifest and freeze explicit ignored/external input trees."""
    coverage = _check_scope(coverage)
    checkout = Path(checkout).resolve(strict=True)
    directory = Path(directory).resolve()
    if directory.is_relative_to(checkout):
        raise ValueError('verification authority directory must be outside the checkout')
    head = _clean_head(checkout)
    _ancestor(checkout, base_sha, head)
    for check in coverage['checks']:
        cwd = verification.inside(checkout, check['cwd']).resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError('verification cwd is not a directory')
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'candidate-manifest.json'
    with tempfile.TemporaryDirectory(prefix='candidate-', dir=directory) as tmp:
        temporary = Path(tmp) / manifest.name
        normal = verification.create(checkout, ['.'], temporary)
        frozen = read_json(temporary)
        git_dirs = _git_dirs(checkout)
        extra = _extra_inventory(checkout, coverage['extra_inputs'], frozen['files'], git_dirs)
        if any(_overlap(directory, Path(path)) for path in extra):
            raise ValueError('extra input overlaps verification authority')
        candidate = {'checkout': str(checkout), 'sha': head, 'base_sha': base_sha, 'git_dirs': git_dirs,
                     'roots': frozen['roots'], 'manifest': str(manifest),
                     'digest': normal['sha256'], 'extra': extra, 'checks': coverage['checks']}
        if manifest.exists():
            if _hash(manifest) != candidate['digest']:
                raise ValueError('frozen candidate manifest differs on resume')
        else:
            os.replace(temporary, manifest)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        preflight = verification.verify(checkout, manifest, candidate['digest'])
        candidate['check_inputs'] = [_command_inputs(checkout, preflight, check) for check in candidate['checks']]
        boundary(checkout, candidate)
        return candidate


def _command_inputs(checkout, preflight, check):
    cwd = verification.inside(checkout, check['cwd']).resolve(strict=True)
    argv = list(check['argv'])
    if '/' in argv[0] and not Path(argv[0]).is_absolute():
        argv[0] = str(cwd / argv[0])
    return verification.evidence_inputs(preflight, argv, check['toolchain'], check['environment'], True)


def boundary(checkout, candidate):
    """Fail if the Git SHA, index, checkout contents or supplied inputs have drifted."""
    checkout = Path(checkout).resolve(strict=True)
    if str(checkout) != candidate['checkout']:
        raise ValueError('candidate belongs to another checkout')
    head = _clean_head(checkout)
    if head != candidate['sha']:
        raise ValueError('candidate HEAD changed')
    manifest = verification.verify(checkout, candidate['manifest'], candidate['digest'])
    if manifest['result'] != 'PASS' or manifest.get('roots') != candidate['roots']:
        raise ValueError('candidate manifest failed: ' + ', '.join(manifest['errors']))
    git_dirs = _git_dirs(checkout)
    if git_dirs != candidate['git_dirs']:
        raise ValueError('candidate Git metadata moved')
    current = _extra_inventory(checkout, list(candidate['extra']), read_json(candidate['manifest'])['files'], git_dirs)
    if current != candidate['extra']:
        raise ValueError('candidate extra inputs changed')
    inputs = [_command_inputs(checkout, manifest, check) for check in candidate['checks']]
    if inputs != candidate.get('check_inputs'):
        raise ValueError('candidate verification harness, toolchain or environment changed')
    if _clean_head(checkout) != head:
        raise ValueError('candidate HEAD changed while inspecting inputs')
    return {'checkout': str(checkout), 'sha': head, 'clean': True, 'result': manifest['result'],
            'manifest': candidate['digest'], 'inputs': manifest['inputs'],
            'extra': verification.digest(candidate['extra'])}


def mount_paths(candidate, writable_roots, *, check_links=True):
    authority = Path(candidate['manifest']).parent
    inputs = [Path(candidate['checkout']), *(Path(path) for path in candidate['git_dirs']),
              *(Path(path) for path in candidate['extra'])]
    protected = [*inputs, authority]
    roots = []
    for root in writable_roots:
        root = Path(root).resolve(strict=True)
        if (not root.is_dir() or any(_overlap(root, path) for path in inputs) or
                (_overlap(root, authority) and not root.is_relative_to(authority / 'artifacts')) or
                any(_overlap(root, Path(path)) for path in ('/proc', '/sys', '/dev'))):
            raise ValueError('writable verification path overlaps a protected path: ' + str(root))
        # A pre-existing hard link in a writable bind mount could alias a protected inode.
        if check_links and any(path.is_file() and not path.is_symlink() and path.stat().st_nlink != 1
                               for path in root.rglob('*')):
            raise ValueError('writable verification path has hard-linked files: ' + str(root))
        if any(_overlap(root, other) for other in roots):
            raise ValueError('overlapping writable verification paths: ' + str(root))
        roots.append(root)
    return [str(p) for p in protected], [str(p) for p in roots]


def _protection(candidate, writable_roots):
    bubblewrap = shutil.which('bwrap', path='/usr/bin:/bin')
    if bubblewrap is None or sys.platform != 'linux':
        raise ValueError('bubblewrap read-only verification is unavailable')
    bubblewrap = Path(bubblewrap).resolve()
    protected, roots = mount_paths(candidate, writable_roots)
    return {'bwrap': str(bubblewrap), 'sha256': _hash(bubblewrap), 'writable_roots': roots,
            'candidate': candidate['digest'], 'checkout': candidate['checkout'],
            'protected': protected, 'extra': verification.digest(candidate['extra'])}


def _role_protection(candidate, guard):
    if 'image' in guard:
        from .cascade_container import guard as container_guard
        roots = guard.get('writable_roots', [])
        if len(roots) != 1:
            raise ValueError('container verifier requires one writable artifact root')
        # All links were checked before launch. A container can create hard links
        # within its artifacts mount (Cargo does this) but not across mounts.
        return container_guard(candidate, roots[0], guard['image'], guard['name'], check_links=False)
    return _protection(candidate, guard.get('writable_roots', []))


def readonly_argv(argv, cwd, writable_roots, *, candidate, role=False, environment=None, process_lock=None):
    """Construct actual protected command; writable mounts can never cover inputs."""
    guard = _protection(candidate, writable_roots)
    args = [guard['bwrap'], '--unshare-user', '--unshare-pid', '--unshare-ipc',
            '--unshare-uts', '--die-with-parent', '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc']
    if role:
        # Retain the controlling PTY for Codex only on a kernel disabling unprivileged TIOCSTI.
        if Path('/proc/sys/dev/tty/legacy_tiocsti').read_text().strip() != '0':
            raise ValueError('interactive verification requires disabled legacy TIOCSTI')
        if '/tmp' in guard['writable_roots']:
            raise ValueError('interactive verification requires private /tmp')
        # Codex uses a hard-coded /tmp scratch location while its own sandbox starts.
        # Mount this before rebinding protected paths that may live beneath /tmp.
        args += ['--tmpfs', '/tmp']
    else:
        args += ['--disable-userns', '--new-session', '--unshare-net']
    if process_lock is not None:
        args += ['--sync-fd', str(process_lock)]
    for path in guard['protected']:
        args += ['--ro-bind', path, path]
    for path in guard['writable_roots']:
        args += ['--bind', path, path]
    if environment is not None:
        args.append('--clearenv')
        for name, value in sorted(environment.items()):
            args += ['--setenv', name, value]
    return args + ['--chdir', str(Path(cwd).resolve(strict=True)), '--', *argv]


_PROBE = """
import errno
import os
import sys
for name in sys.argv[2:]:
    if not os.statvfs(name).f_flag & os.ST_RDONLY:
        raise SystemExit('writable protected mount: ' + name)
try:
    fd = os.open(sys.argv[1], os.O_WRONLY)
except OSError as error:
    if error.errno != errno.EROFS:
        raise
else:
    os.close(fd)
    raise SystemExit('candidate file opened for writing')
print('team-runner read-only probe passed')
"""


def probe_readonly(checkout, candidate, writable_roots, *, role=True, codex_argv=None, environment=None):
    """Prove the input mounts, including inside the actual Codex shell when launching a role."""
    boundary(checkout, candidate)
    guard = _protection(candidate, writable_roots)
    frozen = read_json(candidate['manifest'])
    probe_file = Path(checkout) / next(iter(frozen['files']))
    command = [sys.executable, '-I', '-c', _PROBE, str(probe_file), *guard['protected']]
    if codex_argv is not None:
        command = [*codex_argv, 'sandbox', '--', *command]
    argv = readonly_argv(command, checkout, writable_roots, candidate=candidate, role=role)
    try:
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10,
            env=environment if environment is not None else verification.execution_environment({}),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        message = getattr(exc, 'stderr', b'') or b''
        subject = 'Codex shell' if codex_argv is not None else 'protected command'
        raise ValueError(f'read-only verification probe failed in {subject}: '
                         + (message.decode(errors='replace').strip() or str(exc))) from exc
    if result.stdout.splitlines().count(b'team-runner read-only probe passed') != 1:
        raise ValueError('read-only verification probe did not confirm execution')
    return guard


def _assignment(assignment, role, candidate):
    identity = {k: assignment.get(k) for k in ASSIGNMENT_KEYS}
    if (any(not isinstance(v, str) or not v for v in identity.values()) or
            identity['checkout'] != candidate['checkout'] or identity['role'] != role):
        raise ValueError('invalid verification assignment for ' + role)
    return identity


def run_check(checkout, candidate, assignment, check_index, directory, *, operation_id=None):
    """Run an exact frozen command through the usual verification harness under bwrap."""
    identity = _assignment(assignment, assignment['role'], candidate)
    if identity['role'] not in CHECK_ROLES:
        raise ValueError('verification command requires an assigned verifier')
    directory = Path(directory).resolve(strict=True)
    if directory != Path(candidate['manifest']).parent:
        raise ValueError('wrong verification authority directory')
    if type(check_index) is not int or not 0 <= check_index < len(candidate['checks']):
        raise ValueError('check index is outside the frozen card')
    check = candidate['checks'][check_index]
    before = boundary(checkout, candidate)
    cwd = verification.inside(checkout, check['cwd']).resolve(strict=True)
    preflight = verification.verify(checkout, candidate['manifest'], candidate['digest'])
    command_inputs = _command_inputs(checkout, preflight, check)
    if command_inputs != candidate['check_inputs'][check_index]:
        raise ValueError('verification command inputs changed before execution')
    operation_id = str(uuid.UUID(operation_id)) if operation_id is not None else str(uuid.uuid4())
    stem = 'check-' + operation_id
    output = directory / (stem + '.verification.json')
    path = directory / (stem + '.json')
    if path.exists() or output.exists():
        raise ValueError('verification operation already started; recover its receipt')
    with tempfile.TemporaryDirectory(prefix=stem + '-') as tmp:
        guard = probe_readonly(checkout, candidate, [tmp], role=False)
        env = verification.execution_environment(check['environment'])
        env['TMPDIR'] = tmp
        fd = runner_operations.inherited_lock.get()
        argv = readonly_argv(check['argv'], cwd, [tmp], candidate=candidate, environment=env, process_lock=fd)
        result = verification.run_check(checkout, candidate['manifest'], candidate['digest'], argv,
                                        check['toolchain'], {}, identity['attempt'], output,
                                        pass_fds=(fd,) if fd is not None else ())
    try:
        after = boundary(checkout, candidate)
    except ValueError as exc:
        after = {'result': 'FAIL', 'error': str(exc)}
    receipt = {'operation': operation_id, 'assignment': identity, 'before': before, 'after': after, 'readonly': guard,
               'argv': check['argv'], 'cwd': str(cwd), 'check': check_index, 'inputs': command_inputs,
               'invocation': argv, 'result': result['result'] if before == after else 'FAIL',
               'verification': {'reference': output.name, 'sha256': _hash(output)}}
    atomic_json(path, receipt)
    return {'reference': path.name, 'sha256': _hash(path), 'result': receipt['result']}


def _sealed(directory, descriptor):
    if (not isinstance(descriptor['reference'], str) or
            Path(descriptor['reference']).name != descriptor['reference']):
        raise ValueError('verification receipt must be a top-level authority file')
    path = verification.inside(directory, descriptor['reference'])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != descriptor['sha256']:
        raise ValueError('verification receipt digest changed: ' + path.name)
    return json.loads(raw)


def _check_receipt(directory, descriptor, identity, candidate):
    receipt = _sealed(directory, descriptor)
    if (receipt['assignment'] != identity or receipt['before']['manifest'] != candidate['digest']
            or receipt['before']['sha'] != candidate['sha'] or receipt['before']['clean'] is not True
            or receipt['readonly']['candidate'] != candidate['digest']
            or receipt['readonly']['extra'] != verification.digest(candidate['extra'])):
        raise ValueError('verification command belongs to another assignment or candidate')
    index = receipt['check']
    if type(index) is not int or not 0 <= index < len(candidate['checks']):
        raise ValueError('verification command is outside frozen card')
    check = candidate['checks'][index]
    cwd = verification.inside(candidate['checkout'], check['cwd']).resolve(strict=True)
    if (receipt['argv'] != check['argv'] or receipt['cwd'] != str(cwd)
            or receipt['inputs'] != candidate['check_inputs'][index]):
        raise ValueError('verification command differs from frozen argv, cwd or inputs')
    executed = _sealed(directory, receipt['verification'])
    if (executed['attempt'] != identity['attempt'] or executed['inputs']['command'] != receipt['invocation']
            or executed['inputs']['manifest_inputs'] != receipt['before']['inputs']
            or any(executed['inputs'][key] != receipt['inputs'][key] for key in ('harness', 'toolchain'))
            or executed['status'] != 'executed' or type(executed['exit_code']) is not int
            or executed['result'] not in ('PASS', 'FAIL') or not executed['logs']):
        raise ValueError('missing completed verification execution')
    for log in executed['logs']:
        path = Path(log['path'])
        if not path.is_absolute() or path != path.resolve() or not path.is_relative_to(directory):
            raise ValueError('verification log is outside its authority directory')
        if _hash(path) != log['sha256']:
            raise ValueError('verification log digest changed')
    return receipt, executed


def recover_check(checkout, candidate, assignment, check_index, directory, operation_id):
    """Recover a finished local call under the shared effect lock, without replaying it."""
    operation_id = str(uuid.UUID(operation_id))
    directory = Path(directory).resolve(strict=True)
    if str(Path(checkout).resolve(strict=True)) != candidate['checkout'] or directory != Path(candidate['manifest']).parent:
        raise ValueError('verification recovery belongs to another checkout or authority directory')
    identity = _assignment(assignment, assignment['role'], candidate)
    path = directory / ('check-' + operation_id + '.json')
    if not path.exists():
        return None
    descriptor = {'reference': path.name, 'sha256': _hash(path)}
    receipt, _ = _check_receipt(directory, descriptor, identity, candidate)
    if receipt['operation'] != operation_id or receipt['check'] != check_index or receipt['result'] not in ('PASS', 'FAIL'):
        raise ValueError('verification receipt differs from the started operation')
    return dict(descriptor, result=receipt['result'])


def _validate_check(directory, descriptor, identity, candidate, current):
    receipt, executed = _check_receipt(directory, descriptor, identity, candidate)
    if (receipt['before'] != current or receipt['after'] != current or receipt['result'] != 'PASS'
            or executed['postflight'] != current['inputs'] or executed['result'] != 'PASS' or executed['exit_code'] != 0):
        raise ValueError('verification command does not prove this clean candidate')
    return receipt['check']


def accept(checkout, base_sha, candidate, assignments, reports, verification_receipts, *, directory=None):
    """Check evidence for the exact attempt; caller authenticates Orca receipts and commits the result."""
    if set(assignments) != set(REPORT_ROLES) or set(reports) != set(REPORT_ROLES):
        raise ValueError('coordinator and all three worker reports are required')
    current = boundary(checkout, candidate)
    if candidate['base_sha'] != base_sha:
        raise ValueError('candidate is tied to a different base')
    _ancestor(Path(checkout), base_sha, candidate['sha'])
    directory = Path(directory or Path(candidate['manifest']).parent).resolve(strict=True)
    if directory != Path(candidate['manifest']).parent:
        raise ValueError('wrong verification authority directory')
    receipts = {value['reference']: value for value in verification_receipts}
    if len(receipts) != len(verification_receipts):
        raise ValueError('ambiguous verification receipt reference')
    evidence = {}
    reference_identity = _assignment(assignments['coordinator'], 'coordinator', candidate)
    used_dispatches = set()
    for role in REPORT_ROLES:
        details = assignments[role]
        identity = _assignment(details, role, candidate)
        if (identity['attempt'] != reference_identity['attempt'] or
                identity['execution'] != reference_identity['execution'] or identity['dispatch'] in used_dispatches):
            raise ValueError('worker belongs to another assignment or attempt: ' + role)
        used_dispatches.add(identity['dispatch'])
        report = reports[role]
        if report.get('role') != role or report.get('outcome') != 'succeeded' or report.get('sha') != candidate['sha']:
            raise ValueError('report does not accept the frozen revision: ' + role)
        if role not in CHECK_ROLES:
            continue
        guard = details.get('readonly', {})
        if (details.get('before') != current or details.get('after') != current or
                guard != _role_protection(candidate, guard)):
            raise ValueError('role has no verified read-only session boundary: ' + role)
        if not isinstance(report.get('evidence'), list):
            raise ValueError('report must link verification evidence: ' + role)
        linked = []
        indexes = set()
        for reference in report['evidence']:
            if reference not in receipts:
                continue
            descriptor = receipts[reference]
            indexes.add(_validate_check(directory, descriptor, identity, candidate, current))
            linked.append(descriptor)
        if role == 'tester' and indexes != set(range(len(candidate['checks']))):
            raise ValueError('tester report is missing frozen verification commands')
        evidence[role] = {'assignment': identity, 'boundary': current, 'readonly': guard, 'checks': linked}
    return {'sha': candidate['sha'], 'base_sha': base_sha, 'manifest': candidate['digest'],
            'attempt': reference_identity['attempt'], 'dispatch': reference_identity['dispatch'],
            'evidence': evidence}
