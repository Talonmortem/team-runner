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


def document(runner):
    from .protocol import ContractError, version
    value = read_json(runner.state_dir / 'autonomy.json')
    if value is None and not (runner.state_dir / 'autonomy.json').exists():
        return {'schema_version': 1, 'revision': 0, 'enabled': False, 'history': []}
    version(value)
    if (type(value.get('enabled')) is not bool or type(value.get('revision')) is not int
            or type(value.get('unlimited_passes', False)) is not bool
            or value['revision'] < 0 or not isinstance(value.get('history'), list)
            or not isinstance(value.get('scope'), dict)
            or any(not isinstance(h, dict) or not isinstance(h.get('request_id'), str)
                   for h in value['history'])):
        raise ContractError('invalid_policy', 'Invalid stored autonomy policy; inspect before continuing')
    return value


def policy(runner):
    value = document(runner)
    current = scope(runner)
    active = value.get('scope') == current
    unlimited = active and value['enabled'] and value.get('unlimited_passes', False)
    return {'schema_version': 1, 'scope': current, 'revision': value['revision'],
            'enabled': active and value['enabled'], 'unlimited_passes': unlimited,
            'extra_passes': None if unlimited else LIMIT,
            'network_resumes': 2, 'network_replacements': 1}


def configure(runner, enabled, identifier, *, expected=None, unlimited_passes=False):
    if type(enabled) is not bool:
        raise ValueError('enabled boolean required')
    if type(unlimited_passes) is not bool or (unlimited_passes and not enabled):
        raise ValueError('unlimited_passes requires enabled autonomy')
    with lock(runner.state_dir / 'goal-intent.lock'):
        if not scope(runner)['generation']:
            raise ValueError('autonomy requires a goal generation')
        if expected is not None:
            from .application import _execution_scope
            if expected != _execution_scope(runner, runner.load_state()):
                raise ValueError('autonomy execution changed before application')
        with lock(runner.state_dir / 'autonomy.lock'):
            path = runner.state_dir / 'autonomy.json'
            value = document(runner)
            entry = {'request_id': identifier, 'scope': scope(runner), 'enabled': enabled,
                     'unlimited_passes': unlimited_passes}
            previous = next((h for h in value['history'] if h['request_id'] == identifier), None)
            if previous:
                if any(previous.get(k, False) != v for k, v in entry.items()):
                    raise ValueError('autonomy request conflict')
            else:
                value.update(scope=entry['scope'], enabled=enabled, unlimited_passes=unlimited_passes,
                             revision=value['revision'] + 1)
                value['history'].append(dict(entry, revision=value['revision'], at=time.time()))
                atomic_json(path, value)
    if not enabled:
        from .network_recovery import cancel
        cancel(runner, 'autonomy disabled')
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
    current = policy(runner)
    limit = current['extra_passes']
    budgets = {edge: {'used': len(ledger.get(budget_key(runner, state, edge), {})), 'limit': limit}
               for edge in sorted(edges)}
    for b in budgets.values():
        b['remaining'] = None if limit is None else max(0, limit - b['used'])
    return dict(current, budgets=budgets)


def call_authority(registry, call):
    if call.get('kind') != 'rework':
        return call.get('authorization')
    source = next((a for a in registry['agents'] if a['id'] == call['parent_agent']), {})
    return next((c.get('authorization') for c in registry.get('graph_calls', [])
                 if c.get('child_agent') == source.get('id')), None)


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
        from .event_scope import pending
        if pending(state.get('current') or {}, reg):
            return None
        with lock(runner.state_dir / 'autonomy.lock'):
            from .goal_intent import read
            intent = read(runner)
            if (not policy(runner)['enabled'] or state.get('paused') or state.get('stop_requested')
                    or state.get('control_uncertain') or intent.get('desired_state') != 'running'
                    or (state.get('current') or {}).get('attempt_dir') != str(attempt)):
                return None
            if call_authority(reg, call) != {k: intent.get(k) for k in ('generation', 'epoch')}:
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
                limit = policy(runner)['extra_passes']
                if limit is not None and len(bucket) >= limit:
                    call['autonomy_blocker'] = {'reason': 'automatic edge budget exhausted', 'used': len(bucket), 'limit': limit}
                    runner.launcher.save_registry(attempt, reg)
                    return None
                bucket[identifier] = {'call_id': call_id, 'edge_id': call['edge_id'],
                                      'attempt': str(attempt), 'at': time.time(), 'automatic': True}
                atomic_json(path, ledger)
            grant = {k: v for k, v in bucket[identifier].items() if k != 'attempt'}
            grants = reg.setdefault('graph_grants', {})
            if identifier not in grants:
                call.pop('autonomy_blocker', None)
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
