"""Team Runner's Orca-native cascade commands and feature progression."""

import json
import os
from pathlib import Path
from uuid import uuid4

from . import cascade_messages as messages
from . import cascade_verification as verification
from .cascade_role import ADDRESS, submit
from .cascade_store import Session, TERMINAL, head
from .runner_operations import checkpoint
from .runner_store import read_json


ROLES = ('implementer', 'tester', 'reviewer')
MANAGER_INSTRUCTION = (
    'After Orca wakes this session or you return to it, first run '
    'team-runner cascade resume --execution {execution}. Inspect the frozen step card '
    'and the evidence before calling cascade accept. Answer a forwarded question once '
    'with cascade reply and the answer file. Once ready work starts, end the turn '
    'and wait for Orca.'
)


def add_parser(subparsers):
    command = subparsers.add_parser('cascade', help='manage an Orca-native agent cascade')
    actions = command.add_subparsers(dest='cascade_action', required=True)
    for action in ('start', 'resume', 'reply', 'report', 'accept', 'stop'):
        parser = actions.add_parser(action)
        parser.add_argument('--execution', required=True)
        parser.add_argument('--json', action='store_true')
        if action in ('start', 'stop'):
            parser.add_argument('--project', required=True)
            parser.add_argument('--workspace', required=True)
        if action in ('resume', 'report', 'accept'):
            parser.add_argument('--feature', required=action != 'resume')
            parser.add_argument('--dispatch', required=action != 'resume')
        if action == 'start':
            parser.add_argument('--goal', type=Path, required=True)
            parser.add_argument('--allow-commits', action='store_true')
        elif action == 'stop':
            parser.add_argument('--operator', action='store_true')
        elif action == 'reply':
            parser.add_argument('--question', required=True)
            parser.add_argument('--answer-file', type=Path, required=True)
        elif action == 'report':
            parser.add_argument('--input', type=Path, required=True)


def _summary(session):
    state = session.read()
    return {
        'schema_version': 1, 'execution': session.execution, 'status': state['status'],
        'manager_run': state['manager_run'],
        'features': {
            item['id']: {
                'attempt': item.get('attempt'), 'base_sha': item.get('base_sha'),
                'materials': item['directory'],
                'accepted': item.get('accepted'),
                'verification': item['verification_receipts'],
                'reports': {role: report['outcome'] for role, report in item['reports'].items()},
                'assignments': {role: a['dispatch'] for role, a in item.get('roles', {}).items()
                                if a.get('dispatch')},
            }
            for item in state['features'].values()
        },
        'termination': state.get('termination'),
    }


def _manager(session, state):
    if state['manager'] != session.backend.manager_identity(session.context):
        raise ValueError('cascade manager identity mismatch')


def _operator(session, state):
    if os.environ.get('TEAM_RUNNER_ATTEMPT') or os.environ.get('TEAM_RUNNER_CASCADE_ATTEMPT'):
        raise ValueError('operator must be outside a child attempt')
    paths = (session.catalog.path, session.directory, session.registry, session.index)
    if read_json(session.context.lease_path) == session.lease:
        paths += (session.context.lease_path,)
    if state['uid'] != os.geteuid() or any(path.is_symlink() or path.stat().st_uid != state['uid'] for path in paths):
        raise ValueError('cascade operator does not own catalog and execution state')
    from .catalog import git_root
    if git_root(session.context.repo) != session.context.repo:
        raise ValueError('cascade checkout identity changed')


def _role(session, state, feature_id, dispatch, *, coordinator=False):
    feature = state['features'].get(feature_id)
    assignments = [a for a in (feature or {}).get('roles', {}).values()
                   if a.get('dispatch') == dispatch and (not coordinator or a['role'] == 'coordinator')]
    if len(assignments) != 1 or not dispatch:
        raise ValueError('cascade dispatch does not identify a current assignment')
    assignment = assignments[0]
    observed = session.backend.assignment_identity(session, state, feature, assignment)
    return feature, assignment, observed['dispatch']['status']


def _card(state, feature):
    spec = next(row for row in state['snapshot']['queue']['queue'] if row['id'] == feature['id'])
    return state['snapshot']['cards'][f"{feature['id']}/{spec['items'][0]['id']}"]


def reserve_ready(session):
    with session.transaction() as state:
        for spec in state['snapshot']['queue']['queue']:
            feature_id = spec['id']
            if feature_id in state['features']:
                continue
            base = state['base_sha']
            if spec['depends_on']:
                predecessor = state['features'].get(spec['depends_on'][0], {})
                accepted = predecessor.get('accepted')
                if accepted is None:
                    continue
                base = accepted['sha']
            attempt = str(uuid4())
            directory = session.directory / 'attempts' / attempt
            state['features'][feature_id] = {
                'id': feature_id, 'attempt': attempt, 'directory': str(directory),
                'base_sha': base, 'roles': {}, 'reports': {}, 'run': None,
                'verification': {}, 'verification_receipts': [],
            }
        return list(state['features'])


def _start_ready(session):
    for feature_id in reserve_ready(session):
        if session.read()['features'][feature_id].get('accepted'):
            continue
        session.backend.ensure_attempt(session, feature_id)
        session.claim_child(feature_id)
        session.backend.ensure_role(session, feature_id, 'coordinator')


def _freeze_candidate(session, feature_id):
    # Serialize publication with stop and other coordinators. This does not invoke Orca.
    with session.transaction() as state:
        feature = state['features'][feature_id]
        if 'candidate' in feature:
            return
        coverage = verification.coverage_from_card(_card(state, feature))
        for check in state['profile']['checks']:
            if not any(command['cwd'] == check['cwd'] and command['argv'] == check['argv']
                       for command in coverage['checks']):
                raise ValueError('frozen card is missing a configured profile check')
        candidate = verification.freeze_candidate(
            feature['checkout'], feature['base_sha'], feature['directory'], coverage=coverage,
        )
        if feature['reports']['implementer']['sha'] != candidate['sha']:
            raise ValueError('implementer report SHA differs from the frozen candidate')
        feature['candidate'] = candidate


def _coordinator(session, feature_id, dispatch):
    _, _, status = _role(session, session.read(), feature_id, dispatch, coordinator=True)
    if status != 'dispatched':
        raise ValueError('coordinator Dispatch is no longer active')
    session.backend.ensure_run(session, feature_id)
    pending = messages.resume_coordinator(session, feature_id)
    if pending:
        if pending == [messages.ANCHOR_PENDING]:
            return dict(_summary(session), inbox_anchor_pending=True)
        return dict(_summary(session), pending_questions=pending)
    for index, role in enumerate(ROLES):
        state = session.read()
        feature = state['features'][feature_id]
        if index:
            _freeze_candidate(session, feature_id)
        session.backend.ensure_role(session, feature_id, role)
        if role == 'tester':
            _run_verifications(session, feature_id)
        feature = session.read()['features'][feature_id]
        if not _succeeded(session, feature, role):
            return _summary(session)
    result = _summary(session)
    result['ready_for_coordinator_report'] = True
    return result


def _run_verifications(session, feature_id):
    feature = session.read()['features'][feature_id]
    assignment = feature['roles']['tester']
    candidate = feature['candidate']
    for index, _ in enumerate(candidate['checks']):
        key = f'{feature_id}:verify:{index}'
        with session.transaction() as state:
            operation = state['operations'].setdefault(key, {
                'request_id': str(uuid4()), 'args': ['verification', 'run'],
                'mailbox': None, 'status': 'intent', 'feature': feature_id, 'check': index,
            })
        session.effect(
            key,
            invoke=lambda: verification.run_check(
                feature['checkout'], candidate, assignment, index, feature['directory'],
                operation_id=operation['request_id'],
            ),
            recovery=lambda operation: _recover_check(session, operation),
        )
    with session.transaction() as state:
        state['features'][feature_id]['verification_receipts'] = [
            state['operations'][f'{feature_id}:verify:{index}']['result']
            for index, _ in enumerate(candidate['checks'])
        ]


def _recover_check(session, operation):
    feature = session.read()['features'][operation['feature']]
    return verification.recover_check(
        feature['checkout'], feature['candidate'], feature['roles']['tester'], operation['check'],
        feature['directory'], operation['request_id'],
    )


def _succeeded(session, feature, role):
    assignment = feature['roles'].get(role, {})
    report = feature['reports'].get(role, {})
    if not assignment.get('dispatch') or report.get('outcome') != 'succeeded':
        return False
    operation = session.read()['operations'].get(f"{feature['id']}:report:{assignment['dispatch']}", {})
    return (operation.get('status') == 'completed' and report.get('binding') == _binding(assignment)
            and session.backend.role_done(session, assignment))


def _binding(assignment):
    return {key: assignment[key] for key in ('role', 'run', 'task', 'dispatch', 'attempt', 'checkout', 'execution')}


def _report_payload(payload, assignment, directory):
    if (not isinstance(payload, dict)
            or set(payload) - {'role', 'outcome', 'sha', 'evidence', 'summary', 'files_modified', 'checks', 'issues', 'next_step'}
            or payload.get('role') != assignment['role']
            or payload.get('outcome') not in ('succeeded', 'failed', 'changes_requested')
            or not {'summary', 'evidence'} <= payload.keys()
            or not isinstance(payload.get('sha'), str)
            or payload['sha'] != head(assignment['checkout'])):
        raise ValueError('report role, outcome or SHA does not match the current assignment')
    for key in ('summary', 'issues', 'next_step'):
        value = payload.get(key, 'none')
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError(f'{key} must be concise text')
    root = Path(directory).resolve(strict=True)
    for key in ('evidence', 'checks', 'files_modified'):
        paths = payload.get(key, [])
        if not isinstance(paths, list):
            raise ValueError(f'{key} must be a list of paths')
        for value in paths:
            if not isinstance(value, str) or not value or '\x00' in value:
                raise ValueError(f'{key} must be a list of paths')
            path = Path(value)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError(f'expected relative report path: {value}')
            if key != 'files_modified':
                resolved = (root / path).resolve()
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    raise ValueError(f'missing or external report evidence: {value}')
    return dict(payload, binding=_binding(assignment))


def _report(session, feature_id, dispatch, payload):
    state = session.read()
    feature, assignment, status = _role(session, state, feature_id, dispatch)
    role = assignment['role']
    report = _report_payload(payload, assignment, feature['directory'])
    key = f'{feature_id}:report:{dispatch}'
    pending = False
    if status == 'pending' and 'image' in (assignment.get('readonly') or {}):
        operation = state['operations'].get(f'{feature_id}:{role}:dispatch', {})
        result = operation.get('result') or {}
        pending = (operation.get('status') == 'completed' and result.get('dispatchId') == dispatch
                   and session.backend.container_input_accepted(
                       result, operation.get('request_id'), assignment['terminal_identity'],
                   ))
    if not pending and status != 'dispatched' and (
        status not in ('completed', 'failed') or key not in state['operations']
    ):
        raise ValueError('report Dispatch is no longer active')
    if role == 'coordinator' and report['outcome'] == 'succeeded':
        if any(not _succeeded(session, feature, role) for role in ROLES):
            raise ValueError('coordinator report requires all three successful worker reports')
    args = ['orchestration', 'send', '--from', assignment['terminal'], '--run', assignment['run'],
            '--type', 'worker_done', '--task-id', assignment['task'], '--dispatch-id', dispatch,
            '--outcome', 'succeeded' if report['outcome'] == 'succeeded' else 'failed',
            '--subject', 'Cascade worker result', '--body', report.get('summary', 'none')]
    with session.transaction() as current:
        feature = current['features'][feature_id]
        if (role == 'coordinator' and report['outcome'] == 'succeeded'
                and any(question['feature'] == feature_id and not question.get('delivered')
                        for question in current['questions'].values())):
            raise ValueError('coordinator report is blocked by an outstanding cascade question')
        old = feature['reports'].get(role)
        if old is not None and old != report:
            raise ValueError('report input changed after submission')
        if role in ('tester', 'reviewer'):
            if report['outcome'] == 'succeeded' and role == 'tester':
                receipts = feature['verification_receipts']
                if not receipts or any(receipt['result'] != 'PASS'
                                       or receipt['reference'] not in report.get('evidence', [])
                                       for receipt in receipts):
                    raise ValueError('tester success requires linked PASS receipts for all frozen checks')
            feature['roles'][role]['after'] = verification.boundary(feature['checkout'], feature['candidate'])
        feature['reports'][role] = report
        operation = messages.intent(current, key, args, mailbox=assignment['terminal'])

    def validate(result):
        message = session.backend.validate_report(session, assignment, result)
        payload = message['payload']
        if isinstance(payload, str):
            payload = json.loads(payload)
        if (message.get('body') != report['summary']
                or message.get('subject') != 'Cascade worker result'
                or payload.get('outcome') != ('succeeded' if report['outcome'] == 'succeeded' else 'failed')
                or (result.get('mutation') or {}).get('requestId') != operation['request_id']):
            raise ValueError('Orca worker_done receipt differs from the stored report')

    return session.effect(key, validate=validate)


def _accept(session, feature_id, dispatch):
    state = session.read()
    _manager(session, state)
    feature = state['features'].get(feature_id, {})
    if feature.get('roles', {}).get('coordinator', {}).get('dispatch') != dispatch:
        raise ValueError('accept requires the current coordinator Dispatch')
    if feature.get('accepted'):
        return feature['accepted']
    if any(not _succeeded(session, feature, role) for role in ('coordinator', *ROLES)):
        raise ValueError('accept requires successful worker_done and reports from the current coordinator and all three roles')
    with session.transaction() as current:
        feature = current['features'][feature_id]
        if feature.get('accepted'):
            return feature['accepted']
        if any(question['feature'] == feature_id and not question.get('delivered')
               for question in current['questions'].values()):
            raise ValueError('acceptance is blocked by an outstanding cascade question')
        if feature['reports']['coordinator']['sha'] != feature['candidate']['sha']:
            raise ValueError('coordinator report does not identify the tested candidate')
        evidence = verification.accept(
            feature['checkout'], feature['base_sha'], feature['candidate'],
            feature['roles'], feature['reports'], feature['verification_receipts'],
            directory=feature['directory'],
        )
        feature['accepted'] = dict(evidence, sha=feature['candidate']['sha'],
                                   attempt=feature['attempt'], dispatch=dispatch)
        return feature['accepted']


def _resume_manager(session):
    state = session.read()
    _manager(session, state)
    if state['status'] in TERMINAL:
        session.finish(None)
        return _summary(session)
    if state['status'] != 'active':
        raise ValueError('cascade is stopping; continue with cascade stop')
    session.backend.ensure_manager(session)
    questions = messages.resume_manager(session)
    _start_ready(session)
    state = session.read()
    if all(state['features'].get(row['id'], {}).get('accepted') for row in state['snapshot']['queue']['queue']):
        result = session.backend.cleanup(session, stopping=False)
        session.finish(result)
    output = _summary(session)
    output['questions'] = questions
    output['instruction'] = MANAGER_INSTRUCTION.format(execution=session.execution)
    return output


def _stop(session, operator):
    state = session.read()
    (_operator if operator else _manager)(session, state)
    if state['status'] not in TERMINAL:
        session.begin_stop()
        session.settle(lambda operation: _recover_check(session, operation))
        result = session.backend.cleanup(session, stopping=True, operator=operator)
        checkpoint('cascade-stop:before_terminal_record')
        session.finish(result, stopping=True)
    else:
        session.finish(None)
    return _summary(session)


def execute(args, catalog, backend=None):
    if backend is None:
        from .cascade_orca import OrcaBackend
        backend = OrcaBackend()
    action = args.cascade_action
    if action == 'report' and os.environ.get(ADDRESS):
        return submit(args.execution, args.feature, args.dispatch, read_json(args.input))
    if action == 'start':
        context = catalog.context(args.project, args.workspace)
        session = Session(catalog, context, args.execution, backend)
        session.initialize(args.goal, args.allow_commits)
        return _resume_manager(session)
    session = Session.open(catalog, args.execution, backend, getattr(args, 'project', None),
                           getattr(args, 'workspace', None))
    if action == 'stop':
        return _stop(session, args.operator)
    if action == 'resume':
        if bool(args.feature) != bool(args.dispatch):
            raise ValueError('--feature and --dispatch must be supplied together')
        return _coordinator(session, args.feature, args.dispatch) if args.feature else _resume_manager(session)
    if action == 'reply':
        _manager(session, session.read())
        messages.save_answer(session, args.question, args.answer_file.read_text(encoding='utf-8'))
        return dict(_summary(session), questions=messages.resume_manager(session))
    if action == 'report':
        return _report(session, args.feature, args.dispatch, read_json(args.input))
    if action == 'accept':
        return _accept(session, args.feature, args.dispatch)
    raise ValueError('unknown cascade action')
