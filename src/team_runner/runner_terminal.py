"""Read-only reconciliation of the current runner terminal after process exit."""
from functools import partial
import subprocess

from . import runner_operations as operations
from .protocol import ContractError, request_id
from .runner_store import atomic_json, lock, read_json

PROCESS_FIELDS = ('pid', 'process_stamp', 'boot_id')
INVENTORY_TIMEOUT = 5


def mark_idle(state_dir, nonce):
    """Preserve cleanup fields on the same launch's idle transition under start.lock."""
    path = state_dir / 'startup.json'
    startup = read_json(path, {})
    if startup.get('nonce') != nonce:
        startup = {'nonce': nonce}
    atomic_json(path, dict(startup, status='idle'))


def _binding(runner, startup, launch):
    nonce = startup.get('nonce')
    if (startup.get('status') != 'idle' or launch.get('nonce') != nonce
            or launch.get('identity') != runner.context.identity()):
        return None
    try:
        request_id(nonce)
    except ContractError:
        return None
    return {'create_key': 'runner-' + nonce, 'nonce': nonce, 'identity': launch['identity']}


def _origin(runner, journal, binding):
    key = binding['create_key']
    directory = runner.state_dir / 'startup'
    create = journal.get(key, {})
    worktree = runner.context.orca_id
    if (create.get('args', [])[:4] != ['terminal', 'create', '--worktree', 'id:' + worktree]
            or create.get('evidence') != {'marker': str(directory / (key + '.entry.json')),
                                          'nonce': key + ':' + str(directory.resolve())}):
        return None
    marker = operations._launch_marker(create, worktree)
    if not marker or type(marker['pid']) is not int or marker['pid'] <= 0:
        return None
    process = {field: marker.get(field) for field in PROCESS_FIELDS}
    recorded = read_json(runner.state_dir / 'runner-process.json', {})
    if recorded.get('nonce') == binding['nonce'] and (
            recorded.get('identity') != binding['identity']
            or any(recorded.get(field) != value for field, value in process.items())):
        return None
    return create, process


def _matches(proof, origin):
    create, process = origin
    return (isinstance(proof, dict) and proof.get('process') == process
            and proof.get('terminal') == create['result']['terminal'])


def _observation(journal, binding, origin):
    entry = journal.get(binding['create_key'] + '-absence', {})
    proof = entry.get('result')
    if (entry.get('status') == 'completed' and entry.get('stage') == 'absence_observed'
            and entry.get('evidence') == binding and _matches(proof, origin)):
        return proof
    return None


def _saved_proof(journal, binding, origin):
    close = journal.get(binding['create_key'] + '-close', {})
    if (close.get('stage') == 'close_verified' and close.get('status') == 'completed'
            and (close.get('evidence') or {}).get('launch') == binding['create_key']):
        return close['result']
    return _observation(journal, binding, origin) if origin else None


def has_observation(runner, startup, launch, journal):
    binding = _binding(runner, startup, launch)
    origin = _origin(runner, journal, binding) if binding else None
    proof = _observation(journal, binding, origin) if origin else None
    return proof is not None and proof == startup.get('close_evidence')


def _project(path, startup, proof):
    startup['close_evidence'] = proof
    startup.pop('cleanup_error', None)
    atomic_json(path, startup)


def reconcile(runner):
    """Save a positive observation without sending or settling a close operation."""
    state_dir = runner.state_dir
    path = state_dir / 'startup.json'
    journal_path = runner.launcher.journal(state_dir / 'startup')
    with lock(state_dir / 'start.lock', blocking=False):
        startup = read_json(path, {})
        if startup.get('status') != 'idle' or startup.get('close_evidence'):
            return
        launch = read_json(state_dir / 'launch.json', {})
        binding = _binding(runner, startup, launch)
        if binding is None:
            return
        journal = read_json(journal_path, {})
        origin = _origin(runner, journal, binding)
        saved = _saved_proof(journal, binding, origin)
        if saved is not None:
            _project(path, startup, saved)
            return
        if origin is None:
            return
    create, _ = origin
    transport = partial(runner.launcher.orca, timeout=INVENTORY_TIMEOUT)

    def verified_inventory(*args, **kwargs):
        runner.context.verify(transport)
        return transport(*args, **kwargs)

    try:
        proof = operations.missing_terminal_evidence(create, verified_inventory, runner.context.orca_id)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError):
        return
    if not _matches(proof, origin):
        return
    with lock(state_dir / 'start.lock', blocking=False):
        startup = read_json(path, {})
        current = read_json(state_dir / 'launch.json', {})
        if current != launch or _binding(runner, startup, current) != binding or startup.get('close_evidence'):
            return
        journal = read_json(journal_path, {})
        if _origin(runner, journal, binding) != origin:
            return
        saved = _saved_proof(journal, binding, origin)
        if saved is not None:
            _project(path, startup, saved)
            return
        journal[binding['create_key'] + '-absence'] = {
            'status': 'completed', 'stage': 'absence_observed', 'evidence': binding, 'result': proof,
        }
        atomic_json(journal_path, journal)
        _project(path, startup, proof)
