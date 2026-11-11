"""Durable effects, exact replay identities and conservative recovery."""
from contextvars import ContextVar
import os
import subprocess
from pathlib import Path
import uuid

from .runner_store import atomic_json, lock, read_json

inherited_lock = ContextVar('operation_lock', default=None)


class UncertainOperation(RuntimeError):
    pass


class RejectedOperation(RuntimeError):
    pass


def rejected_reply(op):
    receipt = op.get('failure_receipt') or {}
    return (op.get('args', [])[:2] == ['orchestration', 'reply']
            and receipt.get('ok') is False
            and (receipt.get('error') or {}).get('code') == 'dispatch_inactive')


def checkpoint(stage):
    """Opt-in process crash injection; never enabled by normal runner configuration."""
    if os.environ.get('TEAM_RUNNER_CRASH_AT') == stage:
        os._exit(86)


def _perform(path, key, op, call):
    # The CLI inherits this lock: even an orphaned original call blocks continuation.
    with lock(str(path) + '.' + op['request_id'] + '.lock', blocking=False) as fd:
        token = inherited_lock.set(fd)
        try:
            from .runner_entry import process_stamp
            op.update(stage='calling', caller_pid=os.getpid(), caller_stamp=process_stamp(os.getpid()))
            journal = read_json(path, {})
            journal[key] = op
            atomic_json(path, journal)
            checkpoint(op['args'][1] + ':before_effect')
            args = op['args']
            if args[0] == 'orchestration':
                args = [*args, '--retry-request', op['request_id']]
            result = call(*args, mailbox=op.get('mailbox'))
            checkpoint(op['args'][1] + ':after_effect')
            journal = read_json(path, {})
            op.update(status='completed', stage='receipt', result=result)
            journal[key] = op
            atomic_json(path, journal)
            checkpoint(op['args'][1] + ':after_receipt')
            return result
        except Exception as exc:
            journal = read_json(path, {})
            if hasattr(exc, 'receipt'):
                op['failure_receipt'] = exc.receipt
            op.pop('result', None)
            op.update(status='unknown', error=str(exc))
            if rejected_reply(op):
                op.update(status='rejected', stage='preflight_rejected')
            journal[key] = op
            atomic_json(path, journal)
            error_type = RejectedOperation if op['status'] == 'rejected' else UncertainOperation
            raise error_type(f"operation {op['request_id']}: {exc}") from exc
        finally:
            inherited_lock.reset(token)


def mutation(path, key, call, *args, mailbox=None, evidence=None):
    """Caller serializes the journal with runner.lock or agents.lock."""
    path = Path(path)
    journal = read_json(path, {})
    op = journal.get(key)
    if op:
        if op['args'] != list(args) or (mailbox is not None and op.get('mailbox') != mailbox):
            raise ValueError('operation identity cannot be reused for different arguments')
        if op['status'] == 'completed':
            return op['result']
        if op['status'] == 'rejected' or rejected_reply(op):
            raise RejectedOperation(op.get('error') or 'operation rejected; replay forbidden')
        raise UncertainOperation(f"operation {op['request_id']} unconfirmed; reconcile first")
    mailbox = mailbox or os.environ.get('ORCA_TERMINAL_HANDLE')
    op = dict(request_id=str(uuid.uuid4()), args=list(args), mailbox=mailbox,
              owner=mailbox or os.environ.get('ORCA_TERMINAL_HANDLE'), attempt=str(path.parent),
              status='pending', stage='intent', evidence=evidence)
    journal[key] = op
    atomic_json(path, journal)
    return _perform(path, key, op, call)


def receipt_result(receipt):
    # Orca versions may persist the full transport envelope or its result.
    if receipt.get('ok') is False:
        raise UncertainOperation('Orca recorded a failed mutation: ' + str(receipt.get('error')))
    return receipt.get('result', receipt)


def rejected_dispatch(op):
    # These preflight errors reject worker-start before Dispatch creation/delivery.
    return (op.get('args', [])[:2] == ['orchestration', 'worker-start'] and
            (op.get('failure_receipt') or {}).get('error', {}).get('code') in
            ('consumer_fenced', 'task_not_startable'))


def reconcile(path, call):
    """Read-only at Orca; absence never proves a mutation did not happen."""
    path = Path(path)
    journal = read_json(path, {})
    issues = []
    for key, op in journal.items():
        if op['status'] in ('completed', 'rejected'):
            continue
        if rejected_dispatch(op) or rejected_reply(op):
            op.update(status='rejected', stage='preflight_rejected')
            continue
        try:
            if op['args'][0] == 'terminal':
                result = terminal_evidence(op, call)
            else:
                receipt = call('orchestration', 'request-show', '--request', op['request_id'], mailbox=op.get('mailbox'))
                if receipt.get('state') != 'completed' or not isinstance(receipt.get('receipt'), dict):
                    raise UncertainOperation(f"request {op['request_id']} = {receipt.get('state', 'unknown')}")
                if receipt['receipt'].get('ok') is False:
                    op['failure_receipt'] = receipt['receipt']
                    if rejected_reply(op) or rejected_dispatch(op):
                        op.update(status='rejected', stage='preflight_rejected',
                                  error=str(receipt['receipt'].get('error')))
                        continue
                result = receipt_result(receipt['receipt'])
            op.update(status='completed', stage='reconciled', result=result)
            op.pop('error', None)
        except Exception as exc:
            op['error'] = str(exc)
            issues.append(f'{key}: {exc}')
    atomic_json(path, journal)
    return issues


def terminal_evidence(op, call):
    evidence = op.get('evidence') or {}
    if op['args'][1] == 'create':
        from .runner_entry import process_stamp
        marker = read_json(evidence.get('marker', '/nonexistent'), {})
        terminal = marker.get('terminal') or {}
        if marker.get('nonce') != evidence.get('nonce') or not terminal.get('incarnationId'):
            raise UncertainOperation('terminal marker missing or identity unknown')
        observed = call('terminal', 'show', '--terminal', terminal['handle'], mailbox=op.get('mailbox'))['terminal']
        if any(observed.get(k) != terminal.get(k) for k in ('handle', 'incarnationId', 'ptyId', 'worktreeId')):
            raise UncertainOperation('terminal identity mismatch')
        if not marker.get('process_stamp') or process_stamp(marker['pid']) != marker['process_stamp']:
            raise UncertainOperation('launch process identity no longer live')
        return {'terminal': terminal, 'marker': marker}
    if op['args'][1] == 'close' and evidence.get('incarnation'):
        terminals = call('terminal', 'list', mailbox=op.get('mailbox')).get('terminals')
        if isinstance(terminals, list) and not any(t.get('handle') == evidence['terminal'] for t in terminals):
            return {'closed': True, 'evidence': 'terminal list'}
    raise UncertainOperation('terminal effect lacks conclusive evidence')


def missing_terminal_evidence(op, call, worktree_id):
    """Prove disappearance of an exact local launch; never infer it from silence."""
    from .project_services import departed
    evidence = op.get('evidence') or {}
    terminal = (op.get('result') or {}).get('terminal') or {}
    if (op.get('status') != 'completed' or op.get('args', [])[:2] != ['terminal', 'create']
            or not evidence.get('marker') or not evidence.get('nonce')
            or terminal.get('worktreeId') != worktree_id
            or not all(terminal.get(k) for k in ('handle', 'incarnationId', 'ptyId'))):
        return None
    marker = read_json(evidence['marker'], {})
    if (marker.get('nonce') != evidence['nonce']
            or any((marker.get('terminal') or {}).get(k) != terminal[k]
                   for k in ('handle', 'incarnationId', 'ptyId', 'worktreeId'))
            or not departed(marker)):
        return None
    listing = call('terminal', 'list', '--worktree', 'id:' + worktree_id, mailbox=op.get('mailbox'))
    scope = listing.get('hostScope') or {}
    if (not isinstance(listing.get('terminals'), list) or listing.get('truncated') is not False
            or scope.get('hostIds') != ['local'] or scope.get('omittedHostIds') != []):
        return None
    # Orca may reuse a pane's ptyId after reboot with a new terminal incarnation.
    if any(t.get('handle') == terminal['handle']
           or t.get('incarnationId') == terminal['incarnationId']
           or (t.get('ptyId') == terminal['ptyId']
               and not all(t.get(k) for k in ('handle', 'incarnationId')))
           for t in listing['terminals']):
        return None
    return {'terminal': terminal, 'process': {k: marker.get(k) for k in
            ('pid', 'process_stamp', 'boot_id')}, 'reason': 'exact launch process departed; complete local terminal list absent'}


def closed_launch_evidence(op, call, worktree_id):
    """Prove an exact created terminal was closed, without requiring a live PID."""
    from .project_services import departed
    evidence = op.get('evidence') or {}
    terminal = (op.get('result') or {}).get('terminal') or {}
    if (op.get('status') != 'completed' or op.get('args', [])[:2] != ['terminal', 'create']
            or not evidence.get('marker') or not evidence.get('nonce')
            or terminal.get('worktreeId') != worktree_id
            or not all(terminal.get(k) for k in ('handle', 'incarnationId', 'ptyId'))):
        return None
    marker = read_json(evidence['marker'], {})
    if (marker.get('nonce') != evidence['nonce']
            or any((marker.get('terminal') or {}).get(k) != terminal[k]
                   for k in ('handle', 'incarnationId', 'ptyId', 'worktreeId'))
            or not departed(marker)):
        return None
    try:
        observed = call('terminal', 'show', '--terminal', terminal['handle'],
                        mailbox=op.get('mailbox')).get('terminal') or {}
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return missing_terminal_evidence(op, call, worktree_id)
    if observed and any(observed.get(k) != terminal[k] for k in ('handle', 'incarnationId', 'ptyId', 'worktreeId')):
        return None
    if (observed.get('orphaned') is not True or observed.get('connected') is not False
            or observed.get('writable') is not False
            or (observed.get('exitCause') or {}).get('kind') != 'operator_close'):
        return missing_terminal_evidence(op, call, worktree_id)
    return {'terminal': terminal, 'process': {k: marker.get(k) for k in
            ('pid', 'process_stamp', 'boot_id')},
            'reason': 'exact launch process departed; Orca operator_close'}


def continue_operation(path, request_id, call):
    path = Path(path)
    journal = read_json(path, {})
    found = next(((k, o) for k, o in journal.items() if o['request_id'] == request_id), None)
    if not found:
        raise ValueError('unknown operation')
    key, op = found
    if op['status'] == 'completed':
        return op['result']
    if op['status'] == 'rejected' or rejected_reply(op):
        raise RejectedOperation(op.get('error') or 'operation rejected; replay forbidden')
    if op['args'][0] != 'orchestration':
        raise UncertainOperation('terminal operations cannot be replayed; reconcile their evidence')
    with lock(str(path) + '.' + request_id + '.lock', blocking=False):
        receipt = call('orchestration', 'request-show', '--request', request_id, mailbox=op.get('mailbox'))
        if receipt.get('state') not in ('pending', 'completed'):
            raise UncertainOperation('absent/unknown request; replay forbidden')
    return _perform(path, key, op, call)
