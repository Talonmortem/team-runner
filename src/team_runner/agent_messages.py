"""Read persisted messages by workspace/generation/run/attempt; never consume mail."""
import base64
import binascii
import copy
import json
import os

from . import goal_intent
from .protocol import ContractError, attempt_identity

_MAX_CURSOR = 16384


def _encode(value):
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                             ensure_ascii=True).encode()).decode().rstrip('=')


def _decode(value):
    try:
        if not isinstance(value, str) or not value or len(value) > _MAX_CURSOR:
            raise ValueError()
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        cursor = json.loads(raw)
        if (not isinstance(cursor, dict)
                or set(cursor) != {'schema_version', 'identity', 'generation', 'run_id', 'attempt', 'offset'}
                or type(cursor['schema_version']) is not int or cursor['schema_version'] != 1
                or not isinstance(cursor['identity'], dict)
                or type(cursor['offset']) is not int or cursor['offset'] < 0
                or any(cursor[key] is not None and not isinstance(cursor[key], str)
                       for key in ('generation', 'run_id', 'attempt'))
                or _encode(cursor) != value):
            raise ValueError()
        return cursor
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise ContractError('invalid_cursor', 'Invalid persisted messages cursor') from None


def messages(runner, after=None, limit=100):
    """An existing cursor stays on its original attempt as queue progress continues."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ContractError('invalid_limit', 'Message limit must be an integer from 1 to 1000')
    cursor = _decode(after) if after is not None else None
    state = runner.load_state()
    identity = runner.context.identity()
    intent = goal_intent.read(runner)
    generation = intent.get('generation')
    run_id = state.get('run_id')
    current = state.get('current') or {}
    current_attempt = attempt_identity(state)
    child_attempt = os.environ.get('TEAM_RUNNER_ATTEMPT')
    if child_attempt:
        try:
            runner.context.check_attempt(child_attempt)
        except (ValueError, OSError) as exc:
            raise ContractError('child_scope_mismatch', str(exc)) from None
        if child_attempt != current.get('attempt_dir'):
            raise ContractError('child_scope_mismatch', 'Child can read only its current attempt')
    if cursor:
        if cursor['identity'] != identity:
            raise ContractError('identity_mismatch', 'Messages cursor belongs to another workspace')
        if cursor['generation'] != generation or cursor['run_id'] != run_id:
            raise ContractError('stale_scope', 'Messages cursor belongs to another generation or run')
        selected = cursor['attempt']
        offset = cursor['offset']
        if child_attempt and selected != current_attempt:
            raise ContractError('child_scope_mismatch', 'Child cannot read historical attempt messages')
    else:
        selected, offset = current_attempt, 0
    history = state.get('history', [])
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise ContractError('invalid_document', 'Invalid persisted message history')
    snapshots = [item for item in [*history, current]
                 if item and attempt_identity({'current': item}) == selected]
    if cursor and selected is not None and not snapshots:
        raise ContractError('stale_scope', 'Messages attempt is no longer retained')
    if cursor and selected is None and current_attempt is not None:
        raise ContractError('stale_scope', 'Messages cursor predates the current attempt')
    ordered, positions = [], {}
    for snapshot in snapshots:
        values = snapshot.get('messages', [])
        if not isinstance(values, list):
            raise ContractError('invalid_document', 'Invalid persisted messages')
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not value['id']:
                raise ContractError('invalid_document', 'Invalid persisted message identity')
            message_run = value.get('run_id', snapshot.get('run_id', run_id))
            if not isinstance(message_run, str) or not message_run:
                raise ContractError('invalid_document', 'Missing persisted message run identity')
            key = (message_run, value['id'])
            message = {**value, 'run_id': message_run}
            if key in positions:
                ordered[positions[key]] = {**ordered[positions[key]], **message}
            else:
                positions[key] = len(ordered)
                ordered.append(message)
    if offset > len(ordered):
        raise ContractError('invalid_cursor', 'Message cursor offset exceeds retained messages')
    end = min(offset + limit, len(ordered))
    following = {'schema_version': 1, 'identity': identity, 'generation': generation,
                 'run_id': run_id, 'attempt': selected, 'offset': end}
    return {'schema_version': 1, 'identity': identity, 'items': copy.deepcopy(ordered[offset:end]),
            'next_cursor': _encode(following), 'has_more': end < len(ordered)}
