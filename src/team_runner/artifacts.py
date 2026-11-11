"""Lossless content-addressed storage for explicitly retired verification copies."""
import gzip
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from .retention import mountpoints, process_references, settled
from .runner_store import atomic_json, lock, read_json

CHUNK = 1024 * 1024


def digest_stream(stream):
    digest = hashlib.sha256()
    while block := stream.read(CHUNK):
        digest.update(block)
    return digest.hexdigest()


def signature(path):
    s = path.lstat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode]


class Store:
    def __init__(self, state):
        self.root = Path(state) / 'artifact-store'
        self.verified = set()

    def blob(self, digest):
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid artifact digest')
        return self.root / 'objects' / digest[:2] / (digest + '.gz')

    def verify(self, digest):
        if digest not in self.verified:
            with gzip.open(self.blob(digest), 'rb') as f:
                if digest_stream(f) != digest:
                    raise ValueError('artifact checksum mismatch: ' + digest)
            self.verified.add(digest)

    def put(self, path):
        before = signature(path)
        with path.open('rb') as f:
            digest = digest_stream(f)
        target = self.blob(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as raw:
                    with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1, mtime=0) as out, path.open('rb') as source:
                        shutil.copyfileobj(source, out, CHUNK)
                    raw.flush(); os.fsync(raw.fileno())
                os.replace(temp, target)
                directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(directory)
                finally: os.close(directory)
            finally:
                if os.path.exists(temp): os.unlink(temp)
        self.verify(digest)
        if before != signature(path):
            raise ValueError('input changed while archiving: ' + str(path))
        return digest, before


def validate_run(runner, run):
    run = Path(run).absolute()
    attempts = runner.state_dir / 'attempts'
    relative = run.relative_to(attempts)
    if len(relative.parts) < 3 or run.resolve() != run or not run.is_dir():
        raise ValueError('select a real verification subdirectory inside an attempt')
    inherited = os.environ.get('TEAM_RUNNER_ATTEMPT')
    if inherited and not run.is_relative_to(Path(inherited)):
        raise ValueError('cannot retire another attempt from a child')
    receipts = []
    for path in run.glob('*verification.json'):
        value = read_json(path)
        if value.get('status') != 'executed' or value.get('result') not in ('PASS', 'FAIL'):
            raise ValueError('verification unsettled: ' + str(path))
        resource = value.get('resource')
        if resource and (resource.get('status') != 'clean' or not resource.get('cleanup_inventory_complete')
                         or resource.get('processes') or resource.get('ports')):
            raise ValueError('verification resources not clean')
        receipts.append(str(path))
    if not receipts:
        raise ValueError('no completed verification receipt in subrun')
    if any(str(run) in ref for ref in process_references()[0]):
        raise ValueError('verification subrun referenced by a live process')
    if (run / '.keep').exists():
        raise ValueError('verification subrun pinned')
    return run, receipts


def payloads(run):
    for path in sorted(run.iterdir()):
        if path.is_dir() and (path.name in ('bundle', 'copy', 'inputs', 'pg-data', 'gopath', 'tmp', 'runtime-home', 'vite-cache')
                              or path.name.endswith('-installed') and (path / 'dependencies').is_dir()):
            yield path


def remove_archived_tree(tree):
    # Go module/toolchain trees can be read-only. Their original modes are in the
    # verified manifest; only private directory permissions change during removal.
    for parent, dirs, _ in os.walk(tree, followlinks=False):
        os.chmod(parent, stat.S_IMODE(Path(parent).stat().st_mode) | 0o700)
        for name in dirs:
            p = Path(parent) / name
            if p.is_symlink(): raise ValueError('symlink appeared during removal')
            os.chmod(p, stat.S_IMODE(p.stat().st_mode) | 0o700)
    shutil.rmtree(tree)


def compact(runner, run, *, apply=False, store=None):
    """Explicit release by the caller; completed receipts and closed processes required.

    Preserve exact files in deduplicated compressed objects before removing copies.
    The caller must finish source review and exclude future writers/readers of this run.
    """
    store = store or Store(runner.state_dir)
    with lock(store.root / 'store.lock', blocking=False):
        run, receipts = validate_run(runner, run)
        result = {'run': str(run), 'apply': apply, 'trees': [], 'released_bytes': 0}
        # A process may stop after rmtree succeeds but before its receipt is updated.
        if apply:
            for receipt in (run / '.artifact-archives').glob('*.json'):
                if receipt.name.endswith('.manifest.json'): continue
                record = read_json(receipt)
                tree = run / receipt.stem
                if record.get('status') != 'deleting' or tree.exists() or tree.is_symlink(): continue
                manifest = receipt.with_name(receipt.stem + '.manifest.json')
                if hashlib.sha256(manifest.read_bytes()).hexdigest() != record['sha256']:
                    raise ValueError('archive manifest checksum mismatch')
                for entry in read_json(manifest)['entries']:
                    if not entry['directory']: store.verify(entry['sha256'])
                record['status'] = 'archived'; atomic_json(receipt, record)
        mounts = mountpoints()
        for tree in payloads(run):
            if tree.is_symlink() or any(m == tree or m.is_relative_to(tree) for m in mounts):
                raise ValueError('symlink/mounted artifact tree')
            receipt = run / '.artifact-archives' / (tree.name + '.json')
            paths = [tree]
            for parent, dirs, files in os.walk(tree, followlinks=False):
                paths.extend(Path(parent) / n for n in sorted([*dirs, *files]))
            size = sum(p.lstat().st_blocks * 512 for p in paths)
            entry = {'tree': tree.name, 'bytes': size}
            result['trees'].append(entry)
            if not apply:
                continue
            if receipt.exists():
                record = read_json(receipt)
                manifest = run / '.artifact-archives' / (tree.name + '.manifest.json')
                if record.get('status') not in ('archived', 'deleting') or hashlib.sha256(manifest.read_bytes()).hexdigest() != record['sha256']:
                    raise ValueError('incomplete archive or corrupt manifest; inspect before retry')
                previous = read_json(manifest)['entries']
                actual = {p.relative_to(tree).as_posix() for p in paths}
                expected = {e['path'] for e in previous}
                if (not actual <= expected or record['status'] == 'archived' and actual != expected):
                    raise ValueError('restored tree inventory changed; refusing to overwrite archive')
                for e in previous:
                    if not e['directory']: store.verify(e['sha256'])
                    p = tree / e['path']
                    if e['path'] not in actual: continue
                    s = p.lstat()
                    partial_directory = record['status'] == 'deleting' and e['directory']
                    if p.is_symlink() or (not partial_directory and
                          (stat.S_IMODE(s.st_mode) != e['mode'] or s.st_mtime_ns != e['mtime_ns'])):
                        raise ValueError('restored tree metadata changed')
                    if not e['directory']:
                        digest, _ = store.put(p)
                        if digest != e['sha256']: raise ValueError('restored tree contents changed')
                validate_run(runner, run)
                record['status'] = 'deleting'; atomic_json(receipt, record)
                remove_archived_tree(tree)
                record['status'] = 'archived'; atomic_json(receipt, record)
                result['released_bytes'] += size; entry['status'] = 'archived'
                continue
            entries = []
            for path in paths:
                s = path.lstat()
                if path.name == '.keep' or not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
                    raise ValueError('pinned or unsupported artifact entry: ' + str(path))
                item = {'path': path.relative_to(tree).as_posix(), 'mode': stat.S_IMODE(s.st_mode),
                        'mtime_ns': s.st_mtime_ns, 'directory': path.is_dir()}
                if item['directory']:
                    item['signature'] = signature(path)
                else:
                    item['sha256'], item['signature'] = store.put(path)
                entries.append(item)
            # All bytes are recoverable; prove the original tree stayed unchanged before deletion.
            actual = {'.'}
            for parent, dirs, files in os.walk(tree, followlinks=False):
                actual.update((Path(parent) / n).relative_to(tree).as_posix() for n in [*dirs, *files])
            if actual != {e['path'] for e in entries}:
                raise ValueError('artifact tree inventory changed during archive')
            if any(signature(tree / e['path']) != e['signature'] for e in entries):
                raise ValueError('artifact tree changed during archive')
            validate_run(runner, run)
            manifest = run / '.artifact-archives' / (tree.name + '.manifest.json')
            atomic_json(manifest, {'version': 1, 'tree': tree.name, 'entries': entries})
            manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
            record = {'status': 'deleting', 'manifest': manifest.name, 'sha256': manifest_hash,
                      'bytes': size, 'verification_receipts': receipts, 'store': str(store.root)}
            atomic_json(receipt, record)
            remove_archived_tree(tree)
            record['status'] = 'archived'
            atomic_json(receipt, record)
            result['released_bytes'] += size
            entry['status'] = 'archived'
        return result


def restore(runner, run, tree_name):
    run = Path(run).absolute()
    relative = run.relative_to(runner.state_dir / 'attempts')
    if len(relative.parts) < 3 or run.resolve() != run or Path(tree_name).name != tree_name:
        raise ValueError('invalid restore scope')
    if os.environ.get('TEAM_RUNNER_ATTEMPT') and not run.is_relative_to(Path(os.environ['TEAM_RUNNER_ATTEMPT'])):
        raise ValueError('cannot restore another attempt from a child')
    store = Store(runner.state_dir)
    with lock(store.root / 'store.lock', blocking=False):
        record = read_json(run / '.artifact-archives' / (tree_name + '.json'))
        if not record or record['status'] != 'archived':
            raise ValueError('no completed archive to restore')
        if Path(record['manifest']).name != record['manifest']:
            raise ValueError('invalid manifest path')
        manifest = run / '.artifact-archives' / record['manifest']
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('archive manifest checksum mismatch')
        entries = read_json(manifest)['entries']
        target = run / tree_name
        if target.exists() or target.is_symlink():
            raise ValueError('restore destination already exists')
        for e in entries:
            p = Path(e['path'])
            if p.is_absolute() or '..' in p.parts:
                raise ValueError('archive path escapes destination')
            if not e['directory']: store.verify(e['sha256'])
        temp = Path(tempfile.mkdtemp(prefix='.restore-', dir=run))
        try:
            for e in entries:
                p = temp / e['path']
                if e['directory']:
                    p.mkdir(parents=True, exist_ok=True)
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    with gzip.open(store.blob(e['sha256']), 'rb') as source, p.open('wb') as out:
                        shutil.copyfileobj(source, out, CHUNK)
                    os.chmod(p, e['mode']); os.utime(p, ns=(e['mtime_ns'], e['mtime_ns']))
            for e in reversed(entries):
                if e['directory']:
                    p = temp / e['path']; os.chmod(p, e['mode']); os.utime(p, ns=(e['mtime_ns'], e['mtime_ns']))
            os.rename(temp, target)
        finally:
            if temp.exists(): shutil.rmtree(temp)
        return {'restored': str(target), 'files': len(entries)}


def sweep(runner, *, attempt=None, apply=False):
    """Explicit attempt: caller releases completed subruns. Default: settled attempts only."""
    store = Store(runner.state_dir)
    roots = [Path(attempt).absolute()] if attempt else sorted((runner.state_dir / 'attempts').glob('*'))
    if os.environ.get('TEAM_RUNNER_ATTEMPT') and (not attempt or str(roots[0]) != os.environ['TEAM_RUNNER_ATTEMPT']):
        raise ValueError('child sweep requires its exact attempt')
    results, skipped = [], []
    for root in roots:
        if root.parent != runner.state_dir / 'attempts' or root.resolve() != root or not root.is_dir():
            raise ValueError('invalid attempt scope')
        if not attempt and not settled(root):
            continue
        for parent, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in {'bundle', 'copy', 'inputs', 'dependencies', 'node_modules',
                         '.artifact-archives', 'pg-data', 'gopath', 'tmp', 'runtime-home', '.git', 'materials'}
                       and not (Path(parent) / d).is_symlink()]
            if not any(f.endswith('verification.json') for f in files):
                continue
            try:
                results.append(compact(runner, parent, apply=apply, store=store))
            except (ValueError, OSError) as exc:
                skipped.append({'run': parent, 'reason': str(exc)})
    return {'runs': results, 'skipped': skipped, 'released_bytes': sum(r['released_bytes'] for r in results)}


def prepare(runner, attempt, argv, *, reserve_gib=25, required_gib=12, limit_gib=40):
    """Admission gate: retire completed copies before a serialized fresh preparation."""
    if not attempt or not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v for v in argv):
        raise ValueError('prepare requires an attempt and JSON argv array')
    if min(reserve_gib, required_gib, limit_gib) <= 0:
        raise ValueError('storage limits must be positive')
    attempt = Path(attempt).absolute()
    current = (runner.load_state().get('current') or {}).get('attempt_dir')
    if str(attempt) != current:
        raise ValueError('prepare requires the current attempt')
    with lock(runner.state_dir / 'artifact-prepare.lock', blocking=False):
        released = sweep(runner, attempt=attempt, apply=True)
        # Count all resident payloads, including PREPARED_NOT_RUN copies without receipts.
        size = 0
        for parent, dirs, _ in os.walk(attempt, followlinks=False):
            dirs[:] = [d for d in dirs if d != '.artifact-archives' and not (Path(parent) / d).is_symlink()]
            for tree in payloads(Path(parent)):
                dirs.remove(tree.name)
                for base, subdirs, files in os.walk(tree, followlinks=False):
                    for name in [*subdirs, *files]: size += (Path(base) / name).lstat().st_blocks * 512
        free = shutil.disk_usage(attempt).free
        if free < (reserve_gib + required_gib) * 2**30 or size + required_gib * 2**30 > limit_gib * 2**30:
            raise ValueError(f'storage admission denied: free={free}, resident_payload={size}; no preparation started')
        record = {'argv': argv, 'free_before': free, 'resident_bytes': size,
                  'released_bytes': released['released_bytes'], 'status': 'starting'}
        path = attempt / 'storage-admission.json'
        atomic_json(path, record)
        result = subprocess.run(argv, cwd=runner.repo, check=False)
        record.update(status='finished', exit_code=result.returncode, free_after=shutil.disk_usage(attempt).free)
        atomic_json(path, record)
        return record
