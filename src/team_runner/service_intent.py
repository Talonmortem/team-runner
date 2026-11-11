"""Per-project service permission, independent of process observation and effects."""
import copy
import os
from .catalog import identifier
from .protocol import ContractError, request_id, version
from .runner_store import atomic_json, lock, read_json


def directory(catalog, project_id):
    identifier(project_id)
    if project_id not in catalog.read()['projects']:
        raise ContractError('identity_mismatch', 'Unknown service project')
    return catalog.state_root / 'projects' / project_id / 'service'


def identity(catalog, project_id):
    return {'project_id': project_id, 'config_root': str(catalog.config_root.resolve()),
            'state_root': str(catalog.state_root.resolve())}


def read(catalog, project_id):
    path = directory(catalog, project_id) / 'intent.json'
    value = read_json(path)
    if value is None and not path.exists():
        return {}
    version(value)
    if value.get('identity') != identity(catalog, project_id):
        raise ContractError('identity_mismatch', 'Service intent belongs to another project or roots')
    if (type(value.get('epoch')) is not int or value['epoch'] < 0
            or value.get('desired_state') not in ('running', 'stopped')
            or value.get('policy') != {'max_attempts': 3}
            or not isinstance(value.get('requests'), dict)):
        raise ContractError('invalid_intent', 'Invalid service intent')
    if value['desired_state'] == 'running' and (value['epoch'] < 1 or value.get('running_from_epoch') != value['epoch'] - 1):
        raise ContractError('invalid_intent', 'Invalid service authorization transition')
    for rid, receipt in value['requests'].items():
        request_id(rid)
        if (not isinstance(receipt, dict) or not isinstance(receipt.get('request'), dict)
                or receipt.get('status') not in ('reserved', 'authorized', 'cancelled')
                or type(receipt.get('epoch')) is not int or not 0 <= receipt['epoch'] <= value['epoch']):
            raise ContractError('invalid_intent', 'Invalid service intent receipt')
    return value


def _initial(catalog, project_id):
    return read(catalog, project_id) or {'schema_version': 1, 'identity': identity(catalog, project_id),
            'epoch': 0, 'desired_state': 'stopped', 'policy': {'max_attempts': 3}, 'requests': {}}


def _guard(identifier):
    request_id(identifier)
    if os.environ.get('TEAM_RUNNER_ATTEMPT'):
        raise ContractError('child_lifecycle_forbidden', 'Child attempts cannot authorize services')


def permitted(catalog, project_id, epoch):
    value = read(catalog, project_id)
    return bool(value and type(epoch) is int and value['epoch'] == epoch and value['desired_state'] == 'running')


def reserve(catalog, project_id, identifier, payload):
    """Save an execute's original service epoch before goal authorization/waits."""
    _guard(identifier)
    path = directory(catalog, project_id)
    request = {'action': 'execute', 'payload': copy.deepcopy(payload)}
    with lock(path / 'intent.lock'):
        value = _initial(catalog, project_id)
        old = value['requests'].get(identifier)
        if old:
            if old['request'] != request:
                raise ContractError('request_conflict', 'Service request ID already used')
            return copy.deepcopy(old)
        receipt = {'request': request, 'status': 'reserved', 'epoch': value['epoch']}
        value['requests'][identifier] = receipt
        atomic_json(path / 'intent.json', value)
        return copy.deepcopy(receipt)


def authorize(catalog, project_id, identifier, *, expected_epoch=None, goal=None, action='start'):
    """Commit a reservation or a new explicit start; never upgrade a retried UUID.

    Execute callers hold goal-intent.lock while committing and validate that goal
    receipt immediately before this call. No service lock ever acquires a goal lock.
    """
    _guard(identifier)
    path = directory(catalog, project_id)
    with lock(path / 'intent.lock'):
        value = _initial(catalog, project_id)
        old = value['requests'].get(identifier)
        if goal is not None:
            if not old or old['request'].get('action') != 'execute':
                raise ContractError('service_resume_required', 'Execute has no original service reservation')
            if old['status'] == 'authorized':
                if old.get('goal') != goal:
                    raise ContractError('request_conflict', 'Service reservation belongs to another goal')
                return copy.deepcopy(old)
            if old['epoch'] != expected_epoch:
                raise ContractError('stale_scope', 'Service reservation epoch mismatch')
        else:
            request = {'action': action, 'expected_epoch': expected_epoch}
            if old:
                if old['request'] != request:
                    raise ContractError('request_conflict', 'Service request ID already used')
                return copy.deepcopy(old)
            old = {'request': request, 'status': 'reserved', 'epoch': value['epoch']}
        current = value['epoch']
        # A concurrent authorization may have changed stopped -> running once;
        # joining it is safe only when no intervening cancellation occurred.
        joins = (goal is not None and value['desired_state'] == 'running'
                 and value.get('running_from_epoch') == expected_epoch)
        if expected_epoch is not None and expected_epoch != current and not joins:
            raise ContractError('authorization_cancelled', 'Service permission changed after reservation')
        if value['desired_state'] != 'running' or action == 'restart':
            value.update(epoch=current + 1, desired_state='running', running_from_epoch=current)
        receipt = {**old, 'status': 'authorized', 'epoch': value['epoch']}
        if goal is not None:
            receipt['goal'] = copy.deepcopy(goal)
        value['requests'][identifier] = receipt
        atomic_json(path / 'intent.json', value)
        return copy.deepcopy(receipt)


def cancel(catalog, project_id, identifier):
    """Persist suppression before callers wait for the service operations lock."""
    _guard(identifier)
    path = directory(catalog, project_id)
    request = {'action': 'stop'}
    with lock(path / 'intent.lock'):
        value = _initial(catalog, project_id)
        old = value['requests'].get(identifier)
        if old:
            if old['request'] != request:
                raise ContractError('request_conflict', 'Service request ID already used')
            return copy.deepcopy(old)
        value.update(epoch=value['epoch'] + 1, desired_state='stopped')
        value.pop('running_from_epoch', None)
        receipt = {'request': request, 'status': 'cancelled', 'epoch': value['epoch']}
        value['requests'][identifier] = receipt
        atomic_json(path / 'intent.json', value)
        return copy.deepcopy(receipt)
