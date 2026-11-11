"""Durable explicit goal execution; completion here means launch acceptance only.

Lock order: request -> workspace execute -> goal-intent (brief authorization only). ProjectServices fences
its spawn effect separately; readiness waits do not hold the intent lock. No catalog lock is held during service readiness waits.
The manager is ensured before the project service and its supervisor. All effect
requests retain their original UUID across interrupted observations.
"""
import copy
import uuid

from . import goal_intent, service_intent
from .catalog import Catalog
from .project_services import ProjectServices
from .protocol import ContractError, request_id, version
from .runner_store import atomic_json, lock, read_json

_TERMINAL = ('succeeded', 'failed')
_RECEIPT_TERMINAL = (*_TERMINAL, 'unknown')


def _path(runner, identifier):
    return runner.state_dir / 'ui' / 'commands' / (identifier + '.json')


def _shared_path(runner, generation, epoch, service_epoch):
    request_id(generation)
    if type(epoch) is not int or epoch < 1:
        raise ContractError('invalid_operation', 'Invalid execute epoch')
    if type(service_epoch) is not int or service_epoch < 1:
        raise ContractError('invalid_operation', 'Invalid service epoch')
    return runner.state_dir / 'execute' / (generation + '-' + str(epoch) + '-' + str(service_epoch) + '.json')


def _read(runner, path):
    value = read_json(path)
    if value is None:
        return None
    version(value)
    if value.get('identity', value.get('command', {}).get('scope', {}).get('identity')) != runner.context.identity():
        raise ContractError('identity_mismatch', 'Execute journal belongs to another workspace')
    if value.get('status') not in ('pending', 'running', 'unknown', *_TERMINAL):
        raise ContractError('invalid_operation', 'Invalid execute status')
    if 'command' in value:
        command = value['command']
        version(command)
        if value.get('id') != command.get('request_id') or value.get('action') != command.get('action'):
            raise ContractError('invalid_operation', 'Execute request identity mismatch')
    else:
        request_id(value.get('generation'))
        request_id(value.get('service_request_id'))
        if type(value.get('epoch')) is not int or value['epoch'] < 1 or value.get('phase') not in ('authorized', 'manager', 'service', 'launch'):
            raise ContractError('invalid_operation', 'Invalid execute phase or authorization')
    return value


def _shared(runner, auth):
    value = _read(runner, _shared_path(runner, auth['generation'], auth['epoch'], auth['service_epoch']))
    if value and any(value[key] != auth[key] for key in ('generation', 'epoch', 'service_epoch')):
        raise ContractError('invalid_operation', 'Execute journal authorization mismatch')
    return value


def _result(record):
    command = record['command']
    return {'schema_version': 1, 'request_id': record['id'], 'action': 'execute',
            'scope': copy.deepcopy(command['scope']), 'payload': copy.deepcopy(command['payload']),
            'status': record['status'], 'result': copy.deepcopy(record.get('result')),
            'error': copy.deepcopy(record.get('error')), 'compatibility': None}


def _merge(record, shared):
    if record['status'] not in _RECEIPT_TERMINAL and shared:
        for key in ('status', 'result', 'error'):
            record[key] = copy.deepcopy(shared.get(key))
    return record


def lookup(runner, identifier):
    """Read persisted receipts only; never contact a service or start work."""
    identifier = request_id(identifier)
    record = _read(runner, _path(runner, identifier))
    if not record or record.get('action') != 'execute':
        raise ContractError('operation_not_found', 'No execute with this request_id')
    if record.get('authorization', {}).get('service_epoch') and record['status'] not in _RECEIPT_TERMINAL:
        auth = record['authorization']
        shared = _shared(runner, auth)
        _merge(record, shared)
    return _result(record)


def _unknown(shared, message):
    shared.update(status='unknown', error=ContractError('unknown_effect', message,
                  details={'action': 'Observe the original execute; do not replay with a new request ID'}).as_dict())


def _observe_launch(runner, shared):
    """Resolve a lost reply only from the exact launch's persisted evidence."""
    launch = read_json(runner.state_dir / 'launch.json', {})
    startup = read_json(runner.state_dir / 'startup.json', {})
    expected = {key: shared[key] for key in ('generation', 'epoch')}
    nonce = launch.get('nonce')
    if (launch.get('identity') == runner.context.identity() and launch.get('authorization') == expected
            and launch.get('options') == shared['options'] and nonce
            and nonce != shared.get('previous_nonce') and startup.get('nonce') == nonce
            and startup.get('status') in ('starting', 'running', 'idle')
            and (startup.get('terminal') or (runner.state_dir / ('launch-' + nonce + '.claim')).exists())):
        shared.update(status='succeeded', error=None, result={'status': 'accepted',
                      'generation': shared['generation'], 'epoch': shared['epoch'],
                      'launch': copy.deepcopy(startup)})
        return True
    return False


def _advance(runner, shared, services, start, fresh, manager):
    if shared['status'] in _TERMINAL:
        return
    if shared['phase'] == 'launch':
        if not _observe_launch(runner, shared):
            _unknown(shared, 'Runner launch outcome is unconfirmed')
        return
    if not goal_intent.permitted(runner, shared['generation'], shared['epoch']):
        shared.update(status='failed', error=ContractError('authorization_cancelled',
                      'Goal authorization changed before execution').as_dict())
        return
    project = runner.context.project_id
    path = _shared_path(runner, shared['generation'], shared['epoch'], shared['service_epoch'])
    if shared['phase'] in ('authorized', 'manager'):
        shared.update(phase='manager', status='running')
        atomic_json(path, shared)
        try:
            observed_manager = manager.ensure(shared['manager_request_id'])
        except Exception:
            _unknown(shared, 'Manager outcome is unconfirmed')
            return
        if not observed_manager.get('confirmed') or observed_manager.get('state') != 'running':
            _unknown(shared, 'Manager is not confirmed running')
            return
        fresh = True
    if fresh:
        # Persist before calling code that may create the service. The same UUID
        # is retained across crashes; an interrupted call is only observed.
        shared.update(phase='service', status='running')
        atomic_json(path, shared)
        try:
            operation = services.action(project, 'start', shared['service_request_id'],
                        expected_service_epoch=shared['service_epoch'],
                        expected_authorization={'identity': runner.context.identity(),
                                                **{key: shared[key] for key in ('generation', 'epoch')}})
            if operation.get('error_code') == 'authorization_cancelled':
                raise ContractError('authorization_cancelled', 'Goal authorization changed before service spawn')
        except ContractError as exc:
            shared.update(status='failed', error=exc.as_dict())
            return
        except Exception:
            # The service journal and authenticated status, not exception text,
            # determine whether the effect actually happened.
            pass
    try:
        observed = services.status(project)
    except Exception:
        _unknown(shared, 'Project service outcome is unconfirmed')
        return
    if not observed.get('confirmed') or observed.get('state') != 'running':
        _unknown(shared, 'Project service is not confirmed running')
        return
    if not goal_intent.permitted(runner, shared['generation'], shared['epoch']):
        shared.update(status='failed', error=ContractError('authorization_cancelled',
                      'Goal authorization changed before runner launch').as_dict())
        return
    catalog = Catalog(runner.context.config_root, runner.context.state_root)
    if not service_intent.permitted(catalog, project, shared['service_epoch']):
        shared.update(status='failed', error=ContractError('authorization_cancelled',
                      'Service authorization changed before runner launch').as_dict())
        return
    if shared.get('prior_launch'):
        prior = copy.deepcopy(shared['prior_launch'])
        shared.update(phase='launch', previous_nonce=prior.get('previous_nonce'))
        if prior['status'] == 'succeeded' or _observe_launch(runner, prior):
            shared.update(status='succeeded', result=prior['result'], error=None)
        else:
            _unknown(shared, 'Previous runner launch outcome is unconfirmed')
        return
    shared.update(phase='launch', status='running',
                  previous_nonce=read_json(runner.state_dir / 'launch.json', {}).get('nonce'))
    atomic_json(path, shared)
    try:
        # start_runner rechecks the authorization under its effect fence.
        result = start(runner, copy.deepcopy(shared['options']),
                       expected_authorization={key: shared[key] for key in ('generation', 'epoch')},
                       expected_service_epoch=shared['service_epoch'])
        if result.get('status') not in ('starting', 'running'):
            _unknown(shared, 'Runner launch was not confirmed accepted')
        else:
            shared.update(status='succeeded', error=None, result={'status': 'accepted',
                          'generation': shared['generation'], 'epoch': shared['epoch'], 'launch': result})
    except ContractError as exc:
        if exc.code == 'authorization_cancelled' and exc.details.get('no_effect'):
            shared.update(status='failed', error=exc.as_dict())
        elif not _observe_launch(runner, shared):
            _unknown(shared, 'Runner launch outcome is unconfirmed')
    except Exception:
        if not _observe_launch(runner, shared):
            _unknown(shared, 'Runner launch outcome is unconfirmed')


def execute(runner, options, identifier, *, generation=None, epoch=None, services=None, start=None, manager=None):
    """Authorize, ensure a service, and accept one launch per generation/epoch.

    A repeat observes the same effect journal. Concurrent identical cold requests
    share authorization and launch, including after a lost reply. No request can
    turn an unknown launch into a second attempt. Explicit cancellation/resume
    changes authorization; the underlying service retains its unknown guards.
    """
    identifier = request_id(identifier)
    command = {'schema_version': 1, 'request_id': identifier, 'action': 'execute',
               'scope': {'identity': runner.context.identity(), 'generation': generation,
                         'epoch': epoch, 'attempt': None}, 'payload': copy.deepcopy(options)}
    if start is None:
        from .service import start_runner
        start = start_runner
    ctx = runner.context
    catalog = Catalog(ctx.config_root, ctx.state_root)
    if services is None:
        services = ProjectServices(catalog)
    if manager is None:
        from .service_manager import Manager
        manager = Manager(catalog)
    with lock(runner.state_dir / 'requests' / (identifier + '.lock')):
        if (runner.state_dir / 'commands' / (identifier + '.json')).exists():
            raise ContractError('request_conflict', 'request_id already used in another command journal')
        path = _path(runner, identifier)
        record = _read(runner, path)
        if record and record.get('command') != command:
            raise ContractError('request_conflict', 'request_id already used for another command')
        if record and record['status'] in _RECEIPT_TERMINAL:
            return _result(record)
        if record and record.get('authorization', {}).get('service_epoch'):
            previous_shared = _shared(runner, record['authorization'])
            if previous_shared and previous_shared['phase'] == 'launch':
                # Cancellation revokes future effects; it cannot turn an already
                # attempted unknown launch into a proven failed/no-effect result.
                with lock(runner.state_dir / 'execute.lock'):
                    previous_shared = _shared(runner, record['authorization'])
                    _advance(runner, previous_shared, services, start, False, manager)
                    auth = record['authorization']
                    atomic_json(_shared_path(runner, auth['generation'], auth['epoch'], auth['service_epoch']), previous_shared)
                    _merge(record, previous_shared)
                    atomic_json(path, record)
                    return _result(record)
        if record is None:
            # This reservation precedes waiting for another execute and goal
            # authorization. A later manual service stop invalidates its epoch.
            reservation = service_intent.reserve(catalog, ctx.project_id, identifier, command)
            record = {'schema_version': 1, 'id': identifier, 'action': 'execute', 'command': command,
                      'service_reservation': reservation, 'status': 'pending', 'result': None, 'error': None}
            atomic_json(path, record)
        elif 'service_reservation' not in record:
            raise ContractError('service_resume_required', 'Execute predates service authorization; explicit migration required')
        with lock(runner.state_dir / 'execute.lock'):
            auth = goal_intent.authorize(runner, options, identifier, generation=generation, epoch=epoch)
            record['authorization'] = auth
            atomic_json(path, record)
            from .goal_completion import read as completed
            completion = completed(runner)
            if completion and completion.get('generation') == auth['generation']:
                record.update(status='succeeded', result={'status': 'already_completed',
                              'generation': auth['generation'], 'epoch': auth['epoch'],
                              'completion': completion}, error=None)
                atomic_json(path, record)
                return _result(record)
            try:
                with lock(runner.state_dir / 'goal-intent.lock'):
                    if not goal_intent.permitted(runner, auth['generation'], auth['epoch']):
                        raise ContractError('authorization_cancelled', 'Goal authorization changed before service authorization')
                    receipt = service_intent.authorize(catalog, ctx.project_id, identifier,
                        expected_epoch=record['service_reservation']['epoch'],
                        goal={'identity': ctx.identity(), **{key: auth[key] for key in ('generation', 'epoch')}})
                auth['service_epoch'] = receipt['epoch']
                if not service_intent.permitted(catalog, ctx.project_id, auth['service_epoch']):
                    raise ContractError('authorization_cancelled', 'Service authorization was cancelled')
            except ContractError as exc:
                record.update(status='failed', error=exc.as_dict())
                atomic_json(path, record)
                return _result(record)
            record['authorization'] = auth
            atomic_json(path, record)
            shared_path = _shared_path(runner, auth['generation'], auth['epoch'], auth['service_epoch'])
            shared = _shared(runner, auth)
            fresh = shared is None
            if fresh:
                intent = goal_intent.read(runner)
                shared = {'schema_version': 1, 'identity': runner.context.identity(),
                          'generation': auth['generation'], 'epoch': auth['epoch'], 'service_epoch': auth['service_epoch'],
                          'options': copy.deepcopy(intent['options']), 'phase': 'authorized',
                          'manager_request_id': str(uuid.uuid5(uuid.UUID(auth['generation']),
                                      'manager:' + str(auth['epoch']) + ':' + str(auth['service_epoch']))),
                          'service_request_id': str(uuid.uuid5(uuid.UUID(auth['generation']),
                                      'service:' + str(auth['epoch']) + ':' + str(auth['service_epoch']))),
                          'status': 'pending', 'result': None, 'error': None}
                for old_path in sorted((runner.state_dir / 'execute').glob(auth['generation'] + '-' + str(auth['epoch']) + '-*.json')):
                    old = _read(runner, old_path)
                    if old and old.get('phase') == 'launch':
                        shared['prior_launch'] = old
                        break
                atomic_json(shared_path, shared)
            _advance(runner, shared, services, start, fresh or shared['phase'] == 'authorized', manager)
            atomic_json(shared_path, shared)
            _merge(record, shared)
            atomic_json(path, record)
            return _result(record)
