"""Eligibility and dispatch checks for a stopped attempt displaced by a dependency."""
import copy
from pathlib import Path

from .protocol import ContractError
from .runner_store import read_json
from .tracking import for_goal


def _dependencies(tracking, feature, item):
    # Tracking resolves local dependencies before feature IDs and qualified keys.
    by_name = {task['id']: key for key, feat, _, task in tracking.items() if feat == feature}
    return {by_name.get(dep, dep) for dep in item.get('depends_on', [])}


def plan(runner, intent, state, predecessor):
    tracking = for_goal(runner, intent['goal'], state)
    cur = state.get('current') or {}
    attempt = Path(cur.get('attempt_dir') or '.')
    if attempt.parent != runner.state_dir / 'attempts' or attempt.resolve() != attempt:
        raise ContractError('invalid_attempt', 'Current attempt has no owned attempt directory')
    frozen = read_json(attempt / 'item-contract.json', {})
    saved = frozen.get('depends_on') if isinstance(frozen, dict) else None
    if not isinstance(saved, list) or any(not isinstance(dep, str) for dep in saved):
        raise ContractError('missing_dependency_snapshot', 'Current attempt has no saved dependency list')

    current = tracking.resolve(cur.get('item'))
    before = tracking.resolve(predecessor)
    if not before or before[0] != predecessor:
        raise ContractError('invalid_predecessor', 'An existing qualified predecessor ID is required')
    if not current or current[0] == predecessor or tracking.item_done(current[0]):
        raise ContractError('dependency_changed', 'Current item is missing, already accepted or its own predecessor')
    item, feature, _, contract = current
    dependencies = _dependencies(tracking, feature, contract)
    if predecessor not in dependencies or predecessor in _dependencies(tracking, feature, {'depends_on': saved}):
        raise ContractError('dependency_changed', 'Predecessor must be a newly added direct dependency')
    if tracking.item_done(before[0]):
        raise ContractError('predecessor_accepted', 'Predecessor is already completed')

    items = intent['options'].get('items')
    if not items or items != (state.get('options') or {}).get('items') or state.get('goal') != intent['goal']:
        raise ContractError('invalid_selection', 'Handoff requires the current explicit item selection')
    selected = items.split(',')
    resolved = [tracking.resolve(key) for key in selected]
    if any(found is None for found in resolved):
        raise ContractError('invalid_selection', 'The current selection contains a missing item')
    keys = [found[0] for found in resolved]
    if len(keys) != len(set(keys)):
        raise ContractError('invalid_selection', 'The current selection contains duplicate items')
    if item not in keys or predecessor in keys:
        raise ContractError('invalid_selection', 'Selection must include the current item and omit the predecessor')
    selected.insert(keys.index(item), predecessor)
    skipped = {tracking.canonical(key, strict=False) for key in state.get('skipped', [])}
    nxt = tracking.next_item({*keys, predecessor}, skipped)
    if not nxt or tracking.key(nxt[0], nxt[2]) != predecessor:
        raise ContractError('predecessor_blocked', 'Predecessor cannot run next in the expanded queue')
    return {'item': item, 'predecessor': predecessor, 'dependencies': sorted(dependencies),
            'attempt_dir': str(attempt), 'attempt': cur['attempt'], 'items': ','.join(selected),
            'history_offset': len(state.get('history', []))}


def stopped(runner, intent, state):
    from .project_services import departed

    if (intent['desired_state'] != 'stopped' or state.get('stop_requested') is not True
            or state.get('stop_status') != 'stopped'
            or state.get('runner_status') == 'running'):
        raise ContractError('execution_unsettled', 'Explicitly stop the current attempt and wait for the runner to exit')
    startup = read_json(runner.state_dir / 'startup.json', {})
    launch = read_json(runner.state_dir / 'launch.json', {})
    process = read_json(runner.state_dir / 'runner-process.json', {})
    identity = runner.context.identity()
    if (launch and (launch.get('identity') != identity
                    or not launch.get('nonce') or startup.get('nonce') != launch['nonce']
                    or startup.get('status') != 'idle'
                    or not startup.get('close_evidence') or startup.get('cleanup_error'))
            or process and (process.get('nonce') != launch.get('nonce')
                            or process.get('identity') != identity or not departed(process))
            or state.get('mailbox') and not state.get('mailbox_close_evidence')):
        raise ContractError('execution_unsettled', 'Runner exit and terminal cleanup are unconfirmed')
    cur = state['current']
    registry = runner.launcher.registry(cur['attempt_dir'])
    agents = registry['agents']
    by_id = {key: a for a in agents for key in (a.get('id'), a.get('dispatch_id')) if key}
    if (registry.get('fenced') is not True or registry.get('stop_status') != 'stopped'
            or registry.get('recovery_issues')
            or cur.get('dispatch_id') and not any(a.get('dispatch_id') == cur['dispatch_id']
                                                 and a.get('role') == 'coordinator' for a in agents)
            or any(a.get('cleanup_error') or not _agent_closed(a, by_id) for a in agents)
            or any(r.get('status') != 'closed' or r.get('error') for r in registry['runs'])
            or any(c.get('status') not in ('completed', 'stopped') for c in registry.get('graph_calls', []))):
        raise ContractError('execution_unsettled', 'Attempt assignments or resources are not confirmed closed')
    for path in (Path(cur['attempt_dir']) / 'verification').glob('*.json'):
        record = read_json(path, {})
        if isinstance(record, dict) and record.get('attempt') == cur['attempt_dir'] and (
                record.get('status') == 'running' or record.get('cleanup_error')
                or (record.get('resource') or {}).get('status') == 'cleanup_required'):
            raise ContractError('execution_unsettled', 'Attempt verification resource cleanup is unconfirmed')


def _agent_closed(agent, by_id):
    visited = set()
    while agent.get('status') == 'superseded' and not agent.get('terminal_closed'):
        successor = agent.get('superseded_by')
        if not successor or successor in visited:
            return False
        visited.add(successor)
        replacement = by_id.get(successor, {})
        if (not agent.get('terminal') or replacement.get('terminal') != agent['terminal']
                or replacement.get('incarnation') != agent.get('incarnation')):
            return False
        agent = replacement
    return (agent.get('status') in ('stopped', 'released', 'superseded')
            and not agent.get('error')
            and (agent.get('terminal_closed') or not agent.get('terminal_owned')))


def check_controls(runner, state):
    from .runner_operations import UncertainOperation, check_escalation_receipt

    if state.get('control_uncertain') or state.get('recovery_issues'):
        raise ContractError('unknown_effect', 'Resolve pending controls and recovery issues first')
    agents = runner.launcher.registry(state['current']['attempt_dir'])['agents']
    effects = read_json(runner.state_dir / 'operations.json', {})
    for path in (runner.state_dir / 'commands').glob('*.json'):
        command = read_json(path, {})
        status, identifier = command.get('status'), command.get('id')
        if status not in ('pending', 'applying', 'running', 'unknown'):
            continue
        if status == 'unknown':
            # A reconciliation receipt can remain immutable after its findings
            # are superseded by newer observations in the effect journals.
            if identifier in state.get('applied_commands', []) or command.get('action') == 'reconcile':
                continue
            if command.get('action') == 'resolve-escalation':
                effect = effects.get('escalation-resume:' + str(identifier), {})
                if (effect.get('status') == 'completed'
                        and (effect.get('evidence') or {}).get('scope') == command.get('guard')
                        and any(a.get('role') == 'coordinator' and a.get('follow_up_operation') == identifier
                                and a.get('dispatch_id') == effect.get('confirmed_follow_up') for a in agents)):
                    try:
                        check_escalation_receipt(effect, effect['result'])
                    except (UncertainOperation, KeyError, IndexError, TypeError):
                        pass
                    else:
                        continue
        raise ContractError('unknown_effect', 'Resolve pending controls and recovery issues first')


def defer(state, handoff, identifier):
    after = copy.deepcopy(state)
    cur = after.pop('current')
    after.setdefault('attempts', []).append({
        **cur, 'outcome': 'deferred', 'dependency_handoff': identifier,
        'predecessor': handoff['predecessor'],
    })
    after.setdefault('deferred_items', {})[handoff['item']] = handoff
    after.update(current=None, paused=True)
    after['options']['items'] = handoff['items']
    return after


def check_queue(tracking, state):
    for key, handoff in state.get('deferred_items', {}).items():
        found = tracking.resolve(key)
        if not found or _dependencies(tracking, found[1], found[3]) != set(handoff['dependencies']):
            raise ContractError('dependency_changed', f'{key}: dependencies changed after handoff; inspect Tasks.json')


def require_predecessor(tracking, state, item):
    handoff = state.get('deferred_items', {}).get(item)
    if not handoff:
        return
    check_queue(tracking, state)
    predecessor = handoff['predecessor']
    if not tracking.acceptance(predecessor, state['history'][handoff['history_offset']:]):
        raise ValueError(f'{item}: predecessor {predecessor} requires runner acceptance after handoff')
