"""Mutually exclusive observed state intervals; unsampled time stays unknown."""
import time
from pathlib import Path

from .runner_store import atomic_json, lock, read_json

CATEGORIES = ('agent_work', 'checks', 'coordinator_wait', 'network_wait', 'limit_wait', 'user_wait', 'unknown')


def reason(runner, state):
    cur = state.get('current') or {}
    reg = runner.launcher.registry(cur['attempt_dir']) if cur.get('attempt_dir') else {}
    if state.get('paused') or state.get('stop_requested') or state.get('control_uncertain') or cur.get('failed'):
        return 'user_wait'
    sessions = read_json(runner.state_dir / 'network-recovery.json', {}).get('sessions', {}).values()
    if any(s.get('scope', {}).get('attempt') == cur.get('attempt_dir') and s.get('status') in ('waiting', 'queued') for s in sessions):
        return 'network_wait'
    if any(c.get('status') == 'limit' for c in reg.get('graph_calls', [])):
        return 'limit_wait'
    from .event_scope import pending
    if pending(cur, reg) or cur.get('gate_id') and not cur.get('gate_resolved'):
        return 'user_wait'
    if cur.get('attempt_dir'):
        for p in (Path(cur['attempt_dir']) / 'verification').glob('*.json'):
            record = read_json(p, {})
            if isinstance(record, dict) and record.get('status') == 'running':
                return 'checks'
    live = [a for a in reg.get('agents', []) if a.get('status') == 'active']
    if any(a.get('activity') in ('working', 'running', 'busy') for a in live):
        return 'agent_work'
    if any(a.get('status') == 'waiting_child' for a in reg.get('agents', [])):
        return 'coordinator_wait'
    return 'unknown'


def observe(runner, state, now=None):
    now = time.time() if now is None else now
    kind = reason(runner, state)
    from .goal_intent import read
    scope = {'generation': read(runner).get('generation'), 'attempt': (state.get('current') or {}).get('attempt_dir')}
    path = runner.state_dir / 'state-time.json'
    with lock(runner.state_dir / 'state-time.lock'):
        value = read_json(path, {'schema_version': 1, 'transitions': [], 'seconds': {k: 0 for k in CATEGORIES}})
        last = value.get('last')
        if last:
            elapsed = max(0, now - last['at'])
            # A long gap cannot establish continued work or continued waiting.
            observed = min(30, elapsed)
            value['seconds'][last['kind']] += observed
            value['seconds']['unknown'] += elapsed - observed
        if not last or last['kind'] != kind or last.get('scope') != scope:
            value['transitions'].append({'at': now, 'kind': kind, 'scope': scope})
        value['last'] = {'at': now, 'kind': kind, 'scope': scope}
        atomic_json(path, value)


def view(runner):
    value = read_json(runner.state_dir / 'state-time.json', {'schema_version': 1, 'seconds': {k: 0 for k in CATEGORIES}, 'transitions': []})
    # Persisted observations only: UI reads never advance clocks or mutate journals.
    return dict(value, total_seconds=sum(value['seconds'].values()), basis='exclusive observed intervals; gaps over 30s unknown')
