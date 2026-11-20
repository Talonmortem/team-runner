"""Recoverable question relay over exact Orca Run mailboxes."""

import json
import uuid

from .runner_operations import UncertainOperation, checkpoint
from .runner_store import lock


ANCHOR_SUBJECT = 'Team Runner cascade inbox anchor'
ANCHOR_REQUEST_SUBJECT = 'Team Runner cascade child inbox ready'
ANCHOR_PENDING = 'inbox_anchor_pending'
WAKE_SUBJECT = 'Team Runner cascade answer recorded'


class InboxHistoryGap(RuntimeError):
    def __init__(self, run, anchor, limit, oldest, reason):
        self.details = dict(run=run, anchor=anchor, limit=limit, oldest=oldest)
        super().__init__(f'inbox_history_gap: {reason}; {json.dumps(self.details, ensure_ascii=False)}')


def intent(state, key, argv, *, mailbox=None, expected=None, request_id=None):
    """The enclosing checkout transaction can also advance an inbox checkpoint."""
    operations = state['operations']
    old = operations.get(key)
    args = list(argv)
    if old is None:
        old = operations[key] = dict(request_id=request_id or str(uuid.uuid4()), args=args,
                                    mailbox=mailbox, status='intent')
        if expected is not None:
            old['expected'] = expected
    elif ((old['args'], old['mailbox'], old.get('expected')) != (args, mailbox, expected)
          or request_id is not None and old['request_id'] != request_id):
        raise ValueError(f'cascade operation {key} changed identity')
    return old


def _address(run):
    return f'run:{run}'


def _destination(dispatch):
    return f'dispatch:{dispatch}'


def _position(row):
    return {key: row.get(key) for key in ('run_id', 'id', 'sequence')} if isinstance(row, dict) else None


def _question_key(position):
    return f"{position['run_id']}:{position['id']}"


def _window_contract(session, run, anchor):
    contract = getattr(session.backend, 'inbox_window', None)
    if (not isinstance(contract, tuple) or len(contract) != 2
            or type(contract[0]) is not int or contract[0] < 1
            or contract[1] not in ('asc', 'desc')):
        raise InboxHistoryGap(run, anchor, None, None, 'Orca inbox window semantics unconfirmed')
    return contract


def _window(session, run, anchor, limit, order, mailbox, *, address=None):
    address = address or _address(run)
    receipt = session.call('orchestration', 'inbox', '--terminal', address,
                           '--limit', str(limit), mailbox=mailbox)
    rows = receipt.get('messages') if isinstance(receipt, dict) else None
    oldest = _position(rows[-1 if order == 'desc' else 0]) if isinstance(rows, list) and rows else None
    if (not isinstance(rows, list) or len(rows) > limit
            or type(receipt.get('count')) is not int or receipt['count'] != len(rows)):
        raise InboxHistoryGap(run, anchor, limit, oldest, 'malformed inbox window')
    ordered = list(reversed(rows)) if order == 'desc' else rows
    ids = set()
    last = 0
    for row in ordered:
        if (not isinstance(row, dict) or row.get('run_id') != run
                or row.get('to_handle') != address or not isinstance(row.get('id'), str)
                or not row['id'] or row['id'] in ids or type(row.get('sequence')) is not int
                or row['sequence'] <= last):
            raise InboxHistoryGap(run, anchor, limit, oldest, 'inbox identity or ordering changed')
        ids.add(row['id'])
        last = row['sequence']
    return ordered


def _since(session, run, anchor, mailbox):
    maximum, order = _window_contract(session, run, anchor)
    if not anchor or anchor.get('run_id') != run:
        raise InboxHistoryGap(run, anchor, None, None, 'saved anchor missing')
    limit = min(32, maximum)
    while True:
        rows = _window(session, run, anchor, limit, order, mailbox)
        for index, row in enumerate(rows):
            if row['id'] == anchor['id']:
                if _position(row) != anchor:
                    raise InboxHistoryGap(run, anchor, limit, _position(rows[0]), 'anchor identity changed')
                return rows[index + 1:]
        if limit == maximum:
            raise InboxHistoryGap(run, anchor, limit, _position(rows[0]) if rows else None,
                                  'saved anchor is outside the retained window')
        limit = min(2 * limit, maximum)


def _orchestration(verb, run, mailbox, *args):
    return ('orchestration', verb, '--run', run, *args,
            *(('--from', mailbox) if mailbox and not mailbox.startswith('orca_session_id:') else ()))


def _send(run, mailbox, to, subject, body):
    return _orchestration('send', run, mailbox, '--to', to, '--type', 'status',
                          '--subject', subject, '--body', body)


def _message(run, to, **fields):
    return dict(run_id=run, to_handle=to, **fields)


def _valid_message(row, expected):
    return (isinstance(row, dict) and isinstance(row.get('id'), str) and bool(row['id'])
            and all(row.get(key) == value for key, value in expected.items()))


def validate_receipt(operation, receipt):
    """Validate the stored scope for live delivery and stop reconciliation alike."""
    expected = operation['expected']
    mutation = receipt.get('mutation') if isinstance(receipt, dict) else None
    if not isinstance(mutation, dict) or mutation.get('requestId') != operation['request_id']:
        raise UncertainOperation('cascade message request identity unconfirmed')
    if 'receipt' in expected:
        if any(receipt.get(key) != value for key, value in expected['receipt'].items()):
            raise UncertainOperation('cascade answer retrieval receipt changed')
        return
    if 'question' in expected:
        question_id = receipt.get('messageId')
        if (receipt.get('pending') is not True or not isinstance(question_id, str)
                or not question_id or receipt.get('threadId') != question_id):
            raise UncertainOperation('cascade question receipt identity unconfirmed')
        return
    if not _valid_message(receipt.get('message'), expected['message']):
        raise UncertainOperation('cascade message receipt scope changed')
    if expected.get('redirect'):
        warnings = receipt.get('warnings') or []
        if not any(isinstance(warning, dict) and warning.get('code') == 'recipient_run_bound_redirect'
                   and warning.get('recipient') == expected['redirect'] for warning in warnings):
            raise UncertainOperation('cascade wake redirect did not identify the assigned Dispatch')


def _question_payload(row, role, *, body=None, options=None):
    try:
        payload = json.loads(row['payload'])
    except (KeyError, TypeError, json.JSONDecodeError):
        raise UncertainOperation('question lacks a structured Orca Dispatch payload') from None
    if (not isinstance(payload, dict) or payload.get('dispatchId') != role['dispatch']
            or payload.get('taskId') != role['task'] or row.get('from_handle') != _destination(role['dispatch'])
            or row.get('thread_id') != row['id']
            or not isinstance(row.get('body'), str) or not row['body'].strip()
            or payload.get('question') != row['body'] or (body is not None and body != row['body'])):
        raise UncertainOperation('question sender or payload does not match the assigned worker')
    flags = payload.get('options')
    if (not isinstance(flags, list) or any(not isinstance(flag, str) or not flag or ',' in flag
                                           or flag != flag.strip() for flag in flags)
            or (options is not None and options != flags)):
        raise UncertainOperation('question options cannot be recovered exactly')
    return flags


def _confirm_forward(session, operation, receipt):
    """Orca's ask receipt identifies a Message ID; the stored row proves its scope."""
    expected = operation['expected']['question']
    run = expected['role']['run']
    maximum, order = _window_contract(session, run, None)
    rows = _window(session, run, None, maximum, order, operation['mailbox'])
    row = next((row for row in rows if row['id'] == receipt['messageId']), None)
    if row is None or row.get('type') != 'question':
        raise UncertainOperation('forwarded question is missing from the parent Run mailbox')
    _question_payload(row, expected['role'], body=expected['body'], options=expected['options'])


def verify_operation(session, operation, receipt):
    validate_receipt(operation, receipt)
    if 'question' in operation['expected']:
        _confirm_forward(session, operation, receipt)


def recover_retrieval(session, operation, receipt):
    """Recover an acknowledged read whose final reply was lost inside Orca."""
    expected = operation.get('expected', {})
    answer_row = expected.get('answer_message')
    if (not answer_row or not isinstance(receipt, dict) or receipt.get('accepted') is not True
            or receipt.get('answer') is not None or receipt.get('answerMessageId') is not None):
        return receipt
    request = receipt.get('mutation') or {}
    if (request.get('requestId') != operation['request_id']
            or receipt.get('messageId') != expected['receipt']['messageId']
            or receipt.get('threadId') != receipt['messageId']):
        raise UncertainOperation('cascade answer retrieval acknowledgement changed')
    run = answer_row['run_id']
    maximum, order = _window_contract(session, run, None)
    rows = _window(session, run, None, maximum, order, operation['mailbox'],
                   address=answer_row['to_handle'])
    if not any(_valid_message(row, answer_row) for row in rows):
        raise UncertainOperation('accepted ask resume has no matching saved answer in its Dispatch mailbox')
    return {**receipt, **expected['receipt'], 'reconciledFromAnswer': answer_row['id']}


def _effect(session, key, *, roles=(), retrieve=False):
    operation = session.read()['operations'][key]
    mailbox = operation['mailbox']

    def verify_destinations():
        for role in roles:
            _dispatch(session, role, mailbox)

    def recovery(operation):
        receipt = session.backend.reconcile(session, operation)
        if receipt is None:
            verify_destinations()
        return recover_retrieval(session, operation, receipt) if retrieve else receipt

    if operation['status'] == 'intent':
        verify_destinations()
    return session.effect(key, recovery=recovery,
                          validate=lambda result: verify_operation(session, operation, result))


def _dispatch(session, role, mailbox):
    """Orca send can follow a finished worker. Prove the original Dispatch is active first."""
    observed = session.call('orchestration', 'worker-show', '--dispatch', role['dispatch'], mailbox=mailbox)
    dispatch = observed.get('dispatch') or {}
    expected = {'id': role['dispatch'], 'runId': role['run'],
                'taskId': role['task'], 'assigneeHandle': role['terminal'], 'status': 'dispatched'}
    if any(dispatch.get(key) != value for key, value in expected.items()):
        raise UncertainOperation(f"cascade Dispatch {role['dispatch']} is stale or unexpected")
    terminal = observed.get('terminal')
    observation = observed.get('observation') or {}
    if (not isinstance(terminal, dict) or terminal.get('handle') != role['terminal']
            or observation.get('exactWorker') is not True or observation.get('status') != 'live'):
        raise UncertainOperation(f"cascade Dispatch {role['dispatch']} terminal is unconfirmed")
    identity = role.get('terminal_identity')
    if (not identity or any(terminal.get(key) != identity[key] for key in
                            ('handle', 'incarnationId', 'ptyId', 'worktreeId', 'tabId', 'leafId'))
            or dispatch.get('processIncarnation') != f"{identity['ptyId']}:{identity['incarnationId']}"
            or dispatch.get('assigneePaneKey') != f"{identity['tabId']}:{identity['leafId']}"):
        raise UncertainOperation(f"cascade Dispatch {role['dispatch']} terminal or incarnation changed")


def _binding(role):
    return {key: role[key] for key in ('run', 'task', 'dispatch', 'terminal', 'terminal_identity',
                                      'attempt', 'checkout', 'execution', 'role', 'worktree')
            if key in role}


def _anchor_key(feature_id):
    return f'mail:{feature_id}:anchor'


def ensure_anchor(session, feature_id):
    """The manager addresses the anchor to the coordinator's parent Dispatch."""
    state = session.read()
    feature = state['features'][feature_id]
    run = feature['run']
    coordinator = _binding(feature['roles']['coordinator'])
    parent = state['manager_run']
    if coordinator['run'] != parent:
        raise UncertainOperation('coordinator Dispatch belongs to an unexpected parent Run')
    mailbox = state['manager']['mailbox']
    key = _anchor_key(feature_id)
    with session.transaction() as current:
        scoped = current['features'][feature_id]
        if scoped.get('inbox_anchor') and current['operations'].get(key, {}).get('status') == 'completed':
            return scoped['inbox_anchor']
        if key not in current['operations']:
            if scoped.get('checkpoint'):
                raise InboxHistoryGap(run, scoped['checkpoint'], None, None, 'anchor operation missing')
            observed = session.call('orchestration', 'run-show', '--id', run, mailbox=mailbox).get('run') or {}
            if (observed.get('id') != run or observed.get('legacy') != 0
                    or observed.get('coordinator_handle') != coordinator['terminal']):
                raise UncertainOperation('child Run is not bound to the assigned coordinator')
            if any(role.get('dispatch') for name, role in scoped['roles'].items() if name != 'coordinator'):
                raise InboxHistoryGap(run, None, None, None, 'a sender already owns a Dispatch')
            _, order = _window_contract(session, run, None)
            existing = _window(session, run, None, 1, order, mailbox)
            if existing:
                raise InboxHistoryGap(run, None, 1, _position(existing[0]), 'new Run is not empty')
            request = str(uuid.uuid4())
            destination = _destination(coordinator['dispatch'])
            body = (f"team-runner cascade resume --execution {state['execution']} --feature "
                    f"{feature_id} --dispatch {coordinator['dispatch']}\n"
                    f"Inbox anchor request {request}")
            intent(current, key, _send(parent, mailbox, destination, ANCHOR_SUBJECT, body),
                   mailbox=mailbox, request_id=request,
                   expected={'redirect': destination, 'message': _message(
                       run, _address(run), type='status', subject=ANCHOR_SUBJECT, body=body, from_handle=mailbox)})
    receipt = _effect(session, key, roles=(coordinator,))
    position = _adopt_anchor(session, feature_id, mailbox)
    if position is None or position['id'] != receipt['message']['id']:
        raise InboxHistoryGap(run, position, None, None, 'anchor receipt does not match the child Run checkpoint')
    return position


def _adopt_anchor(session, feature_id, mailbox):
    """The child can prove its first row before the manager saves its receipt."""
    with session.transaction() as state:
        feature = state['features'][feature_id]
        run = feature['run']
        position = feature.get('inbox_anchor')
        if position:
            if position['run_id'] != run:
                raise InboxHistoryGap(run, position, None, None, 'anchor belongs to another Run')
            return position
        if feature.get('checkpoint'):
            raise InboxHistoryGap(run, feature['checkpoint'], None, None, 'original Run anchor is missing')
        operation = state['operations'].get(_anchor_key(feature_id))
        if not operation or operation['status'] not in ('calling', 'completed'):
            return None
        maximum, order = _window_contract(session, run, None)
        limit = min(32, maximum)
        rows = _window(session, run, None, limit, order, mailbox)
        if not rows and operation['status'] == 'calling':
            return None
        expected = operation['expected']['message']
        received = (operation.get('result') or {}).get('message')
        if (len(rows) != 1 or not _valid_message(rows[0], expected)
                or received is not None and rows[0]['id'] != received['id']):
            raise InboxHistoryGap(run, None, limit, _position(rows[0]) if rows else None,
                                  'anchor does not identify the first and only message')
        position = _position(rows[0])
        feature.update(inbox_anchor=position, checkpoint=position)
        return position


def _request_anchor(session, feature_id):
    state = session.read()
    feature = state['features'][feature_id]
    coordinator = _binding(feature['roles']['coordinator'])
    mailbox = coordinator['terminal']
    parent = state['manager_run']
    if coordinator['run'] != parent:
        raise UncertainOperation('coordinator Dispatch belongs to an unexpected parent Run')
    body = (f"team-runner cascade resume --execution {state['execution']}\n"
            f"Feature {feature_id} is waiting for its child Run inbox anchor.")
    key = _anchor_key(feature_id) + ':request'
    with session.transaction() as current:
        if current['features'][feature_id].get('inbox_anchor'):
            previous = current['operations'].get(key)
            if previous and previous['status'] == 'intent':
                previous['status'] = 'cancelled'
            if not previous or previous['status'] != 'calling':
                return
        intent(current, key, _send(parent, mailbox, _address(parent), ANCHOR_REQUEST_SUBJECT, body),
               mailbox=mailbox, expected={'message': _message(
                   parent, _address(parent), type='status', subject=ANCHOR_REQUEST_SUBJECT,
                   body=body, from_handle=mailbox)})
    _effect(session, key, roles=(coordinator,))


def _read_coordinator(session, feature_id):
    with session.transaction() as state:
        feature = state['features'][feature_id]
        run = feature['run']
        coordinator = _binding(feature['roles']['coordinator'])
        if coordinator['run'] != state['manager_run']:
            raise UncertainOperation('coordinator Dispatch belongs to an unexpected parent Run')
        mailbox = coordinator['terminal']
        rows = _since(session, run, feature.get('checkpoint'), mailbox)
        checkpoint('cascade-inbox:after_read')
        events = feature.setdefault('inbox_events', {})
        questions = state['questions']
        for row in rows:
            message_id = row['id']
            position = _position(row)
            question_key = _question_key(position)
            previous = events.get(message_id)
            if previous:
                if position != previous['position'] or previous['type'] != row.get('type'):
                    raise InboxHistoryGap(run, feature.get('checkpoint'), None, position,
                                          'previously recorded Message ID changed')
                continue
            events[message_id] = {'position': position, 'type': row.get('type')}
            if row.get('subject') == WAKE_SUBJECT:
                _record_observed_wake(state, feature_id, row)
            if row.get('type') != 'question':
                continue
            roles = [role for name, role in feature['roles'].items() if name != 'coordinator'
                     and role.get('dispatch') and row.get('from_handle') == _destination(role['dispatch'])]
            if len(roles) != 1 or roles[0]['run'] != run or question_key in questions:
                raise UncertainOperation('question does not belong to an assigned child Dispatch')
            role = _binding(roles[0])
            _dispatch(session, role, mailbox)
            flags = _question_payload(row, role)
            key = f'mail:{feature_id}:{message_id}:forward'
            args = _orchestration('ask', coordinator['run'], mailbox, '--to', _address(coordinator['run']),
                                  '--question', row['body'], *(('--options', ','.join(flags)) if flags else ()))
            intent(state, key, args, mailbox=mailbox, expected={
                'question': {'role': coordinator, 'body': row['body'], 'options': flags}})
            questions[question_key] = {
                'feature': feature_id, 'attempt': feature['attempt'], 'origin': role, 'coordinator': coordinator,
                'source': position, 'body': row['body'], 'options': flags, 'forward': key,
            }
        if rows:
            feature['checkpoint'] = _position(rows[-1])


def _check_question_scope(state, question):
    feature = state['features'][question['feature']]
    roles = feature['roles']
    origin = question['origin']
    coordinator = question['coordinator']
    if (feature['attempt'] != question['attempt'] or _binding(roles.get('coordinator', {})) != coordinator
            or state['manager_run'] != coordinator['run'] or feature['run'] != origin['run']
            or not any(_binding(role) == origin for name, role in roles.items() if name != 'coordinator')):
        raise UncertainOperation('cascade question scope no longer belongs to this attempt')
    return coordinator, origin


def _reply_args(run, mailbox, question, body):
    return _orchestration('reply', run, mailbox, '--id', question, '--body', body)


def _reply_expected(run, dispatch, question, body):
    return {'message': _message(run, _destination(dispatch), from_handle=_address(run),
                                thread_id=question, body=body)}


def _record_observed_wake(state, feature_id, row):
    for question in state['questions'].values():
        if question['feature'] != feature_id or not question.get('wake'):
            continue
        operation = state['operations'][question['wake']]
        if operation['status'] in ('calling', 'completed') and _valid_message(
            row, operation['expected']['message']
        ):
            question['wake_observed'] = _position(row)


def save_answer(session, parent_message_id, answer):
    if not isinstance(answer, str) or not answer.strip() or '\0' in answer:
        raise ValueError('cascade answer must contain text and cannot contain NUL')
    with session.transaction() as state:
        matches = [question for question in state['questions'].values()
                   if question.get('parent_message_id') == parent_message_id]
        if len(matches) != 1:
            raise ValueError('unknown cascade parent question')
        question = matches[0]
        coordinator, origin = _check_question_scope(state, question)
        mailbox = state['manager']['mailbox']
        if 'answer' in question:
            if question['answer'] != answer:
                raise ValueError('cascade answer is already saved')
            return
        reply_key = question['forward'] + ':manager-reply'
        wake_key = question['forward'] + ':wake'
        run = coordinator['run']
        intent(state, reply_key, _reply_args(run, mailbox, parent_message_id, answer), mailbox=mailbox,
               expected=_reply_expected(run, coordinator['dispatch'], parent_message_id, answer))
        destination = _destination(coordinator['dispatch'])
        wake_request = str(uuid.uuid4())
        body = (f"team-runner cascade resume --execution {state['execution']} --feature "
                f"{question['feature']} --dispatch {coordinator['dispatch']}\n"
                f"Wake request {wake_request}")
        intent(state, wake_key, _send(run, mailbox, destination, WAKE_SUBJECT, body), mailbox=mailbox,
               expected={'redirect': destination, 'message': _message(
                   origin['run'], _address(origin['run']), type='status',
                   subject=WAKE_SUBJECT, body=body, from_handle=mailbox)}, request_id=wake_request)
        question.update(answer=answer, manager_reply=reply_key, wake=wake_key)


def resume_manager(session):
    state = session.read()
    for feature_id, feature in state['features'].items():
        if feature.get('run'):
            ensure_anchor(session, feature_id)
    for question in state['questions'].values():
        wake_key = question.get('wake')
        if not wake_key or state['operations'][wake_key]['status'] == 'completed':
            continue
        coordinator, _ = _check_question_scope(state, question)
        _effect(session, question['manager_reply'], roles=(coordinator,))
        checkpoint('cascade-answer:after_manager_reply')
        _effect(session, wake_key, roles=(coordinator,))
    return [{'id': question['parent_message_id'], 'feature': question['feature'], 'question': question['body'],
             'options': question['options']}
            for question in session.read()['questions'].values()
            if question.get('parent_message_id') and 'answer' not in question]


def _forward_question(session, question_key, question, coordinator):
    if question.get('parent_message_id'):
        return
    receipt = _effect(session, question['forward'], roles=(coordinator,))
    with session.transaction() as current:
        persisted = current['questions'][question_key]
        if persisted.get('parent_message_id', receipt['messageId']) != receipt['messageId']:
            raise UncertainOperation('cascade question is linked to another parent message')
        persisted['parent_message_id'] = receipt['messageId']


def _deliver_answer(session, question_key, coordinator, origin):
    state = session.read()
    question = state['questions'][question_key]
    reply_key = question.get('manager_reply')
    if not reply_key or state['operations'][reply_key]['status'] != 'completed':
        return
    # The manager's wake is journaled separately. An early wake from Orca's
    # reply must not drive dependent work before that send is confirmed. A turn
    # started by the wake send can arrive before the manager persists its receipt.
    wake = state['operations'][question['wake']]
    if wake['status'] == 'calling':
        with lock(session.directory / 'effect.lock'):
            state = session.read()
    if state['operations'][question['wake']]['status'] != 'completed' and not question.get('wake_observed'):
        return
    origin_reply = question['forward'] + ':origin-reply'
    if state['operations'].get(origin_reply, {}).get('status') == 'completed':
        with session.transaction() as current:
            current['questions'][question_key]['delivered'] = True
        return
    mailbox = coordinator['terminal']
    answer_id = state['operations'][reply_key]['result']['message']['id']
    parent_message_id = question['parent_message_id']
    answer = question['answer']
    retrieval = question['forward'] + ':retrieve'
    with session.transaction() as current:
        _check_question_scope(current, question)
        intent(current, retrieval, _orchestration('ask', coordinator['run'], mailbox,
                                                   '--resume', parent_message_id, '--timeout-ms', '1000'),
               mailbox=mailbox, expected={'receipt': {'messageId': parent_message_id,
                                                       'threadId': parent_message_id,
                                                       'answerMessageId': answer_id, 'answer': answer,
                                                       'timedOut': False, 'cancelled': False,
                                                       'connectionLost': False},
                                           'answer_message': dict(
                                               state['operations'][reply_key]['expected']['message'],
                                               id=answer_id)})
    _effect(session, retrieval, roles=(coordinator,), retrieve=True)
    with session.transaction() as current:
        _check_question_scope(current, question)
        source_run = origin['run']
        message_id = question['source']['id']
        intent(current, origin_reply, _reply_args(source_run, mailbox, message_id, answer), mailbox=mailbox,
               expected=_reply_expected(source_run, origin['dispatch'], message_id, answer))
    _effect(session, origin_reply, roles=(origin,))
    with session.transaction() as current:
        current['questions'][question_key]['delivered'] = True


def resume_coordinator(session, feature_id):
    state = session.read()
    coordinator = state['features'][feature_id]['roles']['coordinator']
    if _adopt_anchor(session, feature_id, coordinator['terminal']) is None:
        _request_anchor(session, feature_id)
        if _adopt_anchor(session, feature_id, coordinator['terminal']) is None:
            return [ANCHOR_PENDING]
    else:
        request = state['operations'].get(_anchor_key(feature_id) + ':request')
        if request and request['status'] in ('intent', 'calling'):
            _request_anchor(session, feature_id)
    _read_coordinator(session, feature_id)
    state = session.read()
    for question_key, question in state['questions'].items():
        if question['feature'] != feature_id or question.get('delivered'):
            continue
        coordinator, origin = _check_question_scope(state, question)
        _forward_question(session, question_key, question, coordinator)
        _deliver_answer(session, question_key, coordinator, origin)
    return [question['source']['id'] for question in session.read()['questions'].values()
            if question['feature'] == feature_id and not question.get('delivered')]
