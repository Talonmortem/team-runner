"""Bounded persisted observation. Waiting never authorizes or retries effects."""
import math
import time

from . import protocol
from .runner_store import read_json


def _generation(runner, identifier, operation):
    if operation['action'] != 'execute':
        raise protocol.ContractError('invalid_wait_target', 'Goal waiting requires an execute request')
    # Cold execute's command scope has no generation. Its durable authorization
    # remains authoritative even when the operation receipt is immutable unknown.
    record = read_json(runner.state_dir / 'ui' / 'commands' / (identifier + '.json'), {})
    authorization = record.get('authorization') or {}
    if not isinstance(authorization, dict):
        raise protocol.ContractError('invalid_operation', 'Invalid execute authorization')
    generation = authorization.get('generation')
    result_generation = (operation.get('result') or {}).get('generation')
    if generation and result_generation and generation != result_generation:
        raise protocol.ContractError('invalid_operation', 'Execute generation evidence conflicts')
    generation = generation or result_generation
    if generation is not None:
        try:
            protocol.request_id(generation)
        except protocol.ContractError:
            raise protocol.ContractError('invalid_operation', 'Invalid execute generation') from None
    return generation


def _starting(runner, generation):
    launch = read_json(runner.state_dir / 'launch.json', {})
    startup = read_json(runner.state_dir / 'startup.json', {})
    return bool(startup.get('status') == 'starting' and startup.get('nonce')
                and startup['nonce'] == launch.get('nonce')
                and launch.get('identity') == runner.context.identity()
                and launch.get('authorization', {}).get('generation') == generation)


def _blocked(code, message, details=None):
    return {'code': code, 'message': message, 'details': details or {}, 'retryable': False}


def wait(runner, identifier, until='operation', timeout=30, clock=time.monotonic, sleep=time.sleep):
    """Wait for one operation or its exact goal generation; never restart either.

    Operation success means acceptance only. Goal success requires the confirmed
    summary's completion state, including saved final=False queue completion.
    An unconfirmed/torn summary cannot establish success, cancellation, or a
    changed generation. Interrupted clients leave every persisted file untouched.
    """
    identifier = protocol.request_id(identifier)
    if until not in ('operation', 'goal'):
        raise protocol.ContractError('invalid_wait_target', 'until must be operation or goal')
    if type(timeout) not in (int, float) or not 0 <= timeout <= 3600 or not math.isfinite(timeout):
        raise protocol.ContractError('invalid_timeout', 'timeout must be finite and between 0 and 3600 seconds')
    deadline = clock() + timeout
    generation = None
    while True:
        operation = protocol.operation(runner, identifier)
        summary = None
        wait_status, blocker = None, None
        if until == 'operation':
            if operation['status'] == 'succeeded':
                wait_status = 'succeeded'
            elif operation['status'] in ('failed', 'unknown'):
                wait_status = 'blocked'
                blocker = operation.get('error') or _blocked('operation_' + operation['status'],
                                                             'Operation requires inspection')
        else:
            observed_generation = _generation(runner, identifier, operation)
            if generation and observed_generation and generation != observed_generation:
                raise protocol.ContractError('invalid_operation', 'Execute authorization changed while waiting')
            generation = generation or observed_generation
            summary = protocol.summary(runner)
            confirmed = summary.get('confirmed') is True
            observed = summary.get('observed_state')
            matching = bool(generation and summary.get('generation') == generation)
            if generation and confirmed and not matching:
                wait_status, blocker = 'blocked', _blocked('generation_changed',
                    'The observed workspace no longer contains this execution',
                    {'expected': generation, 'observed': summary.get('generation')})
            elif matching and confirmed and observed in ('completed', 'queue_completed'):
                # A completed goal does not rewrite a lost/unknown operation reply.
                wait_status = 'succeeded'
            elif operation['status'] in ('failed', 'unknown'):
                wait_status = 'blocked'
                blocker = operation.get('error') or _blocked('operation_' + operation['status'],
                                                             'Execute outcome requires inspection')
            elif matching and confirmed:
                reason = summary.get('blocker') or {}
                pending_start = (reason.get('code') == 'runner_not_running'
                                 and (operation['status'] in ('pending', 'running') or _starting(runner, generation)))
                if not pending_start and observed in ('needs_action', 'paused', 'stopped', 'failed', 'unknown'):
                    wait_status = 'blocked'
                    blocker = reason or _blocked('goal_' + observed, 'Goal is ' + observed)
            if generation is None and operation['status'] == 'succeeded':
                wait_status, blocker = 'blocked', _blocked('goal_generation_unavailable',
                    'Execute has no persisted goal generation; inspect its receipt')
        remaining = deadline - clock()
        if wait_status is not None or remaining <= 0:
            return {'schema_version': 1, 'request_id': identifier, 'until': until,
                    'wait_status': wait_status or 'timeout', 'generation': generation,
                    'operation': operation, 'summary': summary, 'blocker': blocker}
        sleep(min(.2, remaining))
