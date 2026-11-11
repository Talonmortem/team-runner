"""Resume authorized gaps; never accepts work or repeats an unknown launch."""
import uuid
import math

from . import goal_intent
from .runner_store import atomic_json, lock, read_json, running

DELAYS = (2, 5, 15)


def tick(runner, now, stopped=lambda: False):
    from .goal_completion import read as completion
    from .project_services import departed
    from .recovery import _unknown
    from .service import start_runner

    intent = goal_intent.read(runner)
    if not intent or intent['desired_state'] != 'running' or not intent['policy']['recovery']:
        return
    if completion(runner) or stopped():
        return
    from .catalog import Catalog
    from . import service_intent
    service = service_intent.read(Catalog(runner.context.config_root, runner.context.state_root),
                                  runner.context.project_id)
    if service and service['desired_state'] != 'running':
        return
    service_guard = {'expected_service_epoch': service['epoch']} if service else {}
    scope = {k: intent[k] for k in ('generation', 'epoch')}
    path = runner.state_dir / 'goal-controller.json'
    # Same order as execute; no catalog/project lock held while observing effects.
    with lock(runner.state_dir / 'execute.lock', blocking=False):
        if running(runner.state_dir):
            return
        state = runner.load_state()
        if state.get('current'):
            return  # Attempt recovery belongs to the existing supervisor.
        try:
            journal = read_json(path, {})
        except (ValueError, UnicodeError):
            journal = {'invalid': True}
        if journal and (not isinstance(journal, dict) or journal.get('schema_version') != 1
                        or journal.get('identity') != runner.context.identity()
                        or not isinstance(journal.get('scope'), dict)
                        or type(journal.get('checks')) is not int or not 0 <= journal['checks'] <= len(DELAYS)
                        or journal.get('status') not in ('waiting', 'recovering', 'observing', 'needs_action')
                        or (journal.get('next_check_at') is not None and
                            (type(journal['next_check_at']) not in (int, float)
                             or not math.isfinite(journal['next_check_at'])))):
            atomic_json(path, {'schema_version': 1, 'identity': runner.context.identity(), 'scope': scope,
                              'checks': 0, 'status': 'needs_action', 'reason': 'invalid_controller_journal',
                              'next_check_at': None})
            return
        accepted = [entry for entry in state.get('history', [])
                    if entry.get('outcome') == 'succeeded' and entry.get('problems') == []]
        from .protocol import digest
        progress = digest(accepted)
        progressed = bool(accepted and journal.get('progress') and journal['progress'] != progress)
        if journal.get('scope') != scope or progressed:
            journal = {'schema_version': 1, 'identity': runner.context.identity(), 'scope': scope,
                       'checks': 0, 'status': 'waiting', 'progress': progress, 'next_check_at': now + DELAYS[0]}
            atomic_json(path, journal)
            return
        if journal.get('identity') != runner.context.identity():
            raise ValueError('goal controller identity mismatch')
        if journal.get('status') == 'needs_action' or now < (journal.get('next_check_at') or 0):
            return
        if journal['checks'] >= len(DELAYS):
            journal.update(status='needs_action', reason='recovery_budget_exhausted', next_check_at=None)
            atomic_json(path, journal)
            return
        journal['checks'] += 1
        reason = None
        with lock(runner.state_dir / 'runner.lock', blocking=False):
            state = runner.load_state()
            if state.get('current') or stopped() or not goal_intent.permitted(runner, **scope):
                return
            if state.get('stop_requested') and state.get('stop_status') != 'stopped':
                return
            if state.get('control_uncertain') or state.get('recovery_issues') or _unknown(runner, state):
                reason = 'unknown_effect'
            launch = read_json(runner.state_dir / 'launch.json', {})
            if launch:
                process = read_json(runner.state_dir / 'runner-process.json', {})
                previous_auth = launch.get('authorization') or {}
                same_generation = (previous_auth.get('generation') == scope['generation']
                                   and type(previous_auth.get('epoch')) is int
                                   and 1 <= previous_auth['epoch'] <= scope['epoch'])
                if (launch.get('identity') != runner.context.identity() or not same_generation
                        or process.get('identity') != runner.context.identity()
                        or process.get('nonce') != launch.get('nonce')
                        or not (runner.state_dir / ('launch-' + str(launch.get('nonce')) + '.claim')).exists()
                        or not departed(process)):
                    reason = 'runner_death_unconfirmed'
            elif state.get('run_id') or state.get('history'):
                reason = 'runner_launch_evidence_missing'
            if reason is None and completion(runner):
                return
            if reason is None:
                # Only a proved dead, claimed incarnation may release startup's
                # unknown fence. The new runner retains its prior queue/history.
                if launch:
                    with lock(runner.state_dir / 'start.lock'):
                        if read_json(runner.state_dir / 'launch.json', {}) != launch:
                            reason = 'runner_launch_changed'
                        else:
                            atomic_json(runner.state_dir / 'startup.json',
                                        {'status': 'idle', 'nonce': launch['nonce']})
                if reason is None:
                    journal.update(status='recovering', phase='launch', request_id=str(uuid.uuid4()),
                                   previous_nonce=launch.get('nonce'), next_check_at=None)
                    atomic_json(path, journal)
        if reason is None:
            try:
                if stopped() or not goal_intent.permitted(runner, **scope):
                    return
                result = start_runner(runner, {'resume': True}, recovery=True, expected_authorization=scope, **service_guard)
                journal.update(status='observing', phase='accepted', result=result,
                               reason=None, next_check_at=now + DELAYS[min(journal['checks'], len(DELAYS)-1)])
            except (ValueError, RuntimeError, OSError):
                reason = 'launch_unconfirmed'
        if reason:
            exhausted = journal['checks'] >= len(DELAYS)
            journal.update(status='needs_action' if exhausted else 'waiting', reason=reason,
                           next_check_at=None if exhausted else now + DELAYS[journal['checks']])
        atomic_json(path, journal)
