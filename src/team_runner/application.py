"""Transport-independent workspace queries and command dispatch.

Legacy HTTP names remain aliases; command records retain their existing schema.
"""
from pathlib import Path
from .protocol import ContractError, summary, operation
from . import queue_plan
from .tracking import for_goal
from . import runner_config as config
from .runner_store import atomic_json, enqueue, lock, read_json, running


def goals(runner, *, profile=None):
    repo = runner.repo
    result = []
    profile = runner.attempt_profile() if profile is None else profile
    root = repo / profile['tracking_root']
    for directory in sorted(root.glob('*/')):
        if not directory.is_dir() or not directory.resolve().is_relative_to(root.resolve()):
            continue
        name = str(directory.relative_to(repo))
        if ((directory / queue_plan.NAME).is_file() or queue_plan.snapshot_path(runner, name).is_file()):
            result.append(name)
            continue
        data = read_json(directory / 'Tasks.json', {})
        if data.get('kind') == 'goal' or any('path' in q for q in data.get('queue', [])):
            result.append(name)
    return result


def tail(path, limit=16000):
    if not path.exists():
        return ''
    with path.open('rb') as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - limit))
        return f.read(limit).decode('utf-8', errors='replace')


def config_path(runner):
    selected = runner.load_state().get('options', {}).get('config')
    return Path(selected).expanduser().resolve() if selected else runner.context.settings_dir / 'config.json'


def state_view(runner):
    from .recovery import recovery_view
    from .memory_admission import view as memory_view
    from .runner_operations import rejected_reply
    st = runner.load_state()
    cur = st.get('current') or {}
    profile = runner.attempt_profile(snapshot=st)
    agents = runner.launcher.registry(cur['attempt_dir']) if cur.get('attempt_dir') else {
        'agents': ([{'role': 'coordinator', 'dispatch_id': cur.get('dispatch_id'),
                     'terminal': cur.get('terminal'), 'requested': None, 'observed': None}]
                   if cur.get('dispatch_id') else []), 'runs': []}
    from .runner_metrics import annotate_agents
    annotate_agents(runner.repo, agents['agents'])
    queue = []
    if st.get('goal') in goals(runner, profile=profile):
        tracking = for_goal(runner, st['goal'], st)
        active_key = tracking.canonical(cur.get('item'), strict=False)
        skipped = {tracking.canonical(key, strict=False) for key in st.get('skipped', [])}
        for key, _, _, item in tracking.items():
            status = tracking.item_status(key)
            if status == 'pending':
                if key == active_key:
                    status = 'failed' if cur.get('failed') else 'in_progress'
                elif key in skipped:
                    status = 'skipped'
            queue.append({**item, 'key': key, 'status': status})
    error = st.get('stop_error') or ('stop unconfirmed; inspect Orca resources' if st.get('stop_status') == 'unknown' else None)
    error = (error or st.get('mailbox_cleanup_error')
             or read_json(runner.state_dir / 'startup.json', {}).get('cleanup_error')
             or st.get('last_error') or ('; '.join(st.get('recovery_issues') or [])) or None)
    if not error and cur.get('failed'):
        error = cur.get('acceptance_rework_error') or '; '.join(cur.get('problems') or []) or 'attempt failed'
    operations = [{**op, 'key': key} for path in [runner.state_dir / 'operations.json',
                  runner.launcher.journal(runner.state_dir / 'startup'),
                  runner.launcher.journal(st['mailbox_dir']) if st.get('mailbox_dir') else runner.state_dir / 'unused.json',
                  runner.launcher.journal(cur['attempt_dir']) if cur.get('attempt_dir') else runner.state_dir / 'unused.json']
                  for key, op in read_json(path, {}).items() if op['status'] != 'completed']
    from .coordinator_events import view as events_view
    from .verification import view as verification_view
    active = next((item for item in queue if cur.get('item') == item['key']), {})
    from .event_scope import messages as classified_messages
    from .autonomy import view as autonomy_view
    st = dict(st, current=dict(cur, messages=classified_messages(cur, agents))) if cur else st
    from .state_time import view as time_view
    from .network_recovery import view as network_view
    memory = memory_view(runner.state_dir, st)
    return {'waiting_memory': memory, 'time': time_view(runner), 'network': network_view(runner), 'autonomy': autonomy_view(runner), 'events': events_view(cur.get('attempt_dir')),
            'verification': verification_view(cur.get('attempt_dir'), profile['checks'], active.get('check_scope', 'item')),
            'state': st, 'health': {'state': 'error' if error else 'waiting_memory' if memory else 'ok', 'error': error},
            'recovery': recovery_view(runner),
            'resources': agents, 'queue': queue, 'running': running(runner.state_dir),
            'settings': config.projections(runner.context.settings_dir, st, agents, runner.context.role_layers,
                                           repo=runner.repo, config_root=runner.context.config_root.parent),
            'operations': [op for op in operations if op['status'] != 'rejected' and not rejected_reply(op)],
            'operation_history': [op for op in operations if op['status'] == 'rejected' or rejected_reply(op)],
            'startup': read_json(runner.state_dir / 'startup.json', {}),
            'commands': [read_json(p) for p in sorted((runner.state_dir / 'commands').glob('*.json'),
                        key=lambda p: p.stat().st_mtime_ns)[-20:]], 'log': tail(runner.log_file)}


def legacy_status_view(runner):
    st = runner.load_state()
    cur = st.get("current") or {}
    from .memory_admission import view as memory_view
    result = dict(st, runner_running=running(runner.state_dir), waiting_memory=memory_view(runner.state_dir, st))
    result["resources"] = runner.launcher.registry(cur["attempt_dir"]) if cur.get("attempt_dir") else None
    from .runner_metrics import annotate_agents
    annotate_agents(runner.repo, (result['resources'] or {}).get('agents', []))
    if st.get('goal') in goals(runner):
        result['queue_status'] = for_goal(runner, st['goal'], st).status_index()
    result['settings'] = config.projections(runner.context.settings_dir, st, result['resources'] or {}, runner.context.role_layers,
                                           repo=runner.context.repo, config_root=runner.context.config_root.parent)
    paths = [runner.state_dir / 'operations.json'] + ([runner.launcher.journal(cur['attempt_dir'])] if cur.get('attempt_dir') else [])
    result['operations'] = [{**o, 'key': k} for p in paths for k, o in read_json(p, {}).items() if o['status'] != 'completed']
    return result


def start_options(runner, data, state=None, *, queue=None):
    """Validate explicit launch policy without creating intent or processes."""
    if not isinstance(data, dict):
        raise ValueError('start options must be an object')
    state = runner.load_state() if state is None else state
    _execution_scope(runner, state)
    if 'resume' in data:
        if data != {'resume': True} or type(data['resume']) is not bool:
            raise ValueError('resume uses saved options')
        options = dict(state.get('options') or {})
        if not options:
            raise ValueError('no saved run options')
    else:
        allowed = {'goal', 'items', 'mode', 'final', 'config', 'agent', 'provider', 'profile', 'model', 'effort', 'handoff', 'fast'}
        if set(data) - allowed:
            raise ValueError('unknown start option')
        options = dict(data)
    if options.get('fast') is None:
        options.pop('fast', None)
    goal = options.get('goal')
    if not isinstance(goal, str) or goal not in goals(runner):
        raise ValueError('select an existing goal')
    if state.get('goal') and state['goal'] != goal and (state.get('current') or state.get('control_uncertain')):
        raise ValueError('workspace already has a goal; finish/archive it first')
    if options.get('mode', 'confirm') not in ('auto', 'confirm'):
        raise ValueError('invalid mode')
    if 'fast' in options and options['fast'] is not None and type(options['fast']) is not bool:
        raise ValueError('fast must be boolean')
    if 'final' in options and type(options['final']) is not bool:
        raise ValueError('final must be boolean')
    items = options.get('items')
    if items is not None and not isinstance(items, str):
        raise ValueError('unknown item filter')
    tracking = queue if queue is not None else for_goal(runner, goal, state)
    if any(tracking.resolve(i) is None for i in (items or '').split(',') if i):
        raise ValueError('unknown item filter')
    runner.load_config(options.get('config'), {k: options.get(k) for k in ('agent', 'profile', 'provider', 'model', 'effort', 'fast')})
    return options


def execute(runner, data, *, services=None, start=None, manager=None):
    """Shared execute transport contract; acceptance is not goal completion."""
    import os
    from . import goal_execute
    from .protocol import request_id
    if os.environ.get('TEAM_RUNNER_ATTEMPT'):
        raise ContractError('child_lifecycle_forbidden', 'Child attempts cannot execute goals')
    if (not isinstance(data, dict) or not {'request_id', 'options'} <= set(data)
            or set(data) - {'request_id', 'options', 'generation', 'epoch', 'scope'}):
        raise ContractError('invalid_payload', 'Expected request_id, options and optional generation/epoch')
    identifier = request_id(data['request_id'])
    generation, epoch = data.get('generation'), data.get('epoch')
    if generation is not None:
        request_id(generation)
    if ((generation is None) != (epoch is None)
            or epoch is not None and (type(epoch) is not int or epoch < 1)):
        raise ContractError('invalid_scope', 'generation and positive integer epoch must be supplied together')
    options = data['options']
    allowed = {'goal', 'items', 'mode', 'final', 'config', 'agent', 'provider', 'profile',
               'model', 'effort', 'handoff', 'fast', 'recovery'}
    if not isinstance(options, dict):
        raise ContractError('invalid_payload', 'Execute options must be an object')
    if 'resume' in options:
        if options != {'resume': True} or type(options['resume']) is not bool:
            raise ContractError('invalid_payload', 'resume uses saved options only')
    else:
        if set(options) - allowed or not isinstance(options.get('goal'), str):
            raise ContractError('invalid_payload', 'Execute requires a goal and supported options')
        for key, value in options.items():
            valid = type(value) is bool if key in ('fast', 'final', 'recovery') else isinstance(value, str)
            if not valid:
                raise ContractError('invalid_payload', 'Invalid execute option: ' + key)
        if options.get('mode', 'auto') not in ('auto', 'confirm'):
            raise ContractError('invalid_payload', 'Invalid execute mode')
    scope = data.get('scope')
    if 'scope' in data:
        if (options != {'resume': True} or not isinstance(scope, dict)
                or set(scope) != {'identity', 'generation', 'attempt', 'revision'}
                or scope['generation'] != generation):
            raise ContractError('invalid_scope', 'Guarded resume requires saved options and current scope')
        _check_identity(runner, scope['identity'])
    _check_identity(runner, runner.context.identity())
    try:
        return goal_execute.execute(runner, options, identifier, generation=generation, epoch=epoch,
                                    services=services, start=start, manager=manager, scope=scope)
    except ContractError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise ContractError('invalid_request', str(exc)) from None


def configuration(runner):
    """Full editable configuration and diagnostics shared by transports."""
    from .team_graph import migrate
    return {'config': runner.load_config(runner.load_state().get('options', {}).get('config'),
                                          allow_unavailable_fast=True),
            'graph': migrate(
                runner.load_config(runner.load_state().get('options', {}).get('config'), allow_unavailable_fast=True)),
            'source': str(config_path(runner)), 'defaults': config.defaults(),
            'templates': [{**item, 'config': config.get_template(item['id']),
                           'available': item['id'] != 'cloudru' or config.sber_allowed(runner.repo)}
                          for item in config.templates()],
            'connections': config.connections(repo=runner.repo, config_root=runner.context.config_root.parent),
            'goals': goals(runner), 'projections': state_view(runner)['settings']}


def configuration_capabilities(runner, data):
    """Resolve a draft role's capabilities without saving or launching anything."""
    if (not isinstance(data, dict) or not config.FIELDS <= set(data)
            or set(data) - config.FIELDS - config.OPTIONAL_FIELDS
            or any(not isinstance(data[key], str) or len(data[key]) > 512 for key in config.FIELDS)):
        raise ValueError('invalid role settings')
    catalog = config.connections(repo=runner.repo, config_root=runner.context.config_root.parent)
    return {'fast': config.fast_supported(data, catalog)}


def write_configuration(runner, data):
    """Write workspace settings only; active attempt snapshots remain immutable."""
    if not isinstance(data, dict):
        raise ValueError('configuration must be an object')
    with lock(runner.state_dir / 'config.lock'):
        if data.get('reset') is True:
            if set(data) != {'reset'}:
                raise ValueError('reset does not accept configuration fields')
            value = config.validate(config.defaults(), repo=runner.repo,
                                    config_root=runner.context.config_root.parent)
        elif data.get('version') in (2, 3):
            value = config.validate(data, repo=runner.repo, config_root=runner.context.config_root.parent)
        else:
            value = config.settings_update(data, runner.load_config(runner.load_state().get('options', {}).get('config'),
                                                                         allow_unavailable_fast=True),
                                           repo=runner.repo, config_root=runner.context.config_root.parent)
        atomic_json(config_path(runner), value)
    return value


CONTROL_ACTIONS = frozenset(('pause', 'stop', 'retry', 'resolve', 'reply',
                             'reconcile', 'continue-operation', 'autonomy', 'resolve-escalation'))


def _submit_command(runner, action, data, request_id):
    """One dispatch path for CLI and HTTP, with durable request deduplication."""
    if not request_id:
        raise ValueError('request_id required')
    data = dict(data)
    _execution_scope(runner, runner.load_state())  # Refuse mutations of unsupported intent versions.
    if action == 'autonomy':
        raise ContractError('invalid_scope', 'Autonomy requires a versioned command with scope/revision')
    if action == 'resolve-escalation':
        raise ContractError('invalid_scope', 'Escalation resolution requires a versioned command with scope/revision')
    if action in CONTROL_ACTIONS:
        return runner.submit_control(action, data, request_id)
    if action not in ('start', 'focus', 'configure', 'recovery'):
        raise ValueError('unknown operation')
    with lock(runner.state_dir / 'ui-actions.lock'):
        record = enqueue(runner.state_dir / 'ui', action, data, request_id)
        path = runner.state_dir / 'ui/commands' / (record['id'] + '.json')
        if record['status'] == 'pending':
            record['status'] = 'applying'
            atomic_json(path, record)
            try:
                if action == 'start':
                    from .service import start_runner
                    record['result'] = start_runner(runner, data)
                elif action == 'recovery':
                    _control_payload(action, data)
                    from .recovery import configure
                    record['result'] = configure(runner, data['enabled'])
                elif action == 'configure':
                    record['result'] = write_configuration(runner, data)
                else:
                    agents = state_view(runner)['resources']['agents']
                    if not any(a.get('terminal') == data.get('terminal') for a in agents):
                        raise ValueError('terminal does not belong to current attempt')
                    record['result'] = runner.orca('terminal', 'switch', '--terminal', data['terminal'])
                record['status'] = 'done'
            except Exception as exc:
                uncertain = isinstance(exc, (OSError, RuntimeError)) or read_json(runner.state_dir / 'startup.json', {}).get('status') == 'unknown'
                record.update(status='unknown' if uncertain else 'error', error=str(exc))
            atomic_json(path, record)
        return record


def _control_payload(action, payload, *, journaled=False):
    """Reject transport-only/internal fields before a command reaches the owner."""
    if action == 'autonomy':
        if (not isinstance(payload, dict) or set(payload) - {'enabled', 'unlimited_passes'}
                or type(payload.get('enabled')) is not bool
                or type(payload.get('unlimited_passes', False)) is not bool
                or (payload.get('unlimited_passes') and not payload['enabled'])):
            raise ContractError('invalid_payload', 'enabled boolean required; unlimited_passes requires enabled autonomy')
        return
    if action == 'recovery':
        if not isinstance(payload, dict) or set(payload) != {'enabled'} or type(payload['enabled']) is not bool:
            raise ContractError('invalid_payload', 'enabled boolean required')
        return
    if action == 'configure':
        if not isinstance(payload, dict):
            raise ContractError('invalid_payload', 'Configuration must be an object')
        return
    fields = {'pause': set(), 'stop': set(), 'reconcile': set(),
              'retry': {'force', 'refresh', 'refresh_context', 'allow_extra_attempt', 'reset_attempts', 'reset_reason', 'saved_settings'},
              'reply': {'message_id', 'text', 'run_id'}, 'resolve': {'choice'},
              'resolve-escalation': {'message_id', 'text', 'run_id', 'resume'},
              'continue-operation': {'operation_id'}}
    if not isinstance(action, str) or action not in fields:
        raise ContractError('unknown_action', 'Unsupported command action')
    if not isinstance(payload, dict) or set(payload) - fields[action]:
        raise ContractError('invalid_payload', 'Unknown command fields')
    if action == 'retry' and any(type(v) is not bool for k, v in payload.items() if k != 'reset_reason'):
        raise ContractError('invalid_payload', 'Retry options must be booleans')
    if action == 'retry' and 'reset_reason' in payload and (
            payload.get('reset_attempts') is not True or payload['reset_reason'] not in ('infrastructure', 'task', 'other')):
        raise ContractError('invalid_payload', 'reset_reason requires reset_attempts and must be infrastructure, task or other')
    if action == 'retry' and payload.get('saved_settings') and not payload.get('refresh'):
        raise ContractError('invalid_payload', 'saved settings require explicit refresh')
    if action in ('reply', 'resolve-escalation') and (not isinstance(payload.get('message_id'), str) or not payload['message_id']
            or not isinstance(payload.get('text'), str) or not payload['text'].strip() or len(payload['text']) > 32000):
        raise ContractError('invalid_payload', 'Reply requires message_id and 1..32000 characters')
    if action in ('reply', 'resolve-escalation') and 'run_id' in payload and (not isinstance(payload['run_id'], str)
            or not payload['run_id'] or len(payload['run_id']) > 512):
        raise ContractError('invalid_payload', 'run_id must be a nonempty run identifier')
    if (action == 'resolve-escalation' and ('resume' in payload or not journaled)
            and type(payload.get('resume')) is not bool):
        raise ContractError('invalid_payload', 'An explicit boolean resume is required')
    if action == 'resolve' and payload.get('choice') not in ('start', 'skip', 'stop'):
        raise ContractError('invalid_payload', 'Resolution must be start, skip or stop')
    if action == 'continue-operation' and (not isinstance(payload.get('operation_id'), str) or not payload['operation_id']):
        raise ContractError('invalid_payload', 'operation_id required')


def _execution_scope(runner, state):
    from .protocol import attempt_identity
    from .goal_intent import read
    intent = read(runner)
    return {'generation': intent.get('generation'), 'epoch': intent.get('epoch'), 'attempt': attempt_identity(state),
            'goal': state.get('goal'), 'run_id': state.get('run_id')}


def _check_identity(runner, identity):
    from .catalog import Catalog
    ctx = runner.context
    if identity != ctx.identity() or Catalog(ctx.config_root, ctx.state_root).context(
            ctx.project_id, ctx.workspace_id).identity() != identity:
        raise ContractError('identity_mismatch', 'Command belongs to another workspace')


def require_scope(current, scope):
    if scope.get('identity') != current['identity']:
        raise ContractError('identity_mismatch', 'Command belongs to another workspace')
    if not current['confirmed'] or scope.get('revision') != current['revision']:
        raise ContractError('stale_revision', 'Refresh status before issuing a new command')
    if scope.get('generation') != current['generation'] or scope.get('attempt') != current['current']['attempt']:
        raise ContractError('stale_scope', 'Command belongs to another generation or attempt')


def _submit_v1(runner, command, *, explicit_stop=False):
    """Durably accept a guarded control command; only the runner owner applies it."""
    from .protocol import version, request_id
    import json
    import os
    version(command)
    if set(command) != {'schema_version', 'request_id', 'action', 'scope', 'payload'}:
        raise ContractError('invalid_command', 'Expected schema_version, request_id, action, scope, payload')
    # Copy and validate JSON before retaining anything supplied by a caller.
    try:
        command = json.loads(json.dumps(command, allow_nan=False))
    except (TypeError, ValueError):
        raise ContractError('invalid_command', 'Command must contain finite JSON values') from None
    identifier = request_id(command['request_id'])
    scope = command['scope']
    if not isinstance(scope, dict) or set(scope) != {'identity', 'generation', 'attempt', 'revision'}:
        raise ContractError('invalid_scope', 'Expected identity, generation, attempt and revision')
    if os.environ.get('TEAM_RUNNER_ATTEMPT') and command['action'] != 'reply':
        raise ContractError('child_lifecycle_forbidden', 'Child attempts cannot control goal lifecycle')
    _check_identity(runner, scope['identity'])
    path = runner.state_dir / 'commands' / (identifier + '.json')
    with lock(runner.state_dir / 'commands.lock'):
        previous = read_json(path)
        if previous:
            _control_payload(command['action'], command['payload'], journaled=True)
            if previous.get('schema_version') != 1 or previous.get('command') != command:
                raise ContractError('request_conflict', 'request_id already used for another command')
            return operation(runner, identifier)
        if (runner.state_dir / 'ui/commands' / (identifier + '.json')).exists():
            raise ContractError('request_conflict', 'request_id already used in legacy UI journal')
        _control_payload(command['action'], command['payload'])
        current = summary(runner)
        require_scope(current, scope)
        if (command['action'] == 'resolve-escalation' and command['payload']['resume']
                and (current.get('blocker') or {}).get('code') == 'unknown_effect'):
            raise ContractError('unknown_effect', 'Reconcile the unconfirmed effect before sending an escalation decision')
        state = runner.load_state()
        guard = _execution_scope(runner, state)
        if guard['generation'] != scope['generation'] or guard['attempt'] != scope['attempt']:
            raise ContractError('stale_scope', 'Execution changed during command submission')
        if os.environ.get('TEAM_RUNNER_ATTEMPT'):
            runner.context.check_attempt(os.environ['TEAM_RUNNER_ATTEMPT'])
        record = {'schema_version': 1, 'id': identifier, 'action': command['action'],
                  'command': command, 'guard': guard, 'status': 'pending', 'result': None, 'error': None}
        if command['action'] == 'autonomy':
            from .autonomy import policy
            record['policy_revision'] = policy(runner)['revision']
        atomic_json(path, record)
    # No nested commands.lock -> runner.lock: the long-lived owner also reads commands.
    if not running(runner.state_dir):
        try:
            with lock(runner.state_dir / 'runner.lock', blocking=False):
                runner.process_controls(runner.load_state(),
                                        explicit_stop_id=identifier if explicit_stop else None)
        except RuntimeError:
            pass  # A live owner will consume the persisted command.
    return operation(runner, identifier)


def process_v1(runner, state, path, record, *, explicit_stop=False):
    """Called only under runner.lock. Terminal receipts are never rewritten."""
    from .protocol import version
    from .memory_admission import WaitingMemory
    from . import runner_operations
    import subprocess
    version(record)
    if record['status'] in ('succeeded', 'failed', 'unknown', 'done', 'error'):
        # A pre-upgrade owner can finish a v1 envelope using legacy statuses.
        # Treat its receipt as terminal; never replay or rewrite it.
        return
    if record['status'] in ('running', 'applying'):
        record.update(status='unknown', error=ContractError('interrupted_operation',
                      'Owner interrupted during command; inspect effects before any retry').as_dict())
        state.setdefault('control_uncertain', record['id'])
        runner.save_state(state)
        atomic_json(path, record)
        return
    if record['status'] != 'pending':
        raise ContractError('invalid_operation', 'Unknown operation status')
    if state.get('control_uncertain') and record['action'] not in ('reconcile', 'continue-operation'):
        return
    try:
        command = record['command']
        version(command)
        _control_payload(command['action'], command['payload'], journaled=True)
        _check_identity(runner, command['scope']['identity'])
        if record['guard'] != _execution_scope(runner, state):
            raise ContractError('stale_scope', 'Command belongs to a previous execution')
        if record['action'] == 'autonomy':
            from .autonomy import policy
            if record.get('policy_revision') != policy(runner)['revision']:
                raise ContractError('stale_revision', 'Autonomy policy changed before application')
        # Guard failure has no intent or external effects. Persist running first.
        record['status'] = 'running'
        atomic_json(path, record)
        if record['action'] in ('pause', 'stop', 'retry'):
            from .recovery import set_intent
            set_intent(runner, {'pause': 'paused', 'stop': 'stopped', 'retry': 'paused'}[record['action']])
            if record['action'] == 'stop':
                record['stop_guard'] = _execution_scope(runner, state)
                atomic_json(path, record)
        data = dict(command['payload'], _target=record['guard']['attempt'])
        if explicit_stop and record['action'] == 'stop':
            data['_allow_unmarked_mailbox'] = True
        if record['action'] == 'resolve-escalation':
            data['_scope'] = record['guard']
        if record['action'] == 'autonomy':
            from .autonomy import configure
            result = configure(runner, command['payload']['enabled'], record['id'], expected=record['guard'],
                               unlimited_passes=command['payload'].get('unlimited_passes', False))
            state.setdefault('applied_commands', []).append(record['id'])
            runner.save_state(state)
        elif record['action'] == 'recovery':
            from .recovery import configure
            result = configure(runner, command['payload']['enabled'])
        elif record['action'] == 'configure':
            result = write_configuration(runner, command['payload'])
        else:
            runner.apply_control(state, record['action'], data, record['id'])
            result = {'state': 'ok'}
            if record['action'] == 'retry':
                result['prepared'] = _execution_scope(runner, state)
            if record['action'] == 'resolve-escalation' and data.get('resume') is True:
                result['delivery'] = 'accepted'
        record.update(status='succeeded', result=result, error=None)
    except WaitingMemory as exc:
        record.update(status='pending', result={'state': 'waiting_memory', 'reason': exc.status['reason']}, error=None)
    except ContractError as exc:
        record.update(status='failed', error=exc.as_dict())
    except runner_operations.RejectedOperation as exc:
        runner.save_state(state)
        record.update(status='failed', error=ContractError('command_rejected', str(exc)).as_dict())
    except runner_operations.UncertainOperation as exc:
        state.setdefault('control_uncertain', record['id'])
        runner.save_state(state)
        record.update(status='unknown', error=ContractError('unknown_effect', str(exc)).as_dict())
    except ValueError as exc:
        uncertain = state.get('stop_status') == 'unknown' or state.get('control_uncertain')
        if uncertain:
            state.setdefault('control_uncertain', record['id'])
            runner.save_state(state)
        record.update(status='unknown' if uncertain else 'failed',
                      error=ContractError('unknown_effect' if uncertain else 'command_rejected', str(exc)).as_dict())
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        state.setdefault('control_uncertain', record['id'])
        runner.save_state(state)
        record.update(status='unknown', error=ContractError('unknown_effect', str(exc)).as_dict())
    atomic_json(path, record)


def submit_command(runner, action, data, request_id):
    """Serialize one request ID across legacy and versioned journal namespaces."""
    from .protocol import request_id as validate_id
    identifier = validate_id(request_id)
    with lock(runner.state_dir / 'requests' / (identifier + '.lock')):
        destination = 'commands' if action in CONTROL_ACTIONS else 'ui/commands'
        other = 'ui/commands' if destination == 'commands' else 'commands'
        if (runner.state_dir / other / (identifier + '.json')).exists():
            raise ContractError('request_conflict', 'request_id already used in another command journal')
        return _submit_command(runner, action, data, identifier)


def submit_v1(runner, command, *, explicit_stop=False):
    from .protocol import version, request_id
    version(command)
    identifier = request_id(command.get('request_id'))
    with lock(runner.state_dir / 'requests' / (identifier + '.lock')):
        return _submit_v1(runner, command, explicit_stop=explicit_stop)
