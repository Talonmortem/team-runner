"""Durable goal authorization. This module never starts a process or dispatch."""
import copy
import json
from pathlib import PurePosixPath
import os
import uuid

from .protocol import ContractError, request_id, version
from .runner_store import atomic_json, lock, read_json


def _invalid(field):
    raise ContractError('invalid_intent', f'Invalid goal intent field: {field}',
                        details={'field': field, 'action': 'Inspect goal-intent.json; do not replay effects'})


def _uuid(value, field):
    try:
        request_id(value)
    except ContractError:
        _invalid(field)


def _options(value, field, *, initial=False):
    if not isinstance(value, dict):
        _invalid(field)
    if initial and value == {'resume': True} and type(value['resume']) is bool:
        return
    allowed = {'goal', 'items', 'mode', 'final', 'config', 'agent', 'provider',
               'profile', 'model', 'effort', 'handoff', 'fast'}
    if initial:
        allowed.add('recovery')
    if set(value) - allowed:
        _invalid(field)
    goal = value.get('goal')
    if (not isinstance(goal, str) or not goal or PurePosixPath(goal).is_absolute()
            or '..' in PurePosixPath(goal).parts or '\\' in goal):
        _invalid(field + '.goal')
    if value.get('mode', 'confirm') not in ('auto', 'confirm'):
        _invalid(field + '.mode')
    for key in ('final', 'recovery', 'fast'):
        if key in value and type(value[key]) is not bool:
            _invalid(field + '.' + key)
    for key in allowed - {'goal', 'mode', 'final', 'recovery', 'fast'}:
        if key in value and value[key] is not None and not isinstance(value[key], str):
            _invalid(field + '.' + key)


def validate(runner, value):
    """Check stored authorization structurally, without consulting mutable tracking."""
    version(value)
    if value.get('identity') != runner.context.identity():
        raise ContractError('identity_mismatch', 'Goal intent belongs to another workspace')
    _uuid(value.get('generation'), 'generation')
    if type(value.get('epoch')) is not int or value['epoch'] < 1:
        _invalid('epoch')
    if value.get('desired_state') not in ('running', 'paused', 'stopped'):
        _invalid('desired_state')
    _options(value.get('options'), 'options')
    if value.get('goal') != value['options']['goal']:
        _invalid('goal')
    policy = value.get('policy')
    if not isinstance(policy, dict) or set(policy) != {'mode', 'final', 'recovery', 'max_attempts'}:
        _invalid('policy')
    if policy['mode'] != value['options'].get('mode', 'confirm'):
        _invalid('policy.mode')
    if type(policy['final']) is not bool or policy['final'] != value['options'].get('final', False):
        _invalid('policy.final')
    if type(policy['recovery']) is not bool:
        _invalid('policy.recovery')
    if type(policy['max_attempts']) is not int or policy['max_attempts'] != 3:
        _invalid('policy.max_attempts')
    _options(value.get('initial_options'), 'initial_options', initial=True)
    _uuid(value.get('originating_request_id'), 'originating_request_id')
    receipts = value.get('requests')
    if not isinstance(receipts, dict) or value['originating_request_id'] not in receipts:
        _invalid('requests')
    for identifier, receipt in receipts.items():
        field = 'requests.' + str(identifier)
        _uuid(identifier, field)
        if not isinstance(receipt, dict) or set(receipt) != {'request', 'result'}:
            _invalid(field)
        request, result = receipt['request'], receipt['result']
        if not isinstance(request, dict) or set(request) != {'options', 'generation', 'epoch'}:
            _invalid(field + '.request')
        _options(request['options'], field + '.request.options', initial=True)
        if request['options'] != {'resume': True} and request['options'] != value['initial_options']:
            _invalid(field + '.request.options')
        if request['generation'] is None:
            if request['epoch'] is not None:
                _invalid(field + '.request.epoch')
        elif (request['generation'] != value['generation'] or type(request['epoch']) is not int
              or not 1 <= request['epoch'] <= value['epoch']):
            _invalid(field + '.request.scope')
        if not isinstance(result, dict):
            _invalid(field + '.result')
        version(result)
        if (result.get('generation') != value['generation'] or result.get('goal') != value['goal']
                or result.get('request_id') != identifier or result.get('status') != 'authorized'
                or type(result.get('epoch')) is not int or not 1 <= result['epoch'] <= value['epoch']):
            _invalid(field + '.result')
        if (request['epoch'] is None and result['epoch'] != 1
                or request['epoch'] is not None and result['epoch'] not in (request['epoch'], request['epoch'] + 1)):
            _invalid(field + '.result.epoch')
    if receipts[value['originating_request_id']]['request']['options'] != value['initial_options']:
        _invalid('initial_options')
    return value


def read(runner):
    path = runner.state_dir / 'goal-intent.json'
    try:
        value = read_json(path)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _invalid('document')
    # Only absence is legacy; empty/null/corrupt persisted files cannot authorize.
    if value is None and not path.exists():
        return {}
    return validate(runner, value)


def permitted(runner, generation, epoch):
    value = read(runner)
    return bool(value and value.get('generation') == generation and value.get('epoch') == epoch
                and value.get('desired_state') == 'running')


def authorize(runner, options, identifier, *, generation=None, epoch=None):
    """Atomically persist authorization and its receipt before any launch effects.

    Generation identifies a goal execution. Epoch fences cancellation within that
    generation. Retrying an old request returns its original receipt, not renewed
    permission. Callers must check permitted() immediately before their effects.
    """
    from .application import _check_identity, start_options
    request_id(identifier)
    if os.environ.get('TEAM_RUNNER_ATTEMPT'):
        raise ContractError('child_lifecycle_forbidden', 'Child attempts cannot authorize goals')
    _check_identity(runner, runner.context.identity())
    if not isinstance(options, dict):
        raise ContractError('invalid_payload', 'Execute options must be an object')
    options = copy.deepcopy(options)
    request = {'options': options, 'generation': generation, 'epoch': epoch}
    path = runner.state_dir / 'goal-intent.json'
    with lock(runner.state_dir / 'goal-intent.lock'):
        previous = read(runner)
        receipts = previous.get('requests', {})
        if identifier in receipts:
            receipt = receipts[identifier]
            if receipt['request'] != request:
                raise ContractError('request_conflict', 'Request ID already used for another authorization')
            return copy.deepcopy(receipt['result'])
        state = runner.load_state()
        resume = options == {'resume': True}
        if previous:
            if options.get('goal', previous['goal']) != previous['goal']:
                raise ContractError('goal_conflict', 'Another goal owns this execution; archive it explicitly')
            # Concurrent identical cold requests may share the initial authorization.
            cold_duplicate = (generation is None and epoch is None and previous['epoch'] == 1
                              and previous['desired_state'] == 'running'
                              and previous.get('initial_options') == options)
            if not cold_duplicate and (generation != previous['generation'] or epoch != previous['epoch']):
                raise ContractError('stale_scope', 'Authorization changed; refresh before executing')
            if not resume and not cold_duplicate:
                raise ContractError('resume_required', 'Use resume with the saved policy')
            value = copy.deepcopy(previous)
            if value['desired_state'] != 'running':
                value['epoch'] += 1
                value['desired_state'] = 'running'
        else:
            if generation is not None or epoch is not None:
                raise ContractError('stale_scope', 'No matching goal authorization')
            if state.get('goal') and not resume:
                raise ContractError('resume_required', 'Existing execution requires explicit resume')
            if resume:
                validated = start_options(runner, options, state)
                recovery = read_json(runner.state_dir / 'recovery-settings.json', {}).get('enabled', False)
            else:
                supplied = dict(options)
                recovery = supplied.pop('recovery', True)
                if type(recovery) is not bool:
                    raise ContractError('invalid_payload', 'recovery must be boolean')
                supplied.setdefault('mode', 'auto')
                supplied.setdefault('final', True)
                validated = start_options(runner, supplied, state)
            value = {'schema_version': 1, 'identity': runner.context.identity(),
                     'goal': validated['goal'], 'generation': str(uuid.uuid4()), 'epoch': 1,
                     'desired_state': 'running', 'options': validated,
                     'policy': {'mode': validated.get('mode', 'confirm'),
                                'final': validated.get('final', False), 'recovery': recovery,
                                'max_attempts': 3},
                     'originating_request_id': identifier, 'initial_options': options, 'requests': {}}
        result = {'schema_version': 1, 'generation': value['generation'], 'epoch': value['epoch'],
                  'goal': value['goal'], 'request_id': identifier, 'status': 'authorized'}
        value['requests'][identifier] = {'request': request, 'result': result}
        validate(runner, value)
        atomic_json(path, value)
        return copy.deepcopy(result)


def change(runner, *, desired_state=None, recovery=None, generation=None, epoch=None):
    """Update existing intent; never infer permission by creating legacy intent."""
    if desired_state is not None and desired_state not in ('running', 'paused', 'stopped'):
        raise ContractError('invalid_intent', 'Invalid desired state')
    if recovery is not None and type(recovery) is not bool:
        raise ContractError('invalid_intent', 'recovery must be boolean')
    with lock(runner.state_dir / 'goal-intent.lock'):
        value = read(runner)
        if not value:
            return None
        if generation is not None and generation != value['generation'] or epoch is not None and epoch != value['epoch']:
            raise ContractError('stale_scope', 'Goal authorization changed')
        changed = False
        cancelled_or_resumed = False
        if desired_state is not None and desired_state != value['desired_state']:
            value['desired_state'] = desired_state
            changed = True
            cancelled_or_resumed = True
        if recovery is not None and recovery != value['policy']['recovery']:
            value['policy']['recovery'] = recovery
            changed = True
        if changed:
            if cancelled_or_resumed:
                value['epoch'] += 1
            atomic_json(runner.state_dir / 'goal-intent.json', value)
        return value
