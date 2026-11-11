"""Prepare a new, paused queue authorization after the previous queue settled."""
import copy
import os
import uuid

from . import goal_intent
from .protocol import ContractError, request_id, version
from .runner_store import atomic_json, lock, read_json


def _settled(runner, intent, state):
    from .recovery import _unknown
    from .tracking import Tracking

    if (state.get('current') or state.get('control_uncertain') or state.get('recovery_issues')
            or state.get('stop_status') == 'unknown' or state.get('last_error')
            or state.get('goal') != intent['goal']):
        raise ContractError('execution_unsettled', 'Finish or reconcile the previous execution first')
    startup = read_json(runner.state_dir / 'startup.json', {})
    if startup.get('status') in ('starting', 'unknown') or _unknown(runner, state):
        raise ContractError('unknown_effect', 'Reconcile the previous launch and operations first')
    for path in (runner.state_dir / 'execute').glob('*.json'):
        if read_json(path).get('status') not in ('succeeded', 'failed'):
            raise ContractError('unknown_effect', 'Resolve the previous execute before replacing scope')
    last = (state.get('history') or [{}])[-1]
    if last.get('attempt_dir'):
        registry = runner.launcher.registry(last['attempt_dir'])
        if any(agent.get('status') not in ('released', 'stopped', 'superseded')
               for agent in registry['agents']):
            raise ContractError('execution_unsettled', 'Close the previous attempt agents first')
    tracking = Tracking(runner.repo / intent['goal'])
    items = intent['options'].get('items')
    selected = items.split(',') if items else [key for key, *_ in tracking.items()]
    latest = {tracking.canonical(entry.get('item'), strict=False): entry
              for entry in state.get('history', [])}
    for identifier in selected:
        found = tracking.resolve(identifier)
        entry = latest.get(found[0], {}) if found else {}
        if (not found or not tracking.item_done(found[3])
                or entry.get('outcome') != 'succeeded' or entry.get('problems') != []):
            raise ContractError('queue_incomplete', 'The previous selected queue is not accepted')
    if intent['policy']['final']:
        from .goal_completion import read
        if not read(runner).get('final_accepted'):
            raise ContractError('queue_incomplete', 'Accept the previous final review first')


def prepare(runner, items, identifier, *, generation, epoch):
    """Archive the old intent and prepare its replacement; never launch or resume.

    A durable before/after journal repairs an interrupted local write only. A
    repeated successful request returns its receipt without undoing later work.
    """
    from .application import _check_identity, start_options

    request_id(identifier)
    request_id(generation)
    if type(epoch) is not int or epoch < 1:
        raise ContractError('invalid_scope', 'A positive observed epoch is required')
    if os.environ.get('TEAM_RUNNER_ATTEMPT'):
        raise ContractError('child_lifecycle_forbidden', 'Child attempts cannot replace goal scope')
    if (not isinstance(items, str) or not items
            or any(not part or part.strip() != part for part in items.split(','))):
        raise ContractError('invalid_payload', 'A nonempty comma-separated item selection is required')
    _check_identity(runner, runner.context.identity())
    command = {'schema_version': 1, 'action': 'prepare-scope', 'identity': runner.context.identity(),
               'request_id': identifier, 'generation': generation, 'epoch': epoch, 'items': items}
    journal = runner.state_dir / 'ui/commands' / (identifier + '.json')
    # Match existing execute/start/runner order; nonblocking resource locks reject
    # active work instead of waiting until it silently becomes eligible.
    with lock(runner.state_dir / 'ui-actions.lock'), \
            lock(runner.state_dir / 'requests' / (identifier + '.lock')):
        _check_identity(runner, runner.context.identity())
        record = read_json(journal)
        if record is not None:
            if record.get('action') != 'prepare-scope':
                raise ContractError('request_conflict', 'request_id already used for another command')
            version(record)
            if record.get('command') != command:
                raise ContractError('request_conflict', 'request_id already used for another scope')
            if record.get('status') == 'succeeded':
                return copy.deepcopy(record['result'])
            if record.get('status') != 'pending':
                raise ContractError('invalid_operation', 'Invalid scope preparation journal')
        else:
            for directory in ('commands', 'ui/commands'):
                if (runner.state_dir / directory / (identifier + '.json')).exists():
                    raise ContractError('request_conflict', 'request_id already used for another command')
        with lock(runner.state_dir / 'execute.lock', blocking=False), \
                lock(runner.state_dir / 'start.lock', blocking=False), \
                lock(runner.context.checkout_lock, blocking=False), \
                lock(runner.state_dir / 'runner.lock', blocking=False), \
                lock(runner.state_dir / 'goal-intent.lock'):
            previous = goal_intent.read(runner)
            if record is not None:
                prepared = goal_intent.validate(runner, record['prepared'])
                if (previous.get('generation') == prepared['generation']
                        and previous.get('originating_request_id') == identifier):
                    # Intent replacement committed before a crash; do not pause a
                    # generation that the user may already have explicitly resumed.
                    record['status'] = 'succeeded'
                    atomic_json(journal, record)
                    return copy.deepcopy(record['result'])
                if previous != record['previous']:
                    raise ContractError('unknown_effect', 'Scope preparation outcome requires inspection')
            if (previous.get('generation') != generation or previous.get('epoch') != epoch):
                raise ContractError('stale_scope', 'Authorization changed; refresh before preparing scope')
            if identifier in previous['requests']:
                raise ContractError('request_conflict', 'request_id already used for authorization')
            state = runner.load_state()
            _settled(runner, previous, state)
            for path in journal.parent.glob('*.json'):
                pending = read_json(path)
                if (path != journal and pending.get('action') == 'prepare-scope'
                        and pending.get('status') == 'pending'):
                    raise ContractError('unknown_effect', 'Finish the previous scope preparation first')
            options = copy.deepcopy(previous['options'])
            options['items'] = items
            options = start_options(runner, options, state)
            from .tracking import Tracking
            tracking = Tracking(runner.repo / previous['goal'])
            keys = [tracking.canonical(item) for item in items.split(',')]
            if len(keys) != len(set(keys)):
                raise ContractError('invalid_payload', 'Duplicate selected items')
            if record is None:
                prepared = copy.deepcopy(previous)
                prepared.update(generation=str(uuid.uuid4()), epoch=1, desired_state='paused',
                                options=options, initial_options=options,
                                originating_request_id=identifier, requests={})
                authorization = {'schema_version': 1, 'generation': prepared['generation'], 'epoch': 1,
                                 'goal': prepared['goal'], 'request_id': identifier, 'status': 'authorized'}
                prepared['requests'][identifier] = {'request': {'options': options, 'generation': None,
                                                             'epoch': None}, 'result': authorization}
                goal_intent.validate(runner, prepared)
                archive = runner.state_dir / 'goal-authorizations' / (generation + '.json')
                result = {'schema_version': 1, 'status': 'succeeded', 'request_id': identifier,
                          'generation': prepared['generation'], 'epoch': 1, 'desired_state': 'paused',
                          'options': options, 'archive': str(archive)}
                record = {'schema_version': 1, 'id': identifier, 'action': 'prepare-scope',
                          'identity': runner.context.identity(), 'command': command, 'status': 'pending',
                          'previous': previous, 'prepared': prepared, 'result': result}
                atomic_json(journal, record)
            archive = runner.state_dir / 'goal-authorizations' / (generation + '.json')
            if archive.exists():
                if read_json(archive) != previous:
                    raise ContractError('request_conflict', 'Archived authorization differs; inspect before retry')
            else:
                atomic_json(archive, previous)
            atomic_json(runner.state_dir / 'goal-intent.json', record['prepared'])
            record['status'] = 'succeeded'
            atomic_json(journal, record)
            return copy.deepcopy(record['result'])
