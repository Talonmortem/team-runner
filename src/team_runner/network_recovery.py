"""Bounded recovery of proved provider network failures via the runner owner queue."""
import copy
import json
from pathlib import Path
import re
import time
import uuid

from .runner_store import atomic_json, enqueue, lock, read_json
from . import runner_operations as operations

DELAYS = (30, 120)
NETWORK = re.compile(r'network|connection (?:reset|closed|refused)|error sending request|failed to connect|'
                     r'stream disconnected|timed? out|\b(?:502|503|504)\b|dns|error decoding response body', re.I)


def document(runner):
    from .protocol import version, ContractError
    value = read_json(runner.state_dir / 'network-recovery.json', {'schema_version': 1, 'sessions': {}})
    version(value)
    if not isinstance(value.get('sessions'), dict) or any(not isinstance(s, dict) for s in value['sessions'].values()):
        raise ContractError('invalid_recovery', 'Invalid network recovery journal')
    return value


def _bound(text, agent):
    from .runner_metrics import PREAMBLE
    return (text.startswith(PREAMBLE) and 'Your task ID is: ' + agent['task_id'] + '\n' in text
            and '--from ' + agent['terminal'] + ' --type worker_done' in text
            and '--task-id ' + agent['task_id'] + ' --dispatch-id ' + agent['dispatch_id'] + ' --outcome' in text)


def codex_failure(runner, agent, sessions=None):
    """Only task_complete.error from a uniquely bound local rollout is evidence."""
    root = Path(sessions) if sessions else Path.home() / '.codex/sessions'
    candidates = sorted(root.glob('**/rollout-*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[:40]
    matches = []
    for path in candidates:
        bound, directory, failure = False, None, None
        try:
            with path.open() as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    p = event.get('payload') or {}
                    if event.get('type') == 'session_meta':
                        directory = p.get('cwd')
                    text = p.get('message', '') if p.get('type') == 'user_message' else ''
                    if event.get('type') == 'response_item' and p.get('role') == 'user':
                        text = ''.join(c.get('text', '') for c in p.get('content', []) if isinstance(c, dict))
                    if text and _bound(text, agent):
                        bound, failure = True, None
                    elif text and text.startswith('You are working inside Orca, a multi-agent IDE.'):
                        bound, failure = False, None
                    if not bound:
                        continue
                    if p.get('type') == 'task_started':
                        failure = None
                    if p.get('type') == 'task_complete':
                        error = p.get('error') or {}
                        message = error.get('message', '') if isinstance(error, dict) else ''
                        failure = {'id': event.get('timestamp'), 'error': message[:500], 'source': str(path)} if NETWORK.search(message) else None
            if bound and directory and Path(directory).resolve() == runner.repo.resolve():
                matches.append(failure)
        except (OSError, ValueError, TypeError):
            continue
    return matches[0] if len(matches) == 1 and matches[0] and matches[0]['id'] else None


def failure(runner, agent, data):
    """Identity is checked independently of provider error text."""
    runner.launcher.verify_terminal(agent, data)
    dispatch = data.get('dispatch') or {}
    if (dispatch.get('id') != agent['dispatch_id'] or dispatch.get('status') != 'dispatched'
            or data.get('observation', {}).get('exactWorker') is not True):
        return None
    # Runtime adapters can supply a structured failure, with a distinct event ID.
    error = (data.get('worker') or {}).get('lastError')
    if isinstance(error, dict) and error.get('id') and error.get('code') in (
            'network_error', 'connection_reset', 'request_timeout', 'provider_unavailable'):
        return {'id': error['id'], 'error': error['code'], 'source': 'exact worker-show'}
    if (agent.get('requested') or {}).get('agent') == 'opencode2':
        return opencode_failure(runner, agent)
    if (agent.get('requested') or {}).get('agent') == 'codex':
        return codex_failure(runner, agent)
    return None


def opencode_failure(runner, agent, database=None):
    from contextlib import closing
    import os
    import sqlite3
    path = Path(database) if database else Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'opencode/opencode.db'
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            bindings = [(sid, seq) for sid, seq, raw in conn.execute(
                "SELECT m.session_id,m.seq,m.data FROM session_message m JOIN session_v2 s ON s.id=m.session_id "
                "WHERE s.directory=? AND m.type='user' AND instr(m.data,?)>0", (str(runner.repo.resolve()), agent['dispatch_id']))
                if _bound(json.loads(raw).get('text', ''), agent)]
            if len(bindings) != 1:
                return None
            sid, seq = bindings[0]
            fact = None
            for kind, seq, raw in conn.execute('SELECT type,seq,data FROM session_message WHERE session_id=? AND seq>? ORDER BY seq', (sid, seq)):
                message = json.loads(raw)
                if kind == 'user' and message.get('text', '').startswith('You are working inside Orca, a multi-agent IDE.'):
                    return None
                if kind == 'user':
                    fact = None
                if kind != 'assistant':
                    continue
                error = message.get('error') or {}
                text = json.dumps(error)
                fact = {'id': sid + ':' + str(seq), 'error': text[:500], 'source': str(path)} if error and NETWORK.search(text) else None
            return fact
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None


def permitted(runner, state, expected):
    from .autonomy import policy
    from .goal_intent import read
    from .recovery import settings
    intent = read(runner)
    cur = state.get('current') or {}
    return (policy(runner)['enabled'] and settings(runner)['enabled'] and intent.get('desired_state') == 'running'
            and not state.get('paused') and not state.get('stop_requested') and not state.get('control_uncertain')
            and expected == {'identity': runner.context.identity(), 'generation': intent.get('generation'),
                             'epoch': intent.get('epoch'), 'attempt': cur.get('attempt_dir')})


def supervise(supervisor, runner, state):
    with lock(runner.state_dir / 'network-recovery.lock'):
        return _supervise(supervisor, runner, state)


def _supervise(supervisor, runner, state):
    """Observe/schedule only; all mutations execute under runner.lock."""
    from .autonomy import policy
    from .goal_intent import read
    from .recovery import _unknown
    if not policy(runner)['enabled']:
        return False
    cur = state.get('current') or {}
    if not cur.get('attempt_dir'):
        return False
    path = runner.state_dir / 'network-recovery.json'
    journal = document(runner)
    intent = read(runner)
    expected = {'identity': runner.context.identity(), 'generation': intent.get('generation'),
                'epoch': intent.get('epoch'), 'attempt': cur['attempt_dir']}
    observer_state = state
    uncertain_request = None
    if state.get('control_uncertain'):
        uncertain = read_json(runner.state_dir / 'commands' / (state['control_uncertain'] + '.json'), {})
        original = uncertain
        if uncertain.get('action') == 'network-reconcile':
            original = read_json(runner.state_dir / 'commands' / (uncertain.get('data', {}).get('request', '') + '.json'), {})
        if original.get('action') == 'network-recover' and original.get('data', {}).get('scope') == expected:
            uncertain_request = original['id']
            observer_state = {k: v for k, v in state.items() if k != 'control_uncertain'}
    if not permitted(runner, observer_state, expected):
        return False
    registry = runner.launcher.registry(cur['attempt_dir'])
    retained = {a.get('network_root', a.get('id')) for a in registry['agents']
                if a.get('status') not in ('released', 'stopped', 'superseded')}
    for session in journal['sessions'].values():
        if session.get('scope') == expected and session.get('root') not in retained:
            session.update(status='historical', next_check_at=None, reason='registered session completed')
    due = any(s.get('status') == 'waiting' and (s.get('next_check_at') or 0) <= supervisor.clock()
              for s in journal['sessions'].values())
    if supervisor.clock() - journal.get('checked_at', -100) < 15 and not due:
        return any(s.get('scope') == expected and s.get('status') in ('waiting', 'queued', 'needs_action')
                   for s in journal['sessions'].values())
    journal['checked_at'] = supervisor.clock()
    for session in journal['sessions'].values():
        if session.get('scope') != expected or session.get('status') == 'cancelled':
            continue
        command_id = session.get('command_id')
        if not command_id:
            continue
        command = read_json(runner.state_dir / 'commands' / (command_id + '.json'), {})
        if not command:
            enqueue(runner.state_dir, 'network-recover', session['command_data'], command_id)
        elif command.get('status') in ('error', 'unknown'):
            session.update(status='needs_action', next_check_at=None, reason=command.get('error') or 'recovery outcome unknown')
            if command.get('status') == 'unknown' and uncertain_request == command_id:
                reconcile_count = session.get('reconcile_checks', 0)
                reconcile_id = str(uuid.uuid5(uuid.NAMESPACE_URL, command_id + ':reconcile:' + str(reconcile_count)))
                prior = read_json(runner.state_dir / 'commands' / (reconcile_id + '.json'), {})
                if prior.get('status') in ('error', 'unknown') and reconcile_count < 5:
                    reconcile_count += 1
                    session['reconcile_checks'] = reconcile_count
                    reconcile_id = str(uuid.uuid5(uuid.NAMESPACE_URL, command_id + ':reconcile:' + str(reconcile_count)))
                enqueue(runner.state_dir, 'network-reconcile', {'_target': cur['attempt_dir'],
                        'request': command_id, 'session': session['command_data']['session']}, reconcile_id)
        elif command.get('status') == 'done':
            session.update(status='observing', next_check_at=None)
            session.pop('command_id', None)
            session.pop('command_data', None)
    handled = False
    for agent in registry['agents']:
        if agent.get('status') not in ('active', 'waiting_child') or not agent.get('dispatch_id'):
            continue
        root = agent.get('network_root', agent['id'])
        key = str(cur['attempt_dir']) + ':' + root
        session = journal['sessions'].get(key)
        try:
            data = runner.launcher.observe_agent(agent)
            fact = failure(runner, agent, data)
        except (OSError, RuntimeError):
            fact = None
        if not session and not fact:
            continue
        handled = True
        if not session:
            session = {'root': root, 'scope': expected, 'resumes': 0, 'replacements': 0,
                       'failures': [], 'status': 'observing', 'dispatch': agent['dispatch_id']}
            journal['sessions'][key] = session
        # Explicitly resumed authorization may observe a new failure. Keep spent
        # counters and old failure IDs; cancelled backoff never reauthorizes itself.
        if session['scope'] != expected or session['status'] == 'cancelled':
            if fact and fact['id'] not in session['failures']:
                session.update(scope=expected, status='observing', next_check_at=None)
                session.pop('command_id', None)
                session.pop('command_data', None)
            else:
                session.update(status='cancelled', next_check_at=None, reason='goal authorization changed or recovery cancelled')
                continue
        if session.get('command_id'):
            continue
        if session['status'] in ('needs_action', 'cancelled'):
            continue
        if fact and fact['id'] not in session['failures']:
            session['failures'].append(fact['id'])
            session['failure'] = fact
            if session['resumes'] < 2:
                session.update(status='waiting', action='resume', next_check_at=supervisor.clock() + DELAYS[session['resumes']])
            elif session['replacements'] < 1:
                session.update(status='waiting', action='replace', next_check_at=supervisor.clock())
            else:
                session.update(status='needs_action', reason='network recovery budget exhausted', next_check_at=None)
        if session['status'] != 'waiting' or supervisor.clock() < session['next_check_at']:
            continue
        if not fact or fact['id'] != session['failure']['id']:
            session.update(status='observing', next_check_at=None)
            continue
        if _unknown(runner, state) or registry.get('fenced'):
            session.update(status='needs_action', next_check_at=None, reason='unknown effect or fenced attempt')
            continue
        if any(c.get('parent_agent') == agent['id'] and c.get('status') not in ('completed', 'stopped')
               for c in registry.get('graph_calls', [])):
            session.update(reason='waiting for open child call')
            continue
        if supervisor.stop_event.is_set() or not permitted(runner, runner.load_state(), expected):
            session.update(status='cancelled', next_check_at=None, reason='pause/stop cancelled recovery')
            continue
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, key + ':' + str(len(session['failures'])) + ':' + session['action']))
        data = {'_target': cur['attempt_dir'], 'session': key, 'scope': expected,
                'agent': agent['id'], 'dispatch': agent['dispatch_id'], 'failure': session['failure'],
                'action': session['action']}
        session.update(command_id=identifier, command_data=data, status='queued', next_check_at=None)
        atomic_json(path, journal)  # Persist queue identity before enqueue.
        enqueue(runner.state_dir, 'network-recover', data, identifier)
    atomic_json(path, journal)
    return any(s.get('scope') == expected and s.get('status') in ('waiting', 'queued', 'needs_action')
               for s in journal['sessions'].values())


def check_receipt(receipt, agent, request):
    message = receipt.get('message') or {}
    if (not message.get('id') or message.get('to_handle') != 'dispatch:' + agent['dispatch_id']
            or message.get('run_id') != agent['run_id']
            or receipt.get('mutation', {}).get('requestId') != request):
        raise operations.UncertainOperation('network message acceptance receipt identity unconfirmed')


def apply(runner, state, data, identifier):
    with lock(runner.state_dir / 'network-recovery.lock'):
        return _apply(runner, state, data, identifier)


def _apply(runner, state, data, identifier):
    """Revalidate every effect; never relaunch an item or change its frozen snapshot."""
    from .recovery import _unknown
    if not permitted(runner, runner.load_state(), data['scope']):
        raise ValueError('network recovery cancelled by policy, pause/stop or scope change')
    attempt = Path(data['_target'])
    with lock(attempt / 'agents.lock'):
        reg = runner.launcher.registry(attempt)
        agent = next((a for a in reg['agents'] if a['id'] == data['agent'] and a.get('dispatch_id') == data['dispatch']), None)
        if not agent or agent.get('status') not in ('active', 'waiting_child') or reg.get('fenced'):
            raise ValueError('network recovery session identity changed')
        if any(c.get('parent_agent') == agent['id'] and c.get('status') not in ('completed', 'stopped') for c in reg.get('graph_calls', [])):
            raise ValueError('network recovery waits for open child call')
        if _unknown(runner, state):
            raise ValueError('network recovery blocked by unknown effect')
        observed = runner.launcher.observe_agent(agent)
        if failure(runner, agent, observed) != data['failure']:
            raise ValueError('confirmed network failure no longer current')
        journal_path = runner.state_dir / 'network-recovery.json'
        journal = document(runner)
        session = journal['sessions'][data['session']]
        if session.get('command_id') != identifier or session['scope'] != data['scope']:
            raise ValueError('network recovery reservation mismatch')
        allowed = lambda: permitted(runner, runner.load_state(), data['scope'])
        if data['action'] == 'resume':
            if session['resumes'] >= 2:
                raise ValueError('network continuation budget exhausted')
            session['resumes'] += 1
            atomic_json(journal_path, journal)  # Reserve once before input; unknown receipts cannot authorize another input.
            with runner.launch_permission():
                if not allowed():
                    raise ValueError('network continuation cancelled')
                receipt = runner.launcher.effect(attempt, 'network-' + identifier, 'orchestration', 'send',
                    '--run', agent['run_id'], '--to', 'dispatch:' + agent['dispatch_id'], '--type', 'status',
                    '--subject', 'Continue after confirmed network failure', '--body',
                    'Continue the current task after the recorded network failure. Reconcile unfinished operations; '
                    'do not replay unknown effects or restart the item.', mailbox=agent.get('mailbox'))
            op = read_json(runner.launcher.journal(attempt))['network-' + identifier]
            check_receipt(receipt, agent, op['request_id'])
            session['receipt'] = receipt  # Queueing, starting and processing are separate facts.
        elif data['action'] == 'replace':
            replacement = replace_executor(runner, state, attempt, reg, agent, observed,
                                           session, journal, journal_path, allowed)
            with runner.launch_permission():
                if not allowed():
                    raise ValueError('network handoff cancelled')
                session['receipt'] = runner.launcher.effect(attempt, 'network-handoff-' + identifier,
                    'orchestration', 'send', '--run', replacement['run_id'], '--to', 'dispatch:' + replacement['dispatch_id'],
                    '--type', 'status', '--subject', 'Network replacement handoff', '--body', json.dumps(session['handoff']),
                    mailbox=replacement.get('mailbox'))
                op = read_json(runner.launcher.journal(attempt))['network-handoff-' + identifier]
                check_receipt(session['receipt'], replacement, op['request_id'])
        else:
            raise ValueError('invalid network recovery action')
        atomic_json(journal_path, journal)


def replace_executor(runner, state, attempt, reg, agent, observed, session, journal, journal_path, allowed,
                     reconciled_control=None):
    if session['replacements'] >= 1 or not allowed():
        raise ValueError('network replacement exhausted or cancelled')
    # Stop exact executor first; no new session until stopped + exited are proved.
    closed = read_json(runner.launcher.journal(attempt), {}).get(agent['id'] + '-close', {})
    if closed.get('status') == 'completed':
        if (closed.get('args') != ['terminal', 'close', '--terminal', agent.get('terminal')]
                or (closed.get('evidence') or {}).get('incarnation') != agent.get('incarnation')):
            raise ValueError('recorded terminal close identity mismatch')
        operations.terminal_evidence(closed, runner.launcher.orca)
    elif observed.get('terminalResource', {}).get('ownershipState') == 'external':
        runner.launcher.verify_terminal(agent, observed)
        runner.launcher.lifecycle(agent, 'worker-abandon')
        abandon = read_json(runner.launcher.journal(attempt), {}).get(agent['id'] + '-worker-abandon', {})
        if abandon.get('status') != 'completed':
            raise ValueError('network replacement blocked: assignment stop unconfirmed')
        if not runner.launcher.close_registered_terminal(agent, observed):
            raise ValueError('network replacement blocked: external terminal close unconfirmed')
    else:
        runner.launcher.verify_terminal(agent, observed)
        runner.launcher.lifecycle(agent, 'worker-stop')
        stopped = runner.launcher.observe_agent(agent)
        if (stopped.get('dispatch', {}).get('status') != 'stopped'
                or runner.launcher.liveness(agent['dispatch_id'], agent.get('mailbox')) != 'exited'):
            raise ValueError('network replacement blocked: previous executor stop unconfirmed')
        runner.launcher.verify_terminal(agent, stopped)
    runner.launcher.check_operations(attempt)
    with runner.launch_permission():
        if not allowed():
            raise ValueError('network replacement cancelled before dispatch')
        replacement = {k: copy.deepcopy(v) for k, v in agent.items() if k in (
            'role', 'node_id', 'parent_agent', 'call_id', 'task_id', 'run_id', 'mailbox', 'requested',
            'variant', 'reason', 'function', 'connection_stamp', 'attempt')}
        replacement.update(id=str(uuid.uuid4()), status='ready', reuse=None, keep_session=False,
                           network_root=session['root'], network_replacement_of=agent['id'])
        agent.update(status='superseded', superseded_by=replacement['id'])
        reg['agents'].append(replacement)
        if agent.get('call_id'):
            call = next(c for c in reg['graph_calls'] if c['id'] == agent['call_id'])
            call.setdefault('dispatch_history', []).append(agent['dispatch_id'])
            call['child_agent'] = replacement['id']
        session['replacements'] += 1
        session['replacement'] = replacement['id']
        session['handoff'] = handoff(runner, state, reg)
        runner.launcher.save_registry(attempt, reg)
        atomic_json(journal_path, journal)
        runner.launcher._launch(attempt, reg, replacement, agent.get('launch_title', agent['role']),
                                runner.state_dir, retry_of=agent['dispatch_id'], permitted=allowed,
                                reconciled_control=reconciled_control)
        session['dispatch'] = replacement['dispatch_id']
        if agent.get('role') == 'coordinator':
            state['current'].update(dispatch_id=replacement['dispatch_id'], terminal=replacement['terminal'])
            runner.save_state(state)
    return replacement


def handoff(runner, state, registry):
    cur = state['current']
    from .verification import view
    return {'item': cur['item'], 'task': cur.get('task_id'), 'attempt': cur['attempt_dir'],
            'changed_files': runner.dirty_paths(), 'checks': view(cur['attempt_dir'], [])['records'],
            'remaining_errors': cur.get('problems', []),
            'reports': [str(p) for p in (Path(cur['attempt_dir']) / 'reports').glob('*-summary.json')],
            'unfinished_operations': [k for k, op in read_json(runner.launcher.journal(cur['attempt_dir']), {}).items()
                                      if op.get('status') not in ('completed', 'rejected')]}


def cancel(runner, reason):
    path = runner.state_dir / 'network-recovery.json'
    if not path.exists():
        return
    with lock(runner.state_dir / 'network-recovery.lock'):
        journal = document(runner)
        for session in journal['sessions'].values():
            if session.get('status') in ('waiting', 'queued', 'needs_action', 'observing'):
                session.update(status='cancelled', next_check_at=None, reason=reason)
        atomic_json(path, journal)


def view(runner):
    state = runner.load_state()
    cur = state.get('current') or {}
    journal = document(runner)
    from .goal_intent import read
    intent = read(runner)
    expected = {'identity': runner.context.identity(), 'generation': intent.get('generation'),
                'epoch': intent.get('epoch'), 'attempt': cur.get('attempt_dir')}
    sessions = [s for s in journal.get('sessions', {}).values() if s.get('scope') == expected]
    return {'sessions': sessions, 'next_operation': next(({'action': s.get('action'), 'at': s.get('next_check_at')}
            for s in sessions if s.get('status') in ('waiting', 'queued')), None)}


def reconcile(runner, state, data, *, observe_only=False):
    """Reconcile existing effects, then deliver a not-yet-submitted replacement handoff."""
    with lock(runner.state_dir / 'network-recovery.lock'):
        command_path = runner.state_dir / 'commands' / (data['request'] + '.json')
        command = read_json(command_path, {})
        uncertain_id = state.get('control_uncertain')
        uncertain = read_json(runner.state_dir / 'commands' / (str(uncertain_id) + '.json'), {})
        related = (uncertain_id == data['request'] or uncertain.get('action') == 'network-reconcile'
                   and uncertain.get('data', {}).get('request') == data['request'])
        if command.get('status') != 'unknown' or not related:
            raise ValueError('network reconciliation requires the exact interrupted command')
        journal_path = runner.state_dir / 'network-recovery.json'
        journal = document(runner)
        session = journal['sessions'][data['session']]
        attempt = data['_target']
        reserved = command.get('data', {})
        if (reserved.get('_target') != attempt or reserved.get('session') != data['session']
                or reserved.get('scope') != session.get('scope')
                or reserved.get('scope', {}).get('identity') != runner.context.identity()):
            raise ValueError('network reconciliation scope/reservation mismatch')
        from .recovery import _unknown
        def allowed():
            fresh = runner.load_state()
            if fresh.pop('control_uncertain', None) != uncertain_id:
                return False
            return permitted(runner, fresh, reserved['scope']) and not _unknown(runner, fresh)
        issues = runner.launcher.reconcile_attempt(attempt, accepted_reports=runner.accepted_child_reports(attempt))
        if issues:
            raise ValueError('; '.join(issues))
        with lock(Path(attempt) / 'agents.lock'):
            ops = read_json(runner.launcher.journal(attempt), {})
            reg = runner.launcher.registry(attempt)
            action = command['data']['action']
            if action == 'resume':
                effect = ops.get('network-' + data['request'], {})
                agent = next((a for a in reg['agents'] if a.get('dispatch_id') == command['data']['dispatch']), {})
            else:
                previous = next((a for a in reg['agents'] if a['id'] == reserved.get('agent')
                                 and a.get('dispatch_id') == reserved.get('dispatch')), {})
                candidates = [a for a in reg['agents'] if previous and a.get('network_replacement_of') == previous['id']]
                if candidates:
                    if (len(candidates) != 1 or previous.get('superseded_by') != candidates[0]['id']
                            or session.get('replacement') not in (None, candidates[0]['id'])
                            or session.get('replacements') not in (0, 1)
                            or any(candidates[0].get(k) != previous.get(k) for k in (
                                'role', 'node_id', 'parent_agent', 'call_id', 'task_id', 'run_id',
                                'mailbox', 'requested', 'connection_stamp', 'attempt'))):
                        raise ValueError('network replacement reservation identity mismatch')
                    if not session.get('replacement'):
                        if observe_only:
                            raise ValueError('replacement outcome lacks conclusive registry evidence')
                        session.update(replacement=candidates[0]['id'], replacements=1,
                                       handoff=handoff(runner, state, reg))
                        atomic_json(journal_path, journal)
                agent = next((a for a in reg['agents'] if a['id'] == session.get('replacement')), {})
                if not agent and not observe_only and not session.get('replacement'):
                    # Older owners attempted worker-stop on runner-owned external
                    # terminals. A completed stop_unknown receipt proves no process
                    # action; finish cleanup through the exact registered terminal.
                    stop = ops.get(previous.get('id', '') + '-worker-stop', {})
                    result = stop.get('result') or {}
                    abandon = ops.get(previous.get('id', '') + '-worker-abandon', {})
                    closed = ops.get(previous.get('id', '') + '-close', {})
                    stopped_without_action = (stop.get('status') == 'completed'
                        and stop.get('args') == ['orchestration', 'worker-stop', '--dispatch', reserved.get('dispatch')]
                        and result.get('dispatchId') == reserved.get('dispatch')
                        and result.get('state') == 'stop_unknown' and result.get('processAction') == 'none')
                    fenced_assignment = (abandon.get('status') == 'completed'
                        and abandon.get('args') == ['orchestration', 'worker-abandon', '--dispatch', reserved.get('dispatch')])
                    if (previous.get('status') not in ('active', 'waiting_child', 'cleanup_pending', 'released', 'stopped') or reg.get('fenced')
                            or not (stopped_without_action or fenced_assignment)
                            or any(c.get('parent_agent') == previous.get('id') and c.get('status') not in ('completed', 'stopped')
                                   for c in reg.get('graph_calls', []))):
                        raise ValueError('interrupted external stop lacks conclusive no-process-action evidence')
                    observed = runner.launcher.observe_agent(previous)
                    if closed.get('status') != 'completed' and observed.get('terminalResource', {}).get('ownershipState') != 'external':
                        raise ValueError('interrupted stop is not an exact external terminal')
                    agent = replace_executor(runner, state, Path(attempt), reg, previous, observed,
                                             session, journal, journal_path, allowed, reconciled_control=uncertain_id)
                    candidates = [agent]
                    ops = read_json(runner.launcher.journal(attempt), {})
                if agent and agent not in candidates:
                    raise ValueError('network replacement reservation identity mismatch')
                if agent and not agent.get('dispatch_id'):
                    if (observe_only or agent.get('status') != 'ready' or reg.get('fenced')
                            or agent not in candidates or not allowed()):
                        raise ValueError('reserved network replacement continuation cancelled or unconfirmed')
                    runner.launcher.check_operations(attempt)
                    with runner.launch_permission():
                        runner.launcher._launch(Path(attempt), reg, agent, previous.get('launch_title', previous['role']),
                            runner.state_dir, retry_of=previous['dispatch_id'], permitted=allowed,
                            reconciled_control=uncertain_id)
                    ops = read_json(runner.launcher.journal(attempt), {})
                if not agent.get('dispatch_id'):
                    raise ValueError('replacement outcome lacks conclusive registry evidence')
                if agent.get('role') == 'coordinator' and agent.get('status') == 'active':
                    state['current'].update(dispatch_id=agent['dispatch_id'], terminal=agent['terminal'])
                    runner.save_state(state)
                key = 'network-handoff-' + data['request']
                effect = ops.get(key, {})
                if not effect:
                    if observe_only or agent.get('status') != 'active' or reg.get('fenced'):
                        raise ValueError('network handoff was not submitted; observation cannot send input')
                    if not allowed():
                        raise ValueError('reconciled launch; handoff cancelled or another effect unknown')
                    with runner.launch_permission():
                        if not allowed():
                            raise ValueError('network handoff cancelled before input')
                        runner.launcher.effect(attempt, key, 'orchestration', 'send', '--run', agent['run_id'],
                            '--to', 'dispatch:' + agent['dispatch_id'], '--type', 'status',
                            '--subject', 'Network replacement handoff', '--body', json.dumps(session['handoff']),
                            mailbox=agent.get('mailbox'))
                    effect = read_json(runner.launcher.journal(attempt))[key]
                session['dispatch'] = agent['dispatch_id']
            if effect.get('status') != 'completed':
                raise ValueError('network operation receipt absent; replay forbidden')
            check_receipt(effect['result'], agent, effect['request_id'])
            observed = {k: v for k, v in runner.load_state().items() if k != 'control_uncertain'}
            session.update(status='observing' if permitted(runner, observed, reserved['scope']) else 'cancelled',
                           receipt=effect['result'], next_check_at=None)
            session.pop('command_id', None)
            session.pop('command_data', None)
            atomic_json(journal_path, journal)
            state.pop('control_uncertain', None)
            state.setdefault('applied_commands', []).append(data['request'])
            if uncertain_id != data['request']:
                state['applied_commands'].append(uncertain_id)
            runner.save_state(state)
            command.update(status='done', result={'state': 'ok', 'reconciled': True})
            command.pop('error', None)
            atomic_json(command_path, command)
