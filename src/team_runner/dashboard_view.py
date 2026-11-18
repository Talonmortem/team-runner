"""Dashboard projections and exact terminal selection."""
from pathlib import Path
import subprocess
import time

from . import runner_config, team_graph
from .application import _check_identity, require_scope
from .event_scope import pending
from .network_recovery import view as network_view
from .protocol import ContractError, attempt_identity, operation, summary
from .runner_store import read_json
from .verification import view as verification
from .tracking import Tracking

_SCOPE = {'identity', 'generation', 'attempt', 'revision'}
_FINISHED = ('released', 'stopped', 'superseded')
_READ_ERRORS = (ValueError, OSError, RuntimeError, subprocess.SubprocessError)


def _transport(runner, *args, **kwargs):
    return runner.launcher.orca(*args, '--environment', 'local', timeout=2, **kwargs)


def _workspace(runner):
    return runner.context.verify(lambda *args: _transport(runner, *args))


def _registry(runner, current):
    attempt = current.get('attempt_dir')
    if attempt:
        runner.context.check_attempt(attempt)
        return Path(attempt), runner.launcher.registry(attempt)
    return None, {'agents': [], 'graph_calls': []}


def _agent_view(record, waiting, confirmed):
    proof = record.get('working_evidence') or {}
    stamp = proof.get('at')
    if record.get('status') in _FINISHED or record.get('superseded_by'):
        state = 'finished'
    elif waiting or record.get('status') == 'waiting_child':
        state = 'waiting'
    elif (confirmed and record.get('status') == 'active' and record.get('activity') == 'working'
          and proof.get('source') == 'exact fresh agent_status'
          and proof.get('dispatch') == record.get('dispatch_id')
          and type(stamp) in (int, float) and 0 <= time.time() - stamp <= 30):
        state = 'working'
    else:
        state = 'unconfirmed'
    fields = ('id', 'node_id', 'role', 'status', 'activity', 'dispatch_id', 'run_id', 'task_id',
              'terminal', 'incarnation', 'requested', 'observed')
    return {**{k: record.get(k) for k in fields}, 'state': state,
            'can_focus': bool(record.get('terminal_owned') and not record.get('terminal_closed')
                              and not record.get('superseded_by') and state != 'finished'
                              and all(record.get(k) for k in ('id', 'terminal', 'incarnation',
                                                              'dispatch_id', 'task_id', 'run_id')))}


def _commands(runner):
    paths = [p for name in ('commands', 'ui/commands')
             for p in (runner.state_dir / name).glob('*.json')]
    records = []
    for path in sorted(paths, key=lambda p: p.stat().st_mtime_ns, reverse=True)[:30]:
        try:
            row = operation(runner, path.stem)
            records.append({key: row.get(key) for key in ('request_id', 'action', 'status', 'error')})
        except ContractError as exc:
            records.append({'request_id': path.stem, 'status': 'unconfirmed', 'error': exc.as_dict()})
    return records


def _operations(runner, state, attempt):
    paths = [runner.state_dir / 'operations.json', runner.launcher.journal(runner.state_dir / 'startup')]
    if attempt:
        paths.append(runner.launcher.journal(attempt))
    if state.get('mailbox_dir'):
        paths.append(runner.launcher.journal(state['mailbox_dir']))
    return [{'key': key, 'status': row.get('status'), 'stage': row.get('stage'),
             'request_id': row.get('request_id'), 'operation': row.get('args', [])[:2],
             'error': row.get('error')}
            for path in paths for key, row in read_json(path, {}).items()
            if row.get('status') != 'completed']


def _future(runner, state):
    try:
        options = state.get('options') or {}
        overrides = {key: options[key] for key in runner_config.FIELDS | runner_config.OPTIONAL_FIELDS
                     if options.get(key) is not None}
        config = runner.load_config(options.get('config'), overrides, allow_unavailable_fast=True)
        return {'profile': runner.context.profile, 'graph': team_graph.migrate(config)}
    except _READ_ERRORS as exc:
        return {'profile': runner.context.profile, 'error': str(exc)}


def view(runner, services, observer, *, enable_actions=False):
    state = runner.load_state()
    current = state.get('current') or {}
    status = summary(runner)
    attempt, registry = _registry(runner, current)
    snapshot = read_json(attempt / 'config.json', {}) if attempt else {}
    graph = snapshot if snapshot.get('version') == 3 else None
    rows = registry.get('agents', [])
    if not attempt and current.get('dispatch_id'):
        rows = [{'id': current['dispatch_id'], 'role': 'coordinator', 'dispatch_id': current['dispatch_id'],
                 'terminal': current.get('terminal'), 'status': 'unknown'}]
    messages = pending(current, registry) if attempt else []
    resource_waits = list(current.get('resource_waits', {}).values())
    waits = {m['classification']['dispatch'] for m in messages}
    waits.update(row.get('dispatch') for row in resource_waits if row.get('status') in ('waiting', 'notified'))
    commands = _commands(runner)
    operations = _operations(runner, state, attempt)
    checks = runner.attempt_profile()['checks']
    goal = status.get('goal')
    item = current.get('item')
    tracking_dir = (runner.repo / goal).resolve() if goal else None
    task = (Tracking(tracking_dir).find(item) if item and tracking_dir
            and (tracking_dir / 'Tasks.json').is_file() else None)
    check_scope = task[2].get('check_scope', 'item') if task else 'item'
    checks_view = verification(attempt, checks, check_scope)
    network = network_view(runner)
    confirm = (status['confirmed'] and status['current']['attempt'] == attempt_identity(state)
               and state == runner.load_state()
               and (not attempt or registry == runner.launcher.registry(attempt)))
    if not confirm:
        status = {**status, 'observed_state': 'unconfirmed', 'confirmed': False, 'available_actions': [],
                  'blocker': {'code': 'observation_changed', 'message': 'State changed during observation'}}
    # A checkout identity mismatch can also explain a failed Orca probe.
    try:
        _workspace(runner)
        orca = {'state': 'available', 'confirmed': True}
    except _READ_ERRORS as exc:
        orca = {'state': 'unavailable', 'confirmed': False, 'error': str(exc)}
    can_confirm_work = confirm and orca['confirmed'] and not registry.get('fenced')
    agents = [_agent_view(a, a.get('dispatch_id') in waits, can_confirm_work) for a in rows]
    for agent in agents:
        agent['can_focus'] = agent['can_focus'] and confirm and orca['confirmed']
    observer_status = observer.status()
    observer_status['state'] = observer_status['process']['state']
    return {'summary': status, 'graph': graph, 'agents': agents,
            'legacy_roles': [] if graph else sorted(set(snapshot.get('roles', {})) |
                                                 {a['role'] for a in agents if a.get('role')}),
            'future': _future(runner, state), 'enable_actions': enable_actions,
            'messages': [{'id': m.get('id'), 'type': m['type'],
                          'agent': m['classification']['agent'], 'dispatch': m['classification']['dispatch'],
                          'text': m.get('text') or m.get('body') or ''} for m in messages],
            'diagnostics': {'orca': orca,
                            'service': services.status(runner.context.project_id, read_only=True),
                            'observer': observer_status,
                            'waits': {'resources': resource_waits, 'recovery': status['recovery'],
                                      'graph_calls': registry.get('graph_calls', []), 'network': network},
                            'verification': checks_view,
                            'commands': commands, 'operations': operations}}


def focus(runner, data):
    if set(data) != {'scope', 'agent_id', 'dispatch_id', 'terminal'}:
        raise ValueError('Expected scope, agent_id, dispatch_id and terminal')

    scope = data['scope']
    if not isinstance(scope, dict) or set(scope) != _SCOPE:
        raise ContractError('invalid_scope', 'Terminal selection requires the current workspace scope')

    def selected():
        _check_identity(runner, scope['identity'])
        now = summary(runner)
        require_scope(now, scope)
        current = runner.load_state().get('current') or {}
        attempt, registry = _registry(runner, current)
        if not attempt or str(attempt) != now['current']['attempt']:
            raise ContractError('stale_scope', 'Current attempt changed')
        matches = [a for a in registry['agents'] if a.get('id') == data['agent_id']
                   and a.get('dispatch_id') == data['dispatch_id'] and a.get('terminal') == data['terminal']]
        if len(matches) != 1 or not _agent_view(matches[0], False, False)['can_focus']:
            raise ContractError('identity_mismatch', 'Terminal is no longer owned by the selected Dispatch')
        return matches[0]

    record = selected()
    _workspace(runner)
    handle = record['terminal']
    mailbox = record.get('mailbox')
    result = _transport(runner, 'orchestration', 'worker-show', '--dispatch', record['dispatch_id'], mailbox=mailbox)
    dispatch = result.get('dispatch') or {}
    if (result.get('observation', {}).get('exactWorker') is not True
            or any(dispatch.get(k) != record[v] for k, v in (
                ('id', 'dispatch_id'), ('runId', 'run_id'), ('taskId', 'task_id')))):
        raise ContractError('identity_mismatch', 'Orca did not confirm the exact Dispatch')
    runner.launcher.verify_terminal(record, result)
    terminal = _transport(runner, 'terminal', 'show', '--terminal', handle, mailbox=mailbox).get('terminal')
    runner.launcher.verify_terminal(record, {**result, 'terminal': terminal})
    if selected() != record:
        raise ContractError('stale_scope', 'Terminal registration changed during verification')
    try:
        _transport(runner, 'terminal', 'switch', '--terminal', handle, mailbox=mailbox)
    except _READ_ERRORS:
        return {'status': 'unknown'}
    return {'status': 'opened'}
