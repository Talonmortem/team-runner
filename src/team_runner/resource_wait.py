"""Explicit technical resource waits, delivered through the owner's reply queue."""
import hashlib
from contextlib import contextmanager
from datetime import datetime
import math
import os
from pathlib import Path
import time

from . import event_scope, heavy_resource
from .runner_store import enqueue, lock, read_json


class ResourceBusy(ValueError):
    """No input was sent; the same pending reply may wait for the lease."""


def _proof(attempt, check):
    path = Path(check)
    if not path.is_absolute():
        path = Path(attempt) / path
    path = path.resolve(strict=True)
    if not path.is_relative_to(Path(attempt).resolve()):
        raise ValueError('resource check belongs to another attempt')
    record = read_json(path, {})
    proof_path = Path(str(path) + '.resource-wait.json')
    raw = proof_path.read_bytes()
    proof = read_json(proof_path, {})
    if (not isinstance(record, dict) or not isinstance(proof, dict)
            or proof_path.is_symlink() or proof.get('kind') != 'host_heavy'
            or proof.get('path') != str(heavy_resource.default_path())
            or not record.get('id') or proof.get('check_id') != record['id']
            or record.get('attempt') != str(attempt) or proof.get('attempt') != str(attempt)
            or record.get('resource_wait') != proof or type(proof.get('waiting_at')) not in (int, float)
            or not math.isfinite(proof['waiting_at']) or proof['waiting_at'] <= 0):
        raise ValueError('native heavy-resource wait evidence required')
    return {'check': str(path), 'check_id': record['id'], 'waiting_at': proof['waiting_at'],
            'proof_sha256': hashlib.sha256(raw).hexdigest()}


def _messages(runner, state, data):
    info = data['_resource_wait']
    cur = state.get('current') or {}
    attempt = cur.get('attempt_dir')
    if (not attempt or data.get('_target') != attempt or info.get('identity') != runner.context.identity()
            or info.get('dispatch') != cur.get('dispatch_id')):
        raise ValueError('resource wait scope changed')
    runner.context.check_attempt(attempt)
    reg = runner.launcher.registry(attempt)
    agent = next((a for a in reg['agents'] if a.get('id') == info.get('agent')
                  and a.get('dispatch_id') == info['dispatch'] and a.get('role') == 'coordinator'
                  and a.get('status') in ('active', 'waiting_child') and not a.get('superseded_by')), None)
    if not agent:
        raise ValueError('resource wait coordinator changed')
    selected = []
    identifiers = ([data['message_id']] if data.get('message_id') else []) + info['escalations']
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('duplicate resource wait message')
    for identifier in identifiers:
        matches = [m for m in cur.get('messages', []) if m.get('id') == identifier
                   and m.get('run_id') == data['run_id']]
        if len(matches) != 1:
            raise ValueError('resource wait message missing or ambiguous')
        message = matches[0]
        payload = event_scope.payload(message)
        classification = event_scope.classify(message, cur, reg)['kind']
        waiting = cur.get('resource_waits', {}).get(data['request_id'], {})
        own_wait = (classification == 'waiting_resource' and waiting.get('dispatch') == agent['dispatch_id']
                    and identifier in waiting.get('technical_events', []))
        if (payload.get('dispatchId') != agent['dispatch_id'] or payload.get('taskId') != agent['task_id']
                or message.get('type') != ('question' if identifier == data.get('message_id') else 'escalation')
                or classification != 'active' and not own_wait):
            raise ValueError('resource wait message identity/type mismatch')
        selected.append(message)
    if _proof(attempt, info['check']) != {k: info[k] for k in ('check', 'check_id', 'waiting_at', 'proof_sha256')}:
        raise ValueError('resource wait evidence changed')
    return reg, agent, selected


def register(runner, question, check, escalations, identifier):
    """The exact coordinator registers a technical wait, then ends its turn."""
    from . import graph_dispatch, goal_intent
    from .protocol import request_id
    request_id(identifier)
    attempt = os.environ.get('TEAM_RUNNER_ATTEMPT')
    if not attempt:
        raise ValueError('wait-resource requires inherited attempt context')
    runner.context.check_attempt(attempt)
    with lock(Path(attempt) / 'agents.lock'):
        state = runner.load_state()
        reg = runner.launcher.registry(attempt)
        agent = graph_dispatch.caller(runner.launcher, attempt, reg)
        if agent.get('role') != 'coordinator':
            raise ValueError('only the current coordinator may register resource waiting')
        if any(c.get('parent_agent') == agent['id'] and c.get('status') not in ('completed', 'stopped')
               for c in reg.get('graph_calls', [])):
            raise ValueError('resource waiting requires child settlement; a live native check continues itself')
        intent = goal_intent.read(runner)
        info = dict(_proof(attempt, check), identity=runner.context.identity(),
                    authorization={k: intent.get(k) for k in ('generation', 'epoch')},
                    agent=agent['id'], dispatch=agent['dispatch_id'], escalations=sorted(escalations))
        data = {'_target': attempt, 'message_id': question, 'run_id': agent['run_id'], 'request_id': identifier,
                '_resource_wait': info, 'text':
                'The native host heavy lease is available and its cleanup evidence is conclusive. '
                'Continue the same approved bounded task from its checkpoint through normal native lease acquisition. '
                'Reconcile unfinished work; do not replay unknown effects or repeat completed checks. '
                'Resource availability is not test success or acceptance.'}
        _messages(runner, state, data)
        _permitted(runner, state, data)
        return enqueue(runner.state_dir, 'reply' if question else 'resource-resume', data, identifier)


def _permitted(runner, state, data):
    from .autonomy import policy
    from .goal_intent import read
    intent = read(runner)
    if (not policy(runner)['enabled'] or state.get('paused') or state.get('stop_requested')
            or intent.get('desired_state') != 'running'
            or data['_resource_wait']['authorization'] != {k: intent.get(k) for k in ('generation', 'epoch')}):
        raise ValueError('resource continuation cancelled by policy or goal intent')
    cur = state.get('current') or {}
    if (cur.get('failed') or cur.get('problems') or 'acceptance_rework' in cur
            or cur.get('gate_id') and not cur.get('gate_resolved')):
        raise ValueError('resource continuation cancelled: acceptance or user decision required')


def effect(data):
    if data.get('message_id'):
        return ('reply:' + data['run_id'] + ':' + data['message_id'],
                ['orchestration', 'reply', '--id', data['message_id'], '--body', data['text'], '--run', data['run_id']])
    return ('resource-resume:' + data['request_id'],
            ['orchestration', 'send', '--run', data['run_id'], '--to', 'dispatch:' + data['_resource_wait']['dispatch'],
             '--type', 'status', '--subject', 'Native host resource available', '--body', data['text']])


def check_receipt(operation, data):
    if operation.get('args') != effect(data)[1]:
        raise ValueError('resource reply receipt identity mismatch')
    if not data.get('message_id'):
        result = operation.get('result') or {}
        message = result.get('message') or {}
        if (not message.get('id') or message.get('run_id') != data['run_id']
                or message.get('to_handle') != 'dispatch:' + data['_resource_wait']['dispatch']
                or (result.get('mutation') or {}).get('requestId') != operation.get('request_id')):
            from .runner_operations import UncertainOperation
            raise UncertainOperation('resource continuation receipt identity unconfirmed')


def _receipt(runner, data):
    operation = read_json(runner.state_dir / 'operations.json', {}).get(effect(data)[0], {})
    # An existing completed receipt can settle after pause without sending input.
    if operation.get('status') == 'completed':
        check_receipt(operation, data)
        return True
    return False


def ready(runner, state, data):
    reg, agent, messages = _messages(runner, state, data)
    if _receipt(runner, data):
        return True
    _permitted(runner, state, data)
    if any(c.get('parent_agent') == agent['id'] and c.get('status') not in ('completed', 'stopped')
           for c in reg.get('graph_calls', [])):
        raise ValueError('resource continuation superseded by another child call')
    if data.get('message_id') and (messages[0].get('answered') or messages[0].get('closed')):
        raise ValueError('resource question already resolved')
    if any(m.get('type') == 'question' and m.get('id') != data.get('message_id')
           for m in event_scope.pending(state['current'], reg)):
        return False
    if not data.get('message_id') and any(
            m.get('id') not in data['_resource_wait']['escalations']
            for m in event_scope.pending(state['current'], reg)):
        return False
    from .recovery import _unknown
    if reg.get('fenced') or reg.get('stop_requested') or state.get('control_uncertain') or _unknown(runner, state):
        return False
    if not heavy_resource.available():
        return False
    runner.launcher.verify_terminal(agent, runner.launcher.observe_agent(agent))
    return True


def mark(state, data, command_id, *, cancelled=False):
    if data.get('message_id'):
        return False
    cur = state['current']
    waits = cur.setdefault('resource_waits', {})
    previous = waits.get(command_id)
    entry = dict(previous or {'dispatch': data['_resource_wait']['dispatch'],
                              'proof_sha256': data['_resource_wait']['proof_sha256'],
                              'status': 'waiting', 'technical_events': []})
    if cancelled:
        entry['status'] = 'cancelled'
    else:
        reg_dispatch = data['_resource_wait']['dispatch']
        for message in cur.get('messages', []):
            payload = event_scope.payload(message)
            try:
                ended = datetime.fromisoformat(message['created_at'].replace('Z', '+00:00'))
                ended_at = ended.timestamp() if ended.tzinfo else None
            except (KeyError, TypeError, ValueError, AttributeError):
                ended_at = None
            if (not entry['technical_events'] and message.get('type') == 'escalation' and payload.get('stopReason') == 'finished'
                    and payload.get('dispatchId') == reg_dispatch
                    and ended_at is not None and ended_at >= math.floor(data['_resource_wait']['waiting_at'])
                    and message['id'] not in entry['technical_events']):
                entry['technical_events'] = [*entry['technical_events'], message['id']]
    if previous == entry:
        return False
    waits[command_id] = entry
    return True


@contextmanager
def permission(runner, data):
    if _receipt(runner, data):
        ready(runner, runner.load_state(), data)
        yield  # Existing receipt only; settling never submits new input.
        return
    with runner.launch_permission():
        if not ready(runner, runner.load_state(), data):
            raise ResourceBusy('native heavy resource still unavailable')
        yield


def finish(state, data, command_id):
    """Called only after the exact reply receipt, including cached reconciliation."""
    cur = state['current']
    if command_id in cur.get('resource_waits', {}):
        cur['resource_waits'][command_id]['status'] = 'notified'
    for message in cur.get('messages', []):
        if (message.get('id') in data['_resource_wait']['escalations']
                and message.get('run_id') == data['run_id'] and not message.get('closed')):
            message.update(closed=True, closed_reason='registered native resource wait resolved',
                           resource_resolution={'request_id': command_id,
                                                'proof_sha256': data['_resource_wait']['proof_sha256'],
                                                'at': time.time()})
