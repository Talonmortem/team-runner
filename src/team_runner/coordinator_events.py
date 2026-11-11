"""Durable processing acknowledgements, distinct from transport delivery ACKs."""
import os
from pathlib import Path
import time
import uuid

from .runner_store import atomic_json, read_json, lock


def paths(attempt):
    return Path(attempt) / 'coordinator-events.json', Path(attempt) / 'coordinator-events.lock'


def capture(runner, state, now=None):
    current = state.get('current') or {}
    if not current.get('attempt_dir') or not current.get('dispatch_id'):
        return
    path, guard = paths(current['attempt_dir'])
    with lock(guard):
        journal = read_json(path, {'events': [], 'notifications': []})
        known = {(e['run_id'], e['message_id']) for e in journal['events']}
        for message in current.get('messages', []):
            key = (message.get('run_id'), message.get('id'))
            if (key in known or not all(key) or key[0] == state.get('run_id')
                    or message.get('type') not in ('question', 'escalation', 'worker_done')):
                continue
            journal['events'].append({'id': str(uuid.uuid4()), 'run_id': key[0], 'message_id': key[1],
                                      'type': message['type'], 'created_at': time.time() if now is None else now,
                                      'dispatch': current['dispatch_id'], 'processed_at': None})
            known.add(key)
        atomic_json(path, journal)


def acknowledge(runner, attempt, dispatch, ids, now=None):
    runner.context.check_attempt(attempt)
    current = runner.load_state().get('current') or {}
    if current.get('attempt_dir') != attempt or current.get('dispatch_id') != dispatch:
        raise ValueError('event acknowledgement belongs to a stale attempt/Dispatch')
    terminal = os.environ.get('ORCA_TERMINAL_HANDLE')
    if terminal != current.get('terminal'):
        raise ValueError('only the exact coordinator terminal may acknowledge processing')
    path, guard = paths(attempt)
    with lock(guard):
        journal = read_json(path, {'events': [], 'notifications': []})
        matches = [e for e in journal['events'] if e['id'] in ids and e['dispatch'] == dispatch]
        if set(ids) != {e['id'] for e in matches}:
            raise ValueError('unknown event')
        for event in matches:
            if event['processed_at'] is None:
                event['processed_at'] = time.time() if now is None else now
                event['coordinator_wait_seconds'] = event['processed_at'] - event['created_at']
        atomic_json(path, journal)
        return matches


def view(attempt, now=None):
    if not attempt:
        return {'events': [], 'notifications': [], 'overdue': 0}
    journal = read_json(paths(attempt)[0], {'events': [], 'notifications': []})
    now = time.time() if now is None else now
    journal['overdue'] = sum(e['processed_at'] is None and now - e['created_at'] >= 300
                             for e in journal['events'])
    return journal


def record_receipt(notification, result):
    if notification.get('transport') == 'dispatch':
        message = result.get('message') or {}
        if (not message.get('id') or message.get('to_handle') != 'dispatch:' + notification['dispatch']
                or message.get('run_id') != notification['run_id']
                or (result.get('mutation') or {}).get('requestId') != notification['id']):
            return
        # Durable queueing is not evidence of input, a started turn or processing.
        notification.update(status='accepted', receipt=result, message_id=message['id'])
    else:
        # Retain reconciliation for pre-upgrade terminal submissions; never resend.
        prompt = (result.get('send') or {}).get('prompt') or {}
        if prompt.get('requestId') == notification['id']:
            accepted = 'input_accepted' in prompt.get('stages', [])
            notification.update(status='accepted' if accepted else 'unknown', receipt=result,
                                input_accepted=accepted,
                                turn_started='turn_started' in prompt.get('stages', []))


def tick(runner, state, now=None):
    current = state.get('current') or {}
    attempt = current.get('attempt_dir')
    if not attempt:
        return
    capture(runner, state, now)
    now = time.time() if now is None else now
    path, guard = paths(attempt)
    with lock(guard):
        journal = read_json(path, {'events': [], 'notifications': []})
        pending = [e for e in journal['events'] if e['processed_at'] is None
                   and e['dispatch'] == current.get('dispatch_id')]
        if not pending:
            return
        if any(now - e['created_at'] >= 300 and not e.get('warned') for e in pending):
            for event in pending:
                event['warned'] = True
            atomic_json(path, journal)  # At most one warning, including after restart.
            runner.notify('Координатор не обработал события более 5 минут', current.get('item', ''))
        if (state.get('paused') or state.get('stop_requested') or state.get('control_uncertain')
                or current.get('failed')):
            return
        # Read-only reconciliation; absent receipt never authorizes another send.
        for notification in journal['notifications']:
            if notification['status'] not in ('sending', 'unknown', 'accepted'):
                continue
            if now - notification.get('receipt_checked_at', 0) < 15:
                continue
            notification['receipt_checked_at'] = now
            try:
                receipt = runner.orca('orchestration', 'request-show', '--request', notification['id'])
                notification['receipt_check'] = receipt
                envelope = receipt.get('receipt') or {}
                result = envelope.get('result', envelope)
                if receipt.get('state') == 'completed' and envelope.get('ok') is not False:
                    record_receipt(notification, result)
            except Exception as exc:
                notification['receipt_error'] = str(exc)
        atomic_json(path, journal)
        # An unresolved submission cannot be repeated or overlapped by another batch.
        if any(n['status'] in ('sending', 'unknown', 'accepted') and
               any(e['id'] in n['events'] for e in pending) for n in journal['notifications']):
            return
        try:
            data = runner.orca('orchestration', 'worker-show', '--dispatch', current['dispatch_id'])
        except Exception:
            return  # Unavailable runtime must not cause input or stop the runner.
        dispatch, terminal = data.get('dispatch', {}), data.get('terminal') or {}
        if (dispatch.get('id') != current['dispatch_id'] or dispatch.get('status') != 'dispatched'
                or dispatch.get('assigneeHandle') != current.get('terminal')
                or terminal.get('handle') != current.get('terminal')
                or data.get('observation', {}).get('exactWorker') is not True
                or data.get('observation', {}).get('status') != 'live'
                or not terminal.get('incarnationId') or not terminal.get('ptyId')
                or dispatch.get('processIncarnation') != terminal['ptyId'] + ':' + terminal['incarnationId']
                or terminal.get('worktreeId') != runner.context.orca_id):
            return
        record = {'id': str(uuid.uuid4()), 'dispatch': current['dispatch_id'], 'terminal': terminal['handle'],
                  'incarnation': terminal['incarnationId'], 'events': [e['id'] for e in pending],
                  'run_id': state['run_id'], 'transport': 'dispatch',
                  'status': 'sending', 'created_at': now}
        # Persist intent before the only input operation; lost response remains unknown.
        journal['notifications'].append(record)
        atomic_json(path, journal)
        try:
            with runner.launch_permission():
                result = runner.orca('orchestration', 'send', '--run', state['run_id'],
                                     '--to', 'dispatch:' + current['dispatch_id'],
                                     '--subject', 'Pending child events', '--type', 'status', '--body',
                                     'Pending child events: run team-runner events list, process them, then '
                                     'team-runner events ack --dispatch ' + current['dispatch_id'] +
                                     ' --ids ' + ','.join(record['events']) + '.',
                                     '--retry-request', record['id'])
            record.update(status='unknown', receipt=result)
            record_receipt(record, result)
        except Exception as exc:
            record.update(status='unknown', error=str(exc), failure_receipt=getattr(exc, 'receipt', None))
        atomic_json(path, journal)
