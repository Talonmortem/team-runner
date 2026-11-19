"""Durable, sequential call chains over an immutable team graph.

Only Runner consumes mailboxes. A blocked CLI observes its persisted call result.
"""
import copy
from contextlib import nullcontext
import json
import os
import subprocess
from pathlib import Path
import time
import uuid

from . import memory_admission, runner_operations as operations
from .protocol import request_id
from .runner_store import lock, read_json
from .team_graph import node

TERMINAL = ('completed', 'stopped')


class EdgeLimit(ValueError):
    pass


def snapshot(launcher, attempt):
    launcher.context.check_attempt(attempt)
    cfg = read_json(Path(attempt) / 'config.json', {})
    if cfg.get('version') != 3:
        raise ValueError('delegate requires a version=3 attempt')
    return cfg


def current(launcher, attempt, reconciled_control=None):
    state = read_json(launcher.context.state_dir / 'state.json', {})
    if (state.get('current') or {}).get('attempt_dir') != str(attempt):
        raise ValueError('stale attempt')
    if (state.get('stop_requested') or state.get('control_uncertain')
            and state['control_uncertain'] != reconciled_control):
        raise RuntimeError('attempt stopped or control unconfirmed')
    intent = read_json(launcher.context.state_dir / 'goal-intent.json', {})
    if intent.get('desired_state') == 'stopped':
        raise RuntimeError('execution stopped')
    return state


def execution_allowed(launcher, attempt, expected, reconciled_control=None):
    try:
        if current(launcher, attempt, reconciled_control).get('paused'):
            return False
        intent = read_json(launcher.context.state_dir / 'goal-intent.json', {})
        actual = {k: intent.get(k) for k in ('generation', 'epoch')}
        desired = (intent.get('desired_state') if intent else
                   read_json(launcher.context.state_dir / 'recovery-settings.json', {}).get('intent', 'running'))
        return actual == expected and desired == 'running'
    except (ValueError, RuntimeError):
        return False


def caller(launcher, attempt, reg):
    current(launcher, attempt)
    handle = os.environ.get('ORCA_TERMINAL_HANDLE')
    agents = [a for a in reg['agents'] if handle and a.get('terminal') == handle
              and not a.get('superseded_by') and a.get('status') in ('active', 'waiting_child')]
    if len(agents) != 1:
        raise ValueError('caller is not an active registered terminal/Dispatch')
    agent = agents[0]
    data = launcher.observe_agent(agent)
    launcher.verify_terminal(agent, data)
    if (data.get('dispatch', {}).get('status') != 'dispatched'
            or data.get('worker', {}).get('state') in ('outcome_unknown', 'failed', 'starting')):
        raise RuntimeError('caller Dispatch is not executing; reconcile first')
    return agent


def ancestors(reg, parent):
    result = []
    while parent:
        if parent['id'] in {a['id'] for a in result}:
            raise RuntimeError('corrupt call chain')
        result.append(parent)
        parent = next((a for a in reg['agents'] if a['id'] == parent.get('parent_agent')), None)
    return result


def guard(launcher, attempt, reg, record):
    """Also used immediately before Dispatch and by launch reconciliation."""
    if record.get('role') == 'coordinator':
        if any(a.get('status') in ('active', 'waiting_child', 'unknown', 'launching', 'ready')
               and a['id'] != record['id'] and a.get('role') != 'coordinator' for a in reg['agents']):
            raise RuntimeError('coordinator has an unresolved child')
        return
    call = next((c for c in reg.get('graph_calls', []) if c['id'] == record.get('call_id')), None)
    if not call or call.get('child_agent') != record['id']:
        raise ValueError('graph launch requires a registered edge call')
    parent = next(a for a in reg['agents'] if a['id'] == call['parent_agent'])
    chain = ancestors(reg, parent)
    if any(a.get('status') != 'waiting_child' for a in chain):
        raise RuntimeError('call ancestors must be waiting')
    allowed = {a['id'] for a in chain} | {record['id']}
    if any(a['id'] not in allowed and a.get('status') not in ('released', 'stopped', 'superseded')
           for a in reg['agents']):
        raise RuntimeError('another agent or unknown outcome blocks delegation')


def pass_available(reg, edge, call_id):
    reserved = reg.get('granted_delegations', {})
    extra = sum(g['edge_id'] == edge['id'] and (g['call_id'] == call_id or g['call_id'] not in reserved)
                for g in reg.get('graph_grants', {}).values())
    return reg.get('edge_passes', {}).get(edge['id'], 0) < edge['max_passes'] + extra


def take_pass(reg, cfg, call):
    if call.get('pass_taken'):
        return True
    edge = next(e for e in cfg['edges'] if e['id'] == call['edge_id'])
    if not pass_available(reg, edge, call['id']):
        call['status'] = 'limit'
        return False
    used = reg.setdefault('edge_passes', {})
    used[edge['id']] = used.get(edge['id'], 0) + 1
    call.update(pass_taken=True, status='pending')
    return True


def delegate(launcher, attempt, edge_id, spec, title, state_dir, identifier,
             variant='primary', reason=None):
    attempt = Path(attempt)
    cfg = snapshot(launcher, attempt)
    request_id(identifier)
    launcher.validate_variant(variant, reason, None)
    with lock(attempt / 'agents.lock'):
        reg = launcher.registry(attempt)
        call, edge, target, settings = _delegation(launcher, attempt, reg, cfg, edge_id, spec, title,
                                                     identifier, variant, reason)
        if call['status'] in (*TERMINAL, 'running'):
            return copy.deepcopy(call)
        if not call.get('pass_taken') and not pass_available(reg, edge, identifier):
            _reserve_call(reg, call)
            call['status'] = 'limit'
            launcher.save_registry(attempt, reg)
            return copy.deepcopy(call)
        continuation = next((a.get('terminal') for a in reg['agents']
                             if a['id'] == call.get('child_agent') and a['status'] == 'ready'), None)
        authorization = call['authorization']
    permitted = lambda: execution_allowed(launcher, attempt, authorization)
    admission = (nullcontext() if continuation else memory_admission.queue.hold(
        launcher.context.state_dir, attempt, 'delegate:' + identifier, permitted=permitted, block=True))
    with admission:
        with lock(attempt / 'agents.lock'):
            reg = launcher.registry(attempt)
            call, edge, target, settings = _delegation(launcher, attempt, reg, cfg, edge_id, spec, title,
                                                         identifier, variant, reason)
            if not permitted() or call['authorization'] != authorization:
                raise memory_admission.AdmissionCancelled('goal authorization changed before delegation')
            if call['status'] in (*TERMINAL, 'running'):
                return copy.deepcopy(call)
            _reserve_call(reg, call)
            taken = take_pass(reg, cfg, call)
            launcher.save_registry(attempt, reg)
            if not taken:
                return copy.deepcopy(call)
        return _launch_child(launcher, attempt, call, target, edge, settings, state_dir, permitted, continuation)


def delegation_request(reg, identifier):
    return next((c for c in reg.get('graph_calls', []) if c['id'] == identifier),
                reg.get('granted_delegations', {}).get(identifier))


def _delegation(launcher, attempt, reg, cfg, edge_id, spec, title, identifier, variant, reason):
    parent = caller(launcher, attempt, reg)
    edge = next((e for e in cfg['edges'] if e['id'] == edge_id and e['type'] == 'delegation'), None)
    if not edge or edge['source'] != parent.get('node_id'):
        raise ValueError('edge is not permitted for caller')
    target = node(cfg, edge['target'])
    chain = {a['id']: a.get('node_id') for a in ancestors(reg, parent)}
    if target['id'] in chain.values():
        raise ValueError('cannot delegate to a waiting ancestor; use a rework edge')
    settings = target['reserve' if variant == 'reserve' else 'primary']
    if settings is None:
        raise ValueError('reserve absent from snapshot')
    launcher.preflight(settings)
    params = dict(parent_agent=parent['id'], edge_id=edge_id, spec=spec, title=title,
                  variant=variant, reason=reason)
    call = delegation_request(reg, identifier)
    if call and call['params'] != params:
        raise ValueError('request_id already used for another delegation')
    if call and call['status'] in (*TERMINAL, 'running'):
        return call, edge, target, settings
    if reg.get('fenced') or reg.get('stop_requested'):
        raise RuntimeError('attempt fenced; reconcile first')
    launcher.check_operations(attempt)
    if parent['status'] != 'active' and parent.get('waiting_call') != identifier:
        raise RuntimeError('parent already has an unresolved child')
    if parent['status'] == 'active' and any(a['id'] not in chain
            and a.get('status') not in ('released', 'stopped', 'superseded') for a in reg['agents']):
        raise RuntimeError('another agent blocks delegation')
    if call is None:
        intent = read_json(launcher.context.state_dir / 'goal-intent.json', {})
        call = dict(id=identifier, params=params, parent_agent=parent['id'], edge_id=edge_id,
                    target=target['id'], kind='delegation', status='pending',
                    authorization={k: intent.get(k) for k in ('generation', 'epoch')})
    if not execution_allowed(launcher, attempt, call['authorization']):
        raise memory_admission.AdmissionCancelled('goal authorization changed before delegation')
    return call, edge, target, settings


def _reserve_call(reg, call):
    calls = reg.setdefault('graph_calls', [])
    if call not in calls:
        calls.append(call)
    reg.get('granted_delegations', {}).pop(call['id'], None)
    parent = next(a for a in reg['agents'] if a['id'] == call['parent_agent'])
    parent.update(status='waiting_child', waiting_call=call['id'])


def grant_pass(reg, cfg, call):
    if call.get('kind') == 'rework':
        take_pass(reg, cfg, call)
        return
    # Retain the granted request ID and authorization without an open child.
    # The CLI can die while queued, so only admission reserves the parent.
    reg['graph_calls'].remove(call)
    call['status'] = 'pending'
    reg.setdefault('granted_delegations', {})[call['id']] = call
    parent = next(a for a in reg['agents'] if a['id'] == call['parent_agent'])
    if parent.get('waiting_call') == call['id']:
        parent['status'] = 'active'
        parent.pop('waiting_call', None)


def _launch_child(launcher, attempt, call, target, edge, settings, state_dir, permitted, continuation):
    identifier = call['id']
    spec, title, variant, reason = (call['params'][k] for k in ('spec', 'title', 'variant', 'reason'))
    # Existing journal keys identify every effect. Never create a second task/session
    # after loss of a receipt; check_operations above requires reconciliation first.
    if not permitted():
        raise RuntimeError('goal authorization changed before child run')
    run = launcher.child_run(attempt, permitted=permitted)
    child_spec = ('Agent: ' + target['name'] + '; role: ' + target['role'] + '; node: ' + target['id'] +
                  '\nAuthoritative launch settings: ' + json.dumps(settings) +
                  '\nInstructions:\n' + target['instructions'] + '\nHandoff:\n' + edge['instructions'] +
                  '\nDelegate only via team-runner delegate EDGE_ID --spec FILE --title TITLE --request-id UUID. '
                  'Read allowed edges in config.json. Runtime subagents are disabled. '
                  'Calls block until a result; do not finish with an open child. '
                  'For rework use team_runner.worker_report with outcome=changes_requested and return_edge=EDGE_ID. '
                  'Return edges must address your immediate caller. '
                  'Use team_runner.worker_report for completion with the injected task/dispatch/terminal IDs. '
                  'Only runner reads mailboxes; use team-runner messages for routed messages.\n\n' + spec)
    with lock(attempt / 'agents.lock'):
        reg = launcher.registry(attempt)
        call = next(c for c in reg['graph_calls'] if c['id'] == identifier)
        if reg.get('fenced') or reg.get('stop_requested'):
            raise RuntimeError('attempt fenced')
        launcher.check_operations(attempt)
        record = next((a for a in reg['agents'] if a['id'] == call.get('child_agent')), None)
        if continuation and (not record or record.get('terminal') != continuation
                             or record.get('status') not in ('ready', 'active')):
            raise RuntimeError('delegation continuation terminal changed')
        try:
            if not permitted():
                raise RuntimeError('goal authorization changed before task creation')
            task = launcher.effect(attempt, 'graph-task-' + identifier, 'orchestration', 'task-create',
                                   '--run', run['run_id'], '--task-title', title, '--spec', child_spec,
                                   mailbox=run['mailbox'])['task']
            if record is None:
                record = dict(id=str(uuid.uuid4()), node_id=target['id'], role=target['role'],
                              parent_agent=call['parent_agent'], call_id=identifier,
                              requested=settings, observed=None, variant=variant, reason=reason,
                              task_id=task['id'], run_id=run['run_id'], mailbox=run['mailbox'],
                              status='ready', attempt=str(attempt), reuse=None, keep_session=False,
                              connection_stamp=launcher.connection_stamp(settings))
                call['child_agent'] = record['id']
                reg['agents'].append(record)
            launcher.save_registry(attempt, reg)
            if record.get('dispatch_id'):
                if record['status'] not in ('active', 'released'):
                    raise RuntimeError('child outcome unconfirmed; reconcile first')
            else:
                launcher._launch(attempt, reg, record, title, state_dir, permitted=permitted)
            call.update(status='running', dispatch_id=record['dispatch_id'])
            launcher.save_registry(attempt, reg)
            return copy.deepcopy(call)
        except Exception:
            reg['fenced'] = True
            launcher.save_registry(attempt, reg)
            raise


def wait(launcher, attempt, identifier, sleep=time.sleep):
    """Read persisted results. Never poll/ack an Orca mailbox or replay effects."""
    while True:
        current(launcher, attempt)
        # Settlement holds this lock while closing the child. Its in-flight
        # operation is not an unknown outcome for the waiting parent.
        with lock(Path(attempt) / 'agents.lock'):
            reg = launcher.registry(attempt)
            call = next(c for c in reg.get('graph_calls', []) if c['id'] == identifier)
            if call['status'] in TERMINAL:
                return copy.deepcopy(call)
            if call['status'] == 'limit':
                return copy.deepcopy(call)
            if reg.get('stop_requested') or reg.get('fenced'):
                raise RuntimeError('delegation blocked; reconcile or inspect stop')
            launcher.check_operations(attempt)
        sleep(.2)


def grant(launcher, attempt, call_id, identifier):
    request_id(identifier)
    cfg = snapshot(launcher, attempt)
    current(launcher, attempt)
    with lock(Path(attempt) / 'agents.lock'):
        reg = launcher.registry(attempt)
        grants = reg.setdefault('graph_grants', {})
        if identifier in grants:
            if grants[identifier]['call_id'] != call_id:
                raise ValueError('grant request conflict')
            return grants[identifier]
        call = next((c for c in reg.get('graph_calls', []) if c['id'] == call_id), None)
        if not call or call['status'] != 'limit' or reg.get('stop_requested') or reg.get('fenced'):
            raise ValueError('no exhausted edge awaiting permission')
        grants[identifier] = dict(call_id=call_id, edge_id=call['edge_id'], at=time.time())
        grant_pass(reg, cfg, call)
        launcher.save_registry(attempt, reg)
        return copy.deepcopy(grants[identifier])


def return_edge(launcher, attempt, reg, agent, payload):
    """Validate and durably charge a rework result before worker_done is sent."""
    cfg = snapshot(launcher, attempt)
    if any(c.get('kind') == 'delegation' and c['parent_agent'] == agent['id'] and c['status'] not in TERMINAL for c in reg.get('graph_calls', [])):
        raise ValueError('cannot complete with an unresolved child')
    if payload.get('outcome') == 'succeeded' and rework_problems(reg, agent['id']):
        raise ValueError('rework requires a new successful child check')
    if payload.get('outcome') != 'changes_requested':
        if 'return_edge' in payload:
            raise ValueError('return_edge requires changes_requested')
        return None
    parent = next((a for a in reg['agents'] if a['id'] == agent.get('parent_agent')), None)
    edge = next((e for e in cfg['edges'] if e['id'] == payload.get('return_edge')), None)
    if (not parent or not edge or edge['type'] != 'rework'
            or edge['source'] != agent['node_id'] or edge['target'] != parent['node_id']):
        raise ValueError('rework must address the immediate caller via a snapshot edge')
    ident = 'return-' + agent['id']
    call = next((c for c in reg.get('graph_calls', []) if c['id'] == ident), None)
    if call is None:
        call = dict(id=ident, kind='rework', parent_agent=agent['id'], edge_id=edge['id'], status='pending', params=copy.deepcopy(payload))
        reg.setdefault('graph_calls', []).append(call)
    elif call.get('params') != payload or call['edge_id'] != edge['id']:
        raise ValueError('rework completion input changed; inspect existing intent')
    if not take_pass(reg, cfg, call):
        launcher.save_registry(attempt, reg)
        raise EdgeLimit('rework edge limit exhausted; waiting for a specific grant')
    call.update(status='completed', result=copy.deepcopy(payload))
    launcher.save_registry(attempt, reg)
    return edge


def rework_problems(reg, parent_id=None):
    calls = reg.get('graph_calls', [])
    problems = []
    for i, call in enumerate(calls):
        if (call.get('kind') != 'delegation' or call.get('result', {}).get('outcome') != 'changes_requested'
                or parent_id is not None and call['parent_agent'] != parent_id):
            continue
        if not any(c.get('kind') == 'delegation' and c['parent_agent'] == call['parent_agent']
                   and c['target'] == call['target'] and c['status'] == 'completed'
                   and c.get('result', {}).get('outcome') == 'succeeded' for c in calls[i + 1:]):
            problems.append('changes_requested requires a new successful check from ' + call['target'])
    return problems


def settle_reports(launcher, attempt, reg, coordinator, reports, conflicts, *, permitted=None, probe_unreported=False):
    if reg.get('stop_requested'):
        return []
    issues = []
    # Calls are appended after their ancestors. Close descendants before parents.
    for call in reversed(reg.get('graph_calls', [])):
        if call.get('kind') != 'delegation' or call['status'] in TERMINAL or not call.get('dispatch_id'):
            continue
        agent = next((a for a in reg['agents'] if a.get('id') == call.get('child_agent')), None)
        report = reports.get(call['dispatch_id'])
        if report and permitted is not None and not permitted():
            raise RuntimeError('cleanup cancelled by user intent')
        try:
            if not agent or call['dispatch_id'] in conflicts:
                raise RuntimeError('conflicting graph call or worker_done identity')
            if not report and call['status'] != 'settling':
                if probe_unreported and agent.get('status') == 'active':
                    observe_completion(launcher, agent, None)
                continue
            if not report:
                raise RuntimeError('settling graph call is missing its accepted worker_done')
            root = ancestors(reg, agent)[-1]
            if (not coordinator or root.get('role') != 'coordinator' or root.get('dispatch_id') != coordinator
                    or root.get('attempt') != str(attempt) or root.get('superseded_by')):
                raise RuntimeError('graph call does not belong to the current coordinator')
            if any(c['parent_agent'] == agent['id'] and c['status'] not in TERMINAL for c in reg['graph_calls']):
                continue
            settle(launcher, attempt, reg, agent, report)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            issues.append(f"{call['id']}: {exc}")
            if agent:
                agent.update(status='unknown', cleanup_error=str(exc))
    if issues:
        reg['fenced'] = True
        reg['recovery_issues'] = list(dict.fromkeys([*reg.get('recovery_issues', []), *issues]))
        launcher.save_registry(attempt, reg)
    return issues


def observe_completion(launcher, agent, report):
    data = launcher.observe_agent(agent)
    dispatch = data.get('dispatch') or {}
    status = dispatch.get('status')
    worker_state = (data.get('worker') or {}).get('state')
    known_failure = status == 'failed' and report and report.get('outcome') == 'failed'
    if (dispatch.get('id', agent['dispatch_id']) != agent['dispatch_id']
            or dispatch.get('runId', agent['run_id']) != agent['run_id']
            or worker_state == 'outcome_unknown'
            or status not in ('dispatched', 'completed') and not known_failure
            or worker_state == 'failed' and not known_failure
            or not report and (data.get('observation') or {}).get('status') == 'exited'):
        raise RuntimeError('child outcome unknown or worker exited without accepted worker_done')
    return status == 'completed' or known_failure


def settle(launcher, attempt, reg, agent, report):
    call = next((c for c in reg['graph_calls'] if c['id'] == agent.get('call_id')), None)
    if (not call or call['kind'] != 'delegation' or call.get('child_agent') != agent['id']
            or call.get('dispatch_id') != agent.get('dispatch_id') or agent.get('attempt') != str(attempt)
            or call.get('parent_agent') != agent.get('parent_agent')
            or agent.get('superseded_by') or agent.get('status') in ('stopped', 'superseded')
            or any(a is not agent and a.get('dispatch_id') == agent['dispatch_id'] for a in reg['agents'])
            or report.get('dispatchId') != agent['dispatch_id']
            or report.get('runId', agent['run_id']) != agent['run_id']
            or report.get('taskId', agent['task_id']) != agent['task_id']
            or report.get('outcome') not in ('succeeded', 'failed')):
        raise RuntimeError('accepted worker_done does not match registered graph call')
    parent = next((a for a in reg['agents'] if a['id'] == call['parent_agent']), None)
    if parent is None or parent.get('waiting_call') != call['id']:
        raise RuntimeError('parent is not waiting for this graph call')
    if report['outcome'] == 'succeeded' and rework_problems(reg, agent['id']):
        raise RuntimeError('parent completed before successful recheck')
    payload = read_json(Path(attempt) / 'reports' / (agent['id'] + '-summary.json'), {})
    if payload.get('outcome') == 'changes_requested':
        returned = next((c for c in reg['graph_calls'] if c['id'] == 'return-' + agent['id']), None)
        if (not returned or returned['status'] != 'completed' or returned.get('result') != payload
                or report['outcome'] != 'failed'):
            raise RuntimeError('unconfirmed rework return')
    elif payload and payload.get('outcome') != report['outcome']:
        raise RuntimeError('local summary conflicts with accepted worker_done')
    result = copy.deepcopy(report)
    if payload:
        if payload.get('dispatchId', report['dispatchId']) != report['dispatchId']:
            raise RuntimeError('local summary belongs to a different Dispatch')
        result.update(payload)
        if payload.get('return_edge'):
            result['handoff_instructions'] = next(e['instructions'] for e in snapshot(launcher, attempt)['edges'] if e['id'] == payload['return_edge'])

    journal = read_json(launcher.journal(attempt), {})
    close_key = agent['id'] + '-close'
    closed = journal.get(close_key)
    release = journal.get(agent['id'] + '-worker-release')
    if call['status'] == 'settling' and call.get('result') != result:
        raise RuntimeError('accepted worker_done conflicts with pending settlement')
    if call['status'] == 'settling' and closed:
        if (not (closed.get('evidence') or {}).get('launch')
                or closed.get('args') != ['terminal', 'close', '--terminal', agent.get('terminal')]
                or not isinstance(closed.get('result'), dict)):
            raise RuntimeError('terminal close outcome unconfirmed')
        if closed.get('stage') == 'close_verified':
            evidence = closed['evidence']
            expected = evidence.get('terminal') or {}
            launch = journal.get(evidence['launch'], {})
            registered = (launch.get('result') or {}).get('terminal') or {}
            confirmed = closed['result'].get('terminal') or {}
            if (launch.get('status') != 'completed' or any(not expected.get(k)
                    or expected[k] != registered.get(k) or expected[k] != confirmed.get(k)
                    for k in ('handle', 'incarnationId', 'ptyId', 'worktreeId'))):
                raise RuntimeError('terminal close receipt conflicts with registered launch')
        proof = operations.close_terminal(launcher.journal(attempt), close_key, launcher.orca,
                                          agent['terminal'], agent['incarnation'], launcher.context.orca_id,
                                          mailbox=agent.get('mailbox'))
        agent.update(terminal_closed=True, close_evidence=proof)
    elif call['status'] == 'settling' and release:
        if (release.get('status') != 'completed'
                or release.get('args') != ['orchestration', 'worker-release', '--dispatch', agent['dispatch_id']]
                or launcher.liveness(agent['dispatch_id'], agent.get('mailbox')) != 'exited'):
            raise RuntimeError('worker release outcome unconfirmed')
    else:
        if closed or release or agent.get('status') == 'released':
            raise RuntimeError('unexpected close or release evidence for graph call')
        launcher.check_operations(attempt)
        if not observe_completion(launcher, agent, report):
            return
        call.update(status='settling', result=result)
        launcher.save_registry(attempt, reg)
        launcher.release_agent(agent, attempt, report)
    agent['status'] = 'released'
    agent.pop('cleanup_error', None)
    agent.pop('error', None)
    call.update(status='completed', result=result)
    parent.update(status='active')
    parent.pop('waiting_call', None)
    launcher.save_registry(attempt, reg)


def messages(launcher, attempt, all_messages):
    snapshot(launcher, attempt)
    reg = launcher.registry(attempt)
    agent = caller(launcher, attempt, reg)
    children = {c.get('dispatch_id') for c in reg.get('graph_calls', []) if c['parent_agent'] == agent['id']}
    result = []
    for message in all_messages:
        payload = message.get('payload') or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        if payload.get('dispatchId') in children | {agent['dispatch_id']}:
            result.append(message)
    return result
