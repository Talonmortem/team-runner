"""Conservative, explicit pruning of reproducible payloads; never delete run history."""
import os
from contextlib import nullcontext
from pathlib import Path
import shutil
import time
import uuid

from .runner_store import atomic_json, lock, read_json


KEEP = {'reports', 'evidence', 'verification', 'materials', 'manifests', 'specs', '.git'}


def mountpoints():
    return {Path(line.split()[4].replace('\\040', ' ').replace('\\011', '\t')
                 .replace('\\012', '\n').replace('\\134', '\\'))
            for line in Path('/proc/self/mountinfo').read_text().splitlines()}


def process_references():
    """Read visible references; privileged desktop processes may hide fd/environment."""
    if not Path('/proc/self').exists():
        raise ValueError('cleanup requires Linux process inspection')
    references, restricted = [], []
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if int(proc.name) == os.getpid() or proc.stat().st_uid != os.getuid():
                continue
            references.extend(proc.joinpath('cmdline').read_bytes().decode(errors='replace').split('\0'))
            references.extend(proc.joinpath('environ').read_bytes().decode(errors='replace').split('\0'))
            for entry in [proc / 'cwd', proc / 'exe', *proc.joinpath('fd').iterdir()]:
                try:
                    references.append(os.readlink(entry))
                except FileNotFoundError:
                    pass
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            if proc.exists():
                restricted.append(int(proc.name))
    return references, restricted


def settled(attempt):
    registry = read_json(attempt / 'agents.json')
    # The fence is also set on successful settlement: require it to prevent new children.
    if (not isinstance(registry, dict) or registry.get('fenced') is not True
            or registry.get('recovery_issues') or registry.get('stop_status') == 'unknown'):
        return False
    if any(a.get('status') not in ('released', 'stopped') or not a.get('terminal_closed')
           or a.get('error') or a.get('cleanup_error') for a in registry.get('agents', [])):
        return False
    if any(r.get('status') not in ('closed', 'stopped') for r in registry.get('runs', [])):
        return False
    if any(r.get('status') != 'done' for r in registry.get('launch_requests', [])):
        return False
    operations = read_json(attempt / 'operations.json', {})
    return all(o.get('status') == 'completed' for o in operations.values())


def payload_kind(path):
    if path.name == 'node_modules' and (path.parent / 'package.json').is_file():
        return 'node_modules'
    if ((path / 'PG_VERSION').is_file() and (path / 'global/pg_control').is_file()
            and (path / 'base').is_dir()):
        return 'postgres'
    readme = path / 'README'
    if readme.is_file() and readme.stat().st_size < 4096:
        if readme.read_bytes().startswith(b'This directory holds cached build artifacts from the Go build system.'):
            return 'go-build-cache'
    return None


def candidates(root):
    device = root.stat().st_dev
    mounts = mountpoints()
    for parent, dirs, _ in os.walk(root, followlinks=False):
        if (Path(parent) / '.keep').exists():
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d not in KEEP and
                         not (Path(parent) / d).is_symlink() and
                         Path(parent) / d not in mounts and
                         (Path(parent) / d).stat().st_dev == device)
        for name in list(dirs):
            path = Path(parent) / name
            kind = payload_kind(path)
            if kind:
                dirs.remove(name)
                yield path, kind


def inventory(path):
    """Count allocated bytes; refuse mount boundaries and embedded evidence/logs."""
    device = path.stat().st_dev
    if any(m == path or m.is_relative_to(path) for m in mountpoints()):
        raise ValueError('mounted payload: ' + str(path))
    size, newest = 0, 0
    for parent, dirs, files in os.walk(path, followlinks=False):
        for name in [*dirs, *files]:
            child = Path(parent) / name
            st = child.lstat()
            if st.st_dev != device:
                raise ValueError('mounted payload: ' + str(child))
            if name in KEEP | {'.keep'} or name.endswith(('.log', '.log.gz')):
                raise ValueError('payload contains retained evidence: ' + str(child))
            size += st.st_blocks * 512
            newest = max(newest, st.st_mtime)
        newest = max(newest, Path(parent).stat().st_mtime)
    return size, newest


def cleanup(runner, *, apply=False, days=7, legacy_root=None):
    if os.environ.get('TEAM_RUNNER_ATTEMPT'):
        raise ValueError('child attempts cannot prune run storage')
    if not 0 <= days <= 36500:
        raise ValueError('days must be between 0 and 36500')
    state = runner.state_dir
    result = {'apply': apply, 'days': days, 'candidates': [], 'skipped': [], 'removed_bytes': 0}
    journal = state / 'cleanup' / (str(uuid.uuid4()) + '.json')
    cutoff = time.time() - days * 86400
    # Serialize with starts, retries/child launches (agents.lock), and other pruning.
    with lock(state / 'cleanup.lock', blocking=False), lock(state / 'start.lock', blocking=False):
        current = runner.load_state().get('current') or {}
        protected = {current.get('attempt_dir'), current.get('retry_context_from')}
        references, restricted = process_references()
        result['restricted_processes'] = restricted
        roots = [(p, False) for p in sorted((state / 'attempts').glob('*')) if p.is_dir()]
        if legacy_root is not None:
            legacy_root = Path(legacy_root).expanduser()
            expected = Path.home() / '.local/state/shm-team/runs'
            if legacy_root != expected or legacy_root.resolve() != expected:
                raise ValueError('legacy cleanup is limited to ~/.local/state/shm-team/runs')
            roots += [(p, True) for p in sorted(legacy_root.iterdir()) if p.is_dir()]
        for root, legacy in roots:
            reason = None
            if root.is_symlink() or root.resolve() != root or root in mountpoints():
                reason = 'symlink or mount'
            elif str(root) in protected:
                reason = 'current attempt (including failed/paused)'
            elif (root / '.keep').exists():
                reason = 'pinned with .keep'
            elif any(str(root) in ref for ref in references):
                reason = 'referenced by a live process'
            if reason:
                result['skipped'].append({'path': str(root), 'reason': reason})
                continue
            with nullcontext() if legacy else lock(root / 'agents.lock', blocking=False):
                if not legacy and not settled(root):
                    result['skipped'].append({'path': str(root), 'reason': 'resource settlement unconfirmed'})
                    continue
                for path, kind in candidates(root):
                    if kind == 'postgres' and (path / 'postmaster.pid').exists():
                        # A stale/reused PID is conservatively retained too.
                        try:
                            pid = int((path / 'postmaster.pid').read_text().splitlines()[0])
                        except (ValueError, IndexError):
                            pid = 0
                        if pid <= 0 or Path('/proc', str(pid)).exists():
                            result['skipped'].append({'path': str(path), 'reason': 'PostgreSQL PID live or unknown'})
                            continue
                    try:
                        size, newest = inventory(path)
                    except ValueError as exc:
                        result['skipped'].append({'path': str(path), 'reason': str(exc)})
                        continue
                    if newest > cutoff or (path / '.keep').exists():
                        result['skipped'].append({'path': str(path), 'reason': 'recent or pinned'})
                        continue
                    entry = {'path': str(path), 'kind': kind, 'bytes': size, 'status': 'candidate'}
                    result['candidates'].append(entry)
                    if apply:
                        # Recheck live references immediately before destruction.
                        if any(str(root) in ref for ref in process_references()[0]):
                            entry['status'] = 'skipped_process'
                            continue
                        entry['status'] = 'deleting'
                        atomic_json(journal, result)
                        try:
                            shutil.rmtree(path)
                        except OSError as exc:
                            entry.update(status='error', error=str(exc))
                            atomic_json(journal, result)
                            raise
                        entry['status'] = 'removed'
                        result['removed_bytes'] += size
                        atomic_json(journal, result)
    result['candidate_bytes'] = sum(e['bytes'] for e in result['candidates'])
    if apply:
        result['journal'] = str(journal)
        atomic_json(journal, result)
    return result
