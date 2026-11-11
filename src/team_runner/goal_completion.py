"""Runner-owned completion receipts; reading never infers or records acceptance."""
import hashlib
import json
import os
import stat
import subprocess

from . import goal_intent
from .runner_store import atomic_json, read_json
from .tracking import Tracking

FINAL_ID = 'GOAL-FINAL-REVIEW'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def tracking_fingerprint(runner, goal):
    """Require the complete tracking tree, including dependencies, before hashing."""
    directory = (runner.repo / goal).resolve()
    if not directory.is_relative_to(runner.repo.resolve()):
        raise ValueError('goal escapes checkout')
    tr = Tracking(directory)
    if not isinstance(tr.goal.get('queue'), list):
        raise ValueError('invalid goal queue')
    features = list(tr.features())
    if len(features) != len(tr.goal['queue']):
        raise ValueError('missing feature tracking')
    feature_ids = [f['id'] for f in tr.goal['queue']]
    if len(set(feature_ids)) != len(feature_ids):
        raise ValueError('duplicate feature identity')
    documents = {'Tasks.json': tr.goal}
    for feat, path, data in features:
        if not tr.feature_done(feat['id']) or feat.get('status') != 'completed' or not isinstance(data.get('queue'), list):
            raise ValueError('feature incomplete or invalid')
        ids = [item['id'] for item in data['queue']]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate item identity')
        for dep in feat.get('depends_on', []):
            if dep not in feature_ids or not tr.feature_done(dep):
                raise ValueError('feature dependency incomplete')
        for item in data['queue']:
            if not tr.item_done(item):
                raise ValueError('item incomplete')
            for dep in item.get('depends_on', []):
                if not tr.dependency_done(feat, data, dep):
                    raise ValueError('item dependency incomplete')
        documents[str(path.relative_to(directory))] = data
    return _digest(documents)


def source_proof(runner, goal):
    """Read the reviewed worktree and required artifacts, without touching Git state.

    Scope is tracked and nonignored untracked files, plus required acceptance
    artifacts even if ignored. Configured runtime state is excluded to avoid
    completion/history writes invalidating their own proof. Symlinks are hashed
    as links; required artifacts must resolve within the goal directory.
    """
    repo = runner.repo.resolve()
    excluded = (runner.state_dir.resolve(), runner.context.state_root.resolve())
    if any(repo.is_relative_to(root) for root in excluded):
        raise ValueError('runtime state root contains reviewed checkout')
    def git(*args):
        result = subprocess.run(['git', '-C', str(repo), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise ValueError('cannot establish source proof')
        return result.stdout
    paths = {os.fsdecode(path) for path in git('ls-files', '-z', '--cached', '--others',
                                              '--exclude-standard').split(b'\0') if path}
    # An unborn checkout is valid with a non-commit acceptance profile.
    head = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet', 'HEAD'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if head.returncode not in (0, 1):
        raise ValueError('cannot establish reviewed HEAD')
    directory = (repo / goal).resolve()
    if not directory.is_relative_to(repo):
        raise ValueError('goal escapes checkout')
    profile = runner.attempt_profile()
    tr = Tracking(directory)
    artifacts = [directory / profile['evidence_dir'] / 'goal-final-review.md']
    for feat, _, data in tr.features():
        artifacts.extend(directory / feat['path'] / profile['evidence_dir'] /
                         item['id'].lower() / 'acceptance.md' for item in data['queue'])
    required = set()
    for artifact in artifacts:
        resolved = artifact.resolve()
        if (not resolved.is_relative_to(directory) or not resolved.is_file()
                or any(resolved.is_relative_to(root) for root in excluded)):
            raise ValueError('required acceptance artifact missing or outside goal')
        required.add(str(artifact.relative_to(repo)))
        # Hash resolved content as well when an artifact itself is a symlink.
        required.add(str(resolved.relative_to(repo)))
    paths.update(required)
    entries = {}
    for relative in sorted(paths):
        path = repo / relative
        if any(path.absolute().is_relative_to(root) for root in excluded):
            continue
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            entries[relative] = {'missing': True}
            continue
        if stat.S_ISLNK(mode):
            entries[relative] = {'link': os.readlink(path)}
        elif stat.S_ISREG(mode):
            entries[relative] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                 'executable': bool(mode & 0o111)}
        else:
            raise ValueError('unsupported source entry: ' + relative)
    return {'schema_version': 1, 'scope': 'git-files-and-acceptance-v1',
            'head': head.stdout.decode().strip() or None, 'digest': _digest(entries)}


def _candidate(runner, state, intent):
    if (not intent or state.get('goal') != intent['goal'] or state.get('current')
            or state.get('control_uncertain') or state.get('recovery_issues')
            or state.get('stop_requested') or state.get('skipped')):
        return {}
    fingerprint = tracking_fingerprint(runner, intent['goal'])
    final = intent['policy']['final']
    proof = None
    if final:
        last = (state.get('history') or [{}])[-1]
        if (last.get('item') != FINAL_ID or last.get('outcome') != 'succeeded'
                or last.get('problems') != [] or not last.get('dispatch_id')
                or last.get('completion_generation') != intent['generation']
                or last.get('tracking_fingerprint') != fingerprint
                or last.get('source_proof') != source_proof(runner, intent['goal'])):
            return {}
        proof = _digest(last)
    return {'schema_version': 1, 'identity': runner.context.identity(),
            'generation': intent['generation'], 'goal': intent['goal'],
            'tracking_fingerprint': fingerprint, 'final_required': final,
            'final_accepted': final, 'final_history_proof': proof,
            'status': 'completed' if final else 'queue_completed'}


def read(runner):
    """Return a currently valid receipt, or {} for missing/stale/invalid evidence."""
    try:
        receipt = read_json(runner.state_dir / 'goal-completion.json', {})
        if not receipt:
            return {}
        candidate = _candidate(runner, runner.load_state(), goal_intent.read(runner))
        return receipt if candidate and receipt == candidate else {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def record(runner, state):
    """Called by the owning runner at exhaustion, never by supervisor or readers."""
    intent = goal_intent.read(runner)
    auth = getattr(runner, 'authorization', None)
    if not intent or not auth or not goal_intent.permitted(runner, auth['generation'], auth['epoch']):
        return {}
    try:
        receipt = _candidate(runner, state, intent)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    if receipt and read_json(runner.state_dir / 'goal-completion.json', {}) != receipt:
        atomic_json(runner.state_dir / 'goal-completion.json', receipt)
    return receipt
