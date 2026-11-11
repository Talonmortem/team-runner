"""Versioned, read-only application projections. No transport or process effects."""
import hashlib
import json
from pathlib import Path
import uuid

from .runner_store import read_json, running

SCHEMA_VERSION = 1


class ContractError(ValueError):
    def __init__(self, code, message, *, details=None, retryable=False):
        super().__init__(message)
        self.code, self.details, self.retryable = code, details or {}, retryable

    def as_dict(self):
        return {'code': self.code, 'message': str(self), 'details': self.details,
                'retryable': self.retryable}


def version(value):
    if not isinstance(value, dict):
        raise ContractError('invalid_document', 'Versioned document must be an object')
    if type(value.get('schema_version')) is not int or value['schema_version'] != SCHEMA_VERSION:
        raise ContractError('unsupported_version', 'Unsupported schema version',
                            details={'supported': [SCHEMA_VERSION], 'received': value.get('schema_version')})


def request_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ContractError('invalid_request_id', 'request_id must be a canonical UUID') from None
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def attempt_identity(state):
    cur = state.get('current') or {}
    return cur.get('attempt_dir') or cur.get('dispatch_id') or cur.get('item')


def _capture(runner):
    """Collect persisted facts twice; a torn observation is explicitly unconfirmed."""
    state = runner.load_state()
    cur = state.get('current') or {}
    paths = {'startup': runner.state_dir / 'startup.json',
             'recovery_settings': runner.state_dir / 'recovery-settings.json',
             'recovery': runner.state_dir / 'recovery.json',
             'controller': runner.state_dir / 'goal-controller.json',
             'effects': runner.state_dir / 'operations.json',
             'startup_effects': runner.launcher.journal(runner.state_dir / 'startup')}
    for key in ('attempt_dir',):
        if cur.get(key):
            paths['attempt_effects'] = runner.launcher.journal(cur[key])
    if state.get('mailbox_dir'):
        paths['mailbox_effects'] = runner.launcher.journal(state['mailbox_dir'])
    values = {name: read_json(path, {}) for name, path in paths.items()}
    from .goal_intent import read as read_intent
    values['intent'] = read_intent(runner)
    from .goal_completion import read as read_completion
    values['completion'] = read_completion(runner)
    values['state'] = state
    values['runner_running'] = running(runner.state_dir)
    values['commands'] = {str(p.relative_to(runner.state_dir)): read_json(p)
                          for directory in ('commands', 'ui/commands')
                          for p in sorted((runner.state_dir / directory).glob('*.json'))}
    tracking = {}
    goal = values['intent'].get('goal') or state.get('goal')
    if goal:
        goal_dir = (runner.repo / goal).resolve()
        tracking_root = (runner.repo / runner.attempt_profile()['tracking_root']).resolve()
        if not goal_dir.is_relative_to(tracking_root):
            raise ContractError('invalid_goal', 'Goal is outside tracking root')
        data = read_json(goal_dir / 'Tasks.json')
        tracking['goal'] = data
        for feature in (data or {}).get('queue', []):
            path = (goal_dir / feature['path'] / 'Tasks.json').resolve()
            if not path.is_relative_to(goal_dir):
                raise ContractError('invalid_goal', 'Feature is outside goal directory')
            tracking[feature['id']] = read_json(path)
    from .catalog import Catalog
    from .service_intent import read as service_permission
    catalog = Catalog(runner.context.config_root, runner.context.state_root)
    values['service_intent'] = service_permission(catalog, runner.context.project_id)
    values['watchdog'] = read_json(runner.context.state_root / 'projects' /
                                  runner.context.project_id / 'service' / 'watchdog.json', {})
    values['tracking'] = tracking
    return values


def control_facts(facts):
    # Healthy polling changes these scheduling fields without changing command safety.
    watchdog = facts.get('watchdog') or {}
    if (watchdog.get('phase') == 'observing' and not watchdog.get('blocker')
            and watchdog.get('attempts') == 0 and watchdog.get('checks') == 0
            and watchdog.get('observed_incarnation') and not watchdog.get('operation_id')):
        return {**facts, 'watchdog': {k: v for k, v in watchdog.items()
                                     if k not in ('incident', 'next_check_at')}}
    return facts


def summary(runner):
    first = control_facts(_capture(runner))
    facts = control_facts(_capture(runner))
    confirmed = first == facts
    state, intent = facts['state'], facts['intent']
    cur = state.get('current') or {}
    recovery_fact = facts['recovery']
    if recovery_fact.get('target') and recovery_fact['target'] != attempt_identity(state):
        recovery_fact = {}  # Keep the old incident as history, not a blocker for a new attempt.
    desired = intent.get('desired_state') or facts['recovery_settings'].get('intent')
    # No intent is authorization to start a legacy attempt.
    if desired is None:
        desired = 'stopped' if state.get('stop_requested') else 'paused'
    if desired not in ('running', 'paused', 'stopped'):
        raise ContractError('invalid_intent', 'Invalid desired state')
    progress = {'total': 0, 'completed': 0, 'missing_features': [], 'incomplete_features': []}
    tracking = facts['tracking']
    for feature in (tracking.get('goal') or {}).get('queue', []):
        data = tracking.get(feature['id'])
        if data is None:
            progress['missing_features'].append(feature['id'])
        queue = (data or {}).get('queue', [])
        from .verification import gate_valid
        accepted = [item.get('status') == 'completed' and gate_valid(runner.repo, item) for item in queue]
        gates = [item for item in queue if item.get('check_scope') == 'stage']
        required = feature.get('stage_gate')
        missing_gate = required and not any(item['id'] == required for item in gates)
        if feature.get('status') != 'completed' or missing_gate or (gates and not all(accepted)):
            progress['incomplete_features'].append(feature['id'])
        progress['total'] += len(queue)
        progress['completed'] += sum(accepted)
    last = (state.get('history') or [{}])[-1]
    final = {'status': 'not_verified', 'attempt': None}
    if last.get('item') == 'GOAL-FINAL-REVIEW':
        final = {'status': 'legacy_unverified' if not last.get('problems') else 'failed',
                 'attempt': last.get('attempt_dir')}
    # Only the runner can emit a current acceptance receipt; legacy history is not one.
    observed = 'running' if facts['runner_running'] else 'idle'
    actions = []
    blocker = None

    def block(code, message, allowed, evidence=None):
        nonlocal observed, actions, blocker
        observed, actions = 'needs_action', allowed
        blocker = {'code': code, 'message': message, 'facts': evidence or {}, 'actions': allowed}

    if not facts['runner_running'] and facts['startup'].get('status') == 'starting':
        observed = 'starting'
    if desired == 'stopped':
        observed = 'stopping' if facts['runner_running'] or state.get('stop_status') != 'stopped' and cur else 'stopped'
    elif desired == 'paused':
        observed = 'pausing' if facts['runner_running'] and cur else 'paused'
    if facts['runner_running']:
        actions = ['pause', 'stop'] if desired == 'running' else ['stop']
    unknown = state.get('control_uncertain') or state.get('stop_status') == 'unknown' or facts['startup'].get('status') == 'unknown'
    from .runner_operations import rejected_reply
    unknown = unknown or any(op.get('status') not in ('completed', 'rejected') and not rejected_reply(op)
                             for key, journal in facts.items() if key.endswith('effects')
                             for op in journal.values())
    if unknown:
        message = 'Inspect and reconcile the unconfirmed effect'
        evidence = {'control_request_id': state.get('control_uncertain')}
        if state.get('stop_status') == 'unknown':
            issues = list(state.get('recovery_issues') or [])
            evidence.update(stop_status='unknown', issues=issues)
            message = ('Не подтверждена остановка попытки. Откройте «Диагностика» → '
                       '«Восстановление» → «Сверить с Orca»: проверка установит состояние ресурсов, '
                       'не запуская задание повторно.')
            if issues:
                message += ' Причина: ' + '; '.join(str(issue) for issue in issues)
        block('unknown_effect', message, ['reconcile'], evidence)
    elif desired == 'stopped':
        if facts['runner_running'] or cur and state.get('stop_status') != 'stopped':
            observed, actions = 'stopping', ['stop']
        else:
            observed, actions = 'stopped', ['retry'] if cur.get('attempt_dir') else ['start'] if not cur else []
    elif cur.get('failed'):
        block('attempt_failed', 'Review the failed attempt before retrying',
              ['retry'] if not facts['runner_running'] and state.get('stop_status') == 'stopped' else ['stop'],
              {'problems': cur.get('problems', [])})
    elif cur.get('gate_id') and not cur.get('gate_resolved') and not cur.get('gate_resolution'):
        block('confirmation_required', 'Choose how to proceed with the next item', ['resolve', 'stop'])
    elif any(m.get('type') == 'question' and not m.get('answered') and not m.get('closed')
             for m in cur.get('messages', [])):
        block('answer_required', 'Answer the pending agent question',
              ['reply', 'pause', 'stop'] if facts['runner_running'] and desired == 'running' else ['reply', 'stop'])
    elif any(m.get('type') == 'escalation' and not m.get('answered') and not m.get('closed')
             for m in cur.get('messages', [])):
        block('escalation_required', 'Review the escalation and agent diagnostics; stop execution if intervention is needed',
              ['stop'])
    elif state.get('recovery_issues') or recovery_fact.get('status') == 'needs_action':
        block('recovery_required', 'Technical recovery needs a decision', ['reconcile'],
              {'issues': state.get('recovery_issues', []), 'reason': recovery_fact.get('reason')})
    elif state.get('last_error'):
        block('runner_error', state['last_error'], ['reconcile'])
    elif not facts['runner_running'] and facts['startup'].get('status') == 'starting' and desired == 'running':
        observed, actions = 'starting', ['stop']
    elif not facts['runner_running'] and state.get('goal') and desired == 'running':
        block('runner_not_running', 'Execution is not currently observed', ['start'])
    elif not facts['runner_running'] and not cur:
        actions = ['start']
    elif not facts['runner_running'] and desired == 'paused' and not state.get('stop_requested'):
        actions = ['start', 'stop']
    completion = facts['completion']
    controller = facts['controller']
    controller_current = (intent and controller.get('scope') ==
                          {k: intent[k] for k in ('generation', 'epoch')}
                          and controller.get('identity') == runner.context.identity())
    if completion and not facts['runner_running'] and not unknown:
        observed, actions, blocker = completion['status'], [], None
        final = {'status': 'accepted' if completion['final_accepted'] else 'not_required',
                 'attempt': last.get('attempt_dir') if completion['final_accepted'] else None}
    elif (controller_current and not cur and desired == 'running' and not facts['runner_running']
          and not unknown and (blocker is None or blocker['code'] == 'runner_not_running')):
        if controller.get('status') == 'needs_action':
            block(controller.get('reason') or 'recovery_required',
                  'Goal recovery requires inspection of launch evidence', ['reconcile', 'stop'])
        elif controller.get('status') in ('waiting', 'recovering'):
            observed, actions, blocker = 'recovering', ['pause', 'stop'], None
    watchdog = facts['watchdog']
    service_intent = facts['service_intent']
    if (not completion and service_intent.get('desired_state') == 'running'
            and watchdog.get('epoch') == service_intent.get('epoch')
            and watchdog.get('phase') == 'needs_action'
            and (blocker is None or blocker['code'] == 'runner_not_running')):
        block('service_recovery_required', watchdog.get('blocker') or 'Service recovery requires inspection',
              ['service-reconcile'], {'service_epoch': service_intent['epoch'],
                                      'operation_id': watchdog.get('operation_id')})
    # Explicit retry may stop owned resources before preparing one extra attempt.
    # Unknown effects and human gates remain reconciliation/decision barriers.
    if (not completion and not unknown and not facts['runner_running']
            and cur.get('attempt_dir') and not state.get('recovery_issues')
            and facts['startup'].get('status') != 'starting'
            and (observed == 'stopped' or blocker and blocker['code'] in (
                'attempt_failed', 'runner_not_running', 'runner_error', 'recovery_required'))):
        if 'retry' not in actions:
            actions.append('retry')
    recovery_fact = controller if controller_current and not cur else recovery_fact
    if (observed == 'stopped' and not blocker and state.get('stop_status') == 'stopped'
            and cur.get('attempt', 1) < 3):
        recovery_fact = {'status': 'observing', 'next_check_at': None}
    if not confirmed:
        observed, actions = 'unconfirmed', []
        blocker = {'code': 'observation_changed', 'message': 'State changed during observation',
                   'facts': {}, 'actions': []}
    return {'schema_version': SCHEMA_VERSION, 'identity': runner.context.identity(),
            'revision': digest(facts), 'generation': intent.get('generation'), 'epoch': intent.get('epoch'),
            'goal': intent.get('goal') or state.get('goal'), 'desired_state': desired,
            'observed_state': observed, 'confirmed': confirmed,
            'current': {'item': cur.get('item'), 'attempt': attempt_identity(state)},
            'progress': progress, 'final_acceptance': final, 'blocker': blocker,
            'available_actions': [{'action': action} for action in actions],
            'recovery': {'enabled': intent['policy']['recovery'] if intent else facts['recovery_settings'].get('enabled', False),
                         'status': recovery_fact.get('status', 'observing'),
                         'next_check': recovery_fact.get('next_check_at')},
            'compatibility': 'v1' if intent else 'legacy'}


def operation(runner, identifier):
    """Read legacy receipts without rewriting their schema or inventing an intent."""
    identifier = request_id(identifier)
    matches = [(path, read_json(path)) for path in
               (runner.state_dir / 'commands' / (identifier + '.json'),
                runner.state_dir / 'ui/commands' / (identifier + '.json')) if path.exists()]
    if not matches:
        raise ContractError('operation_not_found', 'No operation with this request_id')
    if len(matches) != 1:
        raise ContractError('ambiguous_legacy_operation', 'Request ID exists in multiple legacy journals')
    path, record = matches[0]
    if record.get('schema_version') == 1 and record.get('action') == 'execute':
        from .goal_execute import lookup
        return lookup(runner, identifier)
    if 'schema_version' in record:
        version(record)
        command = record['command']
        version(command)
        if record.get('id') != identifier or command.get('request_id') != identifier:
            raise ContractError('invalid_operation', 'Operation request identity mismatch')
        if command.get('scope', {}).get('identity') != runner.context.identity():
            raise ContractError('identity_mismatch', 'Operation belongs to another workspace')
        statuses = {'pending': 'pending', 'running': 'running', 'succeeded': 'succeeded',
                    'failed': 'failed', 'unknown': 'unknown', 'done': 'succeeded',
                    'error': 'failed', 'applying': 'unknown'}
        if record.get('status') not in statuses:
            raise ContractError('invalid_operation', 'Unknown operation status')
        error = record.get('error')
        if isinstance(error, str):
            error = {'code': 'legacy_operation_error', 'message': error, 'details': {}, 'retryable': False}
        compatibility = ({'source': str(path.relative_to(runner.state_dir)), 'legacy_status': record['status']}
                         if record['status'] in ('done', 'error', 'applying') else None)
        return {'schema_version': SCHEMA_VERSION, 'request_id': identifier,
                'action': command['action'], 'scope': command['scope'], 'payload': command['payload'],
                'status': statuses[record['status']], 'result': record.get('result'), 'error': error,
                'compatibility': compatibility}
    statuses = {'pending': 'pending', 'applying': 'unknown', 'done': 'succeeded',
                'error': 'failed', 'unknown': 'unknown'}
    if record.get('status') not in statuses:
        raise ContractError('unsupported_legacy_status', 'Unknown legacy operation status')
    error = record.get('error')
    return {'schema_version': SCHEMA_VERSION, 'request_id': identifier, 'action': record['action'],
            'scope': {'identity': runner.context.identity(), 'generation': None,
                      'attempt': record.get('data', {}).get('_target')},
            'payload': {k: v for k, v in record.get('data', {}).items() if k != '_target'},
            'status': statuses[record['status']], 'result': record.get('result'),
            'error': {'code': 'legacy_operation_error', 'message': error, 'details': {}, 'retryable': False} if error else None,
            'compatibility': {'source': str(path.relative_to(runner.state_dir)), 'legacy_status': record['status']}}
