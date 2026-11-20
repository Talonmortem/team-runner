"""Prepare a new, paused queue authorization after the previous queue settled."""
import copy
from contextlib import nullcontext
import os
from pathlib import Path
import uuid

from . import dependency_handoff, goal_intent
from .protocol import ContractError, digest, request_id, summary, version
from .runner_store import atomic_json, lock, read_json


def _superseded_launch(runner, intent, startup, path, record):
    """An older launch cannot acquire the checkout under a retired epoch."""
    from .goal_execute import _shared_path
    from .runner_terminal import has_observation

    identity = runner.context.identity()
    if (record.get('status') != 'unknown' or record.get('phase') != 'launch'
            or record.get('identity') != identity):
        return False
    generation, epoch = record.get('generation'), record.get('epoch')
    if path != _shared_path(runner, generation, epoch, record.get('service_epoch')):
        return False
    if generation == intent['generation']:
        predecessor = intent
    else:
        predecessor = read_json(runner.state_dir / 'goal-authorizations' / (generation + '.json'))
        if not predecessor or goal_intent.validate(runner, predecessor)['generation'] != generation:
            return False
    if epoch >= predecessor['epoch'] or record.get('options') != predecessor['options']:
        return False

    # Scope preparation holds the start and checkout locks. Require a newer
    # runner to have claimed its nonce and exited, with terminal cleanup proven.
    launch = read_json(runner.state_dir / 'launch.json', {})
    nonce = launch.get('nonce')
    authorization = launch.get('authorization') or {}
    launch_epoch = authorization.get('epoch')
    if (startup.get('status') != 'idle' or not startup.get('close_evidence')
            or not nonce or nonce == record.get('previous_nonce') or startup.get('nonce') != nonce
            or launch.get('identity') != identity
            or launch.get('options') != intent['options']
            or authorization.get('generation') != intent['generation']
            or type(launch_epoch) is not int or not 1 <= launch_epoch <= intent['epoch']
            or (generation == intent['generation'] and launch_epoch <= epoch)):
        return False
    request_id(nonce)
    terminal_key = 'runner-' + nonce
    operations = read_json(runner.launcher.journal(runner.state_dir / 'startup'), {})
    close = operations.get(terminal_key + '-close', {})
    closed = (close.get('status') == 'completed' and close.get('result') == startup['close_evidence']
              and (close.get('evidence') or {}).get('launch') == terminal_key)
    if not (runner.state_dir / ('launch-' + nonce + '.claim')).is_file():
        return False
    return closed or ((not close or close.get('status') in ('completed', 'rejected'))
                      and has_observation(runner, startup, launch, operations))


def _effects_settled(runner, intent, state):
    from .recovery import _unknown

    startup = read_json(runner.state_dir / 'startup.json', {})
    if startup.get('status') in ('starting', 'unknown') or _unknown(runner, state):
        raise ContractError('unknown_effect', 'Reconcile the previous launch and operations first')
    for path in (runner.state_dir / 'execute').glob('*.json'):
        record = read_json(path)
        if (record.get('status') not in ('succeeded', 'failed')
                and not _superseded_launch(runner, intent, startup, path, record)):
            raise ContractError('unknown_effect', 'Resolve the previous execute before replacing scope')


def _settled(runner, intent, state):
    from .tracking import for_goal

    if (state.get('current') or state.get('control_uncertain') or state.get('recovery_issues')
            or state.get('stop_status') == 'unknown' or state.get('last_error')
            or state.get('goal') != intent['goal']):
        raise ContractError('execution_unsettled', 'Finish or reconcile the previous execution first')
    _effects_settled(runner, intent, state)
    last = (state.get('history') or [{}])[-1]
    if last.get('attempt_dir'):
        registry = runner.launcher.registry(last['attempt_dir'])
        if any(agent.get('status') not in ('released', 'stopped', 'superseded')
               for agent in registry['agents']):
            raise ContractError('execution_unsettled', 'Close the previous attempt agents first')
    tracking = for_goal(runner, intent['goal'], state)
    items = intent['options'].get('items')
    selected = items.split(',') if items else [key for key, *_ in tracking.items()]
    if any(not tracking.item_done(key) for key in selected):
        raise ContractError('queue_incomplete', 'The previous selected queue is not accepted')
    if not items and tracking.blocked_report():
        raise ContractError('queue_incomplete', 'The previous queue is not accepted')
    if intent['policy']['final']:
        from .goal_completion import read
        if not read(runner).get('final_accepted'):
            raise ContractError('queue_incomplete', 'Accept the previous final review first')


def _pending(runner, actions, exclude=None):
    for path in (runner.state_dir / 'ui/commands').glob('*.json'):
        record = read_json(path, {})
        if path != exclude and record.get('action') in actions and record.get('status') == 'pending':
            yield record


def require_no_pending_handoff(runner, intent):
    # A crash before intent replacement must leave the stopped authorization
    # fenced until the same request finishes its local writes.
    if any(record['previous']['generation'] == intent.get('generation')
           for record in _pending(runner, ('prepare-dependency-handoff',))):
        raise ContractError('unknown_effect', 'Finish the pending dependency handoff with its original request ID')


def _check_scope(runner, command, *, owns_runner_lock=False):
    from .application import require_scope

    current = summary(runner, owns_runner_lock=owns_runner_lock)
    require_scope(current, command)
    if command['epoch'] != current['epoch']:
        raise ContractError('stale_scope', 'Authorization changed; refresh before preparing handoff')


def prepare_dependency_handoff(runner, predecessor, identifier=None, *,
                               generation, epoch, attempt, revision, dry_run=False):
    from .application import _check_identity

    _validate_scope(generation, epoch)
    if any(not isinstance(value, str) or not value for value in (predecessor, attempt, revision)):
        raise ContractError('invalid_payload', 'Predecessor, attempt and revision are required')
    identity = runner.context.identity()
    _check_identity(runner, identity)
    command = {'schema_version': 1, 'action': 'prepare-dependency-handoff', 'identity': identity,
               'request_id': identifier, 'generation': generation, 'epoch': epoch,
               'attempt': attempt, 'revision': revision, 'predecessor': predecessor}
    if dry_run:
        _check_scope(runner, command)
        plan = dependency_handoff.plan(runner, goal_intent.read(runner), runner.load_state(), predecessor)
        _check_scope(runner, command)
        return {'schema_version': 1, 'status': 'eligible', 'dry_run': True, **plan}
    return _prepare(runner, command)


def _validate_scope(generation, epoch):
    request_id(generation)
    if type(epoch) is not int or epoch < 1:
        raise ContractError('invalid_scope', 'A positive observed epoch is required')
    if os.environ.get('TEAM_RUNNER_ATTEMPT'):
        raise ContractError('child_lifecycle_forbidden', 'Child attempts cannot replace goal scope')


def prepare(runner, items, identifier, *, generation, epoch):
    """Archive the old intent and prepare its replacement; never launch or resume.

    A durable before/after journal repairs an interrupted local write only. A
    repeated successful request returns its receipt without undoing later work.
    """
    _validate_scope(generation, epoch)
    if (not isinstance(items, str) or not items
            or any(not part or part.strip() != part for part in items.split(','))):
        raise ContractError('invalid_payload', 'A nonempty comma-separated item selection is required')
    command = {'schema_version': 1, 'action': 'prepare-scope', 'identity': runner.context.identity(),
               'request_id': identifier, 'generation': generation, 'epoch': epoch, 'items': items}
    return _prepare(runner, command)


def _prepare(runner, command):
    from .application import _check_identity, start_options
    from .artifacts import Store
    from .tracking import for_goal

    identifier = request_id(command['request_id'])
    generation, epoch = command['generation'], command['epoch']
    action = command['action']
    handoff = action == 'prepare-dependency-handoff'
    _check_identity(runner, command['identity'])
    journal = runner.state_dir / 'ui/commands' / (identifier + '.json')
    # Match existing execute/start/runner order; nonblocking resource locks reject
    # active work instead of waiting until it silently becomes eligible.
    with lock(runner.state_dir / 'requests' / (identifier + '.lock')), \
            lock(runner.state_dir / 'ui-actions.lock'):
        _check_identity(runner, command['identity'])
        record = read_json(journal)
        if record is not None:
            if record.get('action') != action:
                raise ContractError('request_conflict', 'request_id already used for another command')
            version(record)
            if record.get('command') != command:
                raise ContractError('request_conflict', 'request_id already used for another scope')
            if record.get('status') == 'succeeded':
                return copy.deepcopy(record['result'])
            if record.get('status') != 'pending':
                raise ContractError('invalid_operation', 'Invalid scope preparation journal')
            prepared = goal_intent.validate(runner, record['prepared'])
            observed = goal_intent.read(runner)
            if (observed.get('generation') == prepared['generation']
                    and observed.get('originating_request_id') == identifier):
                # The intent already committed. A new runner may now own the
                # checkout, so repair just the receipt under the request lock.
                record['status'] = 'succeeded'
                atomic_json(journal, record)
                return copy.deepcopy(record['result'])
        else:
            for directory in ('commands', 'ui/commands'):
                if (runner.state_dir / directory / (identifier + '.json')).exists():
                    raise ContractError('request_conflict', 'request_id already used for another command')
        with lock(runner.state_dir / 'execute.lock', blocking=False), \
                lock(runner.state_dir / 'start.lock', blocking=False), \
                lock(runner.context.checkout_lock, blocking=False), \
                lock(runner.state_dir / 'runner.lock', blocking=False), \
                (lock(runner.state_dir / 'artifact-prepare.lock', blocking=False) if handoff else nullcontext()), \
                lock(runner.state_dir / 'commands.lock'), \
                lock(runner.state_dir / 'goal-intent.lock'):
            previous = goal_intent.read(runner)
            if record is not None and previous != record['previous']:
                raise ContractError('unknown_effect', 'Scope preparation outcome requires inspection')
            if (previous.get('generation') != generation or previous.get('epoch') != epoch):
                raise ContractError('stale_scope', 'Authorization changed; refresh before preparing scope')
            if identifier in previous['requests']:
                raise ContractError('request_conflict', 'request_id already used for authorization')
            state = runner.load_state()
            if next(_pending(runner, ('prepare-scope', 'prepare-dependency-handoff'), journal), None):
                raise ContractError('unknown_effect', 'Finish the previous scope preparation first')
            archive = runner.state_dir / 'goal-authorizations' / (generation + '.json')
            if archive.exists() and read_json(archive) != previous:
                raise ContractError('request_conflict', 'Archived authorization differs; inspect before retry')
            if handoff and record is None:
                _check_scope(runner, command, owns_runner_lock=True)
                plan = dependency_handoff.plan(runner, previous, state, command['predecessor'])
                _effects_settled(runner, previous, state)
                dependency_handoff.check_controls(runner, state)
                dependency_handoff.stopped(runner, previous, state)
            elif not handoff:
                _settled(runner, previous, state)
            if record is None:
                items = plan['items'] if handoff else command['items']
                options = start_options(runner, {**previous['options'], 'items': items}, state)
                tracking = for_goal(runner, previous['goal'], state)
                keys = [tracking.canonical(item) for item in items.split(',')]
                if len(keys) != len(set(keys)):
                    raise ContractError('invalid_payload', 'Duplicate selected items')
                prepared = copy.deepcopy(previous)
                prepared.update(generation=str(uuid.uuid4()), epoch=1, desired_state='paused',
                                options=options, initial_options=options,
                                originating_request_id=identifier, requests={})
                authorization = {'schema_version': 1, 'generation': prepared['generation'], 'epoch': 1,
                                 'goal': prepared['goal'], 'request_id': identifier, 'status': 'authorized'}
                prepared['requests'][identifier] = {'request': {'options': options, 'generation': None,
                                                             'epoch': None}, 'result': authorization}
                goal_intent.validate(runner, prepared)
                result = {'schema_version': 1, 'status': 'succeeded', 'request_id': identifier,
                          'generation': prepared['generation'], 'epoch': 1, 'desired_state': 'paused',
                          'options': options, 'archive': str(archive)}
                record = {'schema_version': 1, 'id': identifier, 'action': action,
                          'identity': runner.context.identity(), 'command': command, 'status': 'pending',
                          'previous': previous, 'prepared': prepared, 'result': result}
                if handoff:
                    after = dependency_handoff.defer(state, plan, identifier)
                    record.update(state_before=digest(state), state_after=digest(after))
                    result['deferred'] = plan
                    _check_scope(runner, command, owns_runner_lock=True)
                    # Pin before the commit journal so retention stays fenced
                    # even if the process dies before writing state or intent.
                    with lock(Store(runner.state_dir).root / 'store.lock', blocking=False):
                        pin = Path(plan['attempt_dir']) / '.keep'
                        if not pin.exists():
                            atomic_json(pin, {'dependency_handoff': identifier})
                atomic_json(journal, record)
            if handoff:
                state_digest = digest(state)
                if state_digest not in (record['state_before'], record['state_after']):
                    raise ContractError('unknown_effect', 'Attempt state changed during handoff; inspect before retry')
                plan = record['result']['deferred']
                attempt = Path(plan['attempt_dir'])
                if attempt.resolve() != attempt or not (attempt / '.keep').exists():
                    raise ContractError('invalid_attempt', 'Deferred attempt directory or its pin is missing')
            if not archive.exists():
                atomic_json(archive, previous)
            if handoff:
                if state_digest == record['state_before']:
                    after = dependency_handoff.defer(state, plan, identifier)
                    runner.save_state(after)
            atomic_json(runner.state_dir / 'goal-intent.json', record['prepared'])
            record['status'] = 'succeeded'
            atomic_json(journal, record)
            return copy.deepcopy(record['result'])
