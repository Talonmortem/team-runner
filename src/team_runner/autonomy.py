"""Versioned workspace/generation policy, and an atomic cross-attempt grant ledger."""
import copy
from pathlib import Path
import time
import uuid

from .runner_store import atomic_json, lock, read_json

LIMIT = 3


def scope(runner, state=None):
    from .goal_intent import read
    intent = read(runner)
    return {'identity': runner.context.identity(), 'generation': intent.get('generation')}


def policy(runner):
    value = read_json(runner.state_dir / 'autonomy.json', {})
    active = value.get('scope') == scope(runner)
    return {'schema_version': 1, 'scope': scope(runner), 'revision': value.get('revision', 0),
            'enabled': bool(active and value.get('enabled')), 'extra_passes': LIMIT,
            'network_resumes': 2, 'network_replacements': 1}


def configure(runner, enabled, identifier):
    if type(enabled) is not bool:
        raise ValueError('enabled boolean required')
    if not scope(runner)['generation']:
        raise ValueError('autonomy requires a goal generation')
    with lock(runner.state_dir / 'autonomy.lock'):
        path = runner.state_dir / 'autonomy.json'
        value = read_json(path, {'schema_version': 1, 'revision': 0, 'history': []})
        entry = {'request_id': identifier, 'scope': scope(runner), 'enabled': enabled}
        previous = next((h for h in value['history'] if h['request_id'] == identifier), None)
        if previous:
            if any(previous[k] != v for k, v in entry.items()):
                raise ValueError('autonomy request conflict')
            return policy(runner)
        value.update(scope=entry['scope'], enabled=enabled, revision=value['revision'] + 1)
        value['history'].append(dict(entry, revision=value['revision'], at=time.time()))
        atomic_json(path, value)
    return policy(runner)


def budget_key(runner, state, edge):
    from .protocol import digest
    return digest(dict(scope(runner), goal=state.get('goal'), item=(state.get('current') or {}).get('item'), edge=edge))


def view(runner):
    state = runner.load_state()
    cur = state.get('current') or {}
    ledger = read_json(runner.state_dir / 'autonomy-budget.json', {})
    reg = runner.launcher.registry(cur['attempt_dir']) if cur.get('attempt_dir') else {}
    edges = {c['edge_id'] for c in reg.get('graph_calls', [])}
    budgets = {edge: {'used': len(ledger.get(budget_key(runner, state, edge), {})), 'limit': LIMIT}
               for edge in sorted(edges)}
    for b in budgets.values():
        b['remaining'] = max(0, b['limit'] - b['used'])
    return dict(policy(runner), budgets=budgets)


def grant(runner, attempt, call_id):
    """Reserve+publish in one durable ledger write; deterministic grant repairs crashes.

    Lock order: agents.lock -> autonomy.lock, also used by report-side rework.
    The ledger is authoritative; a crash before registry write cannot spend twice.
    """
    from . import graph_dispatch as graph
    state = runner.load_state()
    graph.current(runner.launcher, attempt)
    with lock(Path(attempt) / 'agents.lock'):
        reg = runner.launcher.registry(attempt)
        call = next((c for c in reg.get('graph_calls', []) if c['id'] == call_id), None)
        if not call or reg.get('fenced') or reg.get('stop_requested'):
            return None
        with lock(runner.state_dir / 'autonomy.lock'):
            from .goal_intent import read
            intent = read(runner)
            if (not policy(runner)['enabled'] or state.get('paused') or state.get('stop_requested')
                    or state.get('control_uncertain') or intent.get('desired_state') != 'running'
                    or (state.get('current') or {}).get('attempt_dir') != str(attempt)):
                return None
            from .recovery import _unknown
            if _unknown(runner, state):
                return None
            identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, str(attempt) + ':autonomy:' + call_id))
            path = runner.state_dir / 'autonomy-budget.json'
            ledger = read_json(path, {})
            bucket = ledger.setdefault(budget_key(runner, state, call['edge_id']), {})
            if identifier not in bucket:
                if call.get('status') != 'limit':
                    return None
                if len(bucket) >= LIMIT:
                    call['autonomy_blocker'] = {'reason': 'automatic edge budget exhausted', 'used': len(bucket), 'limit': LIMIT}
                    runner.launcher.save_registry(attempt, reg)
                    return None
                bucket[identifier] = {'call_id': call_id, 'edge_id': call['edge_id'],
                                      'attempt': str(attempt), 'at': time.time(), 'automatic': True}
                atomic_json(path, ledger)
            grant = {k: v for k, v in bucket[identifier].items() if k != 'attempt'}
            grants = reg.setdefault('graph_grants', {})
            if identifier not in grants:
                grants[identifier] = copy.deepcopy(grant)
                graph.take_pass(reg, graph.snapshot(runner.launcher, attempt), call)
                runner.launcher.save_registry(attempt, reg)
            return copy.deepcopy(grants[identifier])


def tick(runner, state):
    cur = state.get('current') or {}
    if not cur.get('attempt_dir') or not policy(runner)['enabled']:
        return
    reg = runner.launcher.registry(cur['attempt_dir'])
    for call in reg.get('graph_calls', []):
        if call.get('status') == 'limit':
            grant(runner, cur['attempt_dir'], call['id'])
