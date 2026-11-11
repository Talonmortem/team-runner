"""Explicit start; detached runners outlive the panel and never autostart."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import uuid
from .catalog import Catalog
from .runner_store import atomic_json, read_json, lock, running
from .runner_entry import process_stamp, boot_id
from .protocol import ContractError


def _adopt_running(runner, options, authorization):
    """A held checkout lock alone is never evidence of the requested runner."""
    launch = read_json(runner.state_dir / 'launch.json', {})
    startup = read_json(runner.state_dir / 'startup.json', {})
    process = read_json(runner.state_dir / 'runner-process.json', {})
    expected = {key: authorization[key] for key in ('generation', 'epoch')} if authorization else None
    nonce, pid = launch.get('nonce'), process.get('pid')
    valid = (nonce and pid and launch.get('identity') == runner.context.identity()
             and process.get('identity') == runner.context.identity()
             and launch.get('options') == options and launch.get('authorization') == expected
             and process.get('nonce') == nonce and startup.get('nonce') == nonce
             and startup.get('status') == 'running' and process.get('boot_id') == boot_id()
             and process.get('process_stamp') and process_stamp(pid) == process['process_stamp']
             and (runner.state_dir / ('launch-' + nonce + '.claim')).exists()
             and running(runner.state_dir))
    if not valid:
        raise ValueError('running runner identity/options/authorization unconfirmed')
    return {'status': 'running', 'nonce': nonce}


@contextmanager
def _service_permission(catalog, ctx, epoch):
    if epoch is None:
        yield
        return
    from . import service_intent
    with lock(service_intent.directory(catalog, ctx.project_id) / 'intent.lock'):
        if not service_intent.permitted(catalog, ctx.project_id, epoch):
            raise ContractError('authorization_cancelled', 'service authorization changed before runner start',
                                details={'no_effect': True})
        yield


def start_runner(runner, data, *, recovery=False, expected_authorization=None, expected_service_epoch=None):
    ctx = runner.context
    catalog = Catalog(ctx.config_root, ctx.state_root)
    # Per-workspace start -> checkout -> brief catalog reservation. Catalog
    # edits never wait for checkout (their acquisition is nonblocking). Orca
    # RPC runs only after the durable starting marker protects address edits.
    with lock(runner.state_dir / 'start.lock'):
        with lock(ctx.config_root / 'catalog.lock'):
            latest = catalog.context(ctx.project_id, ctx.workspace_id)
            if latest.identity() != ctx.identity(): raise ValueError('workspace identity changed')
        from .goal_intent import read as read_intent, permitted
        authorization = read_intent(runner)
        if expected_authorization and not permitted(runner, expected_authorization['generation'], expected_authorization['epoch']):
            raise ContractError('authorization_cancelled', 'goal authorization changed before start',
                                details={'no_effect': True})
        with _service_permission(catalog, ctx, expected_service_epoch):
            pass
        effective_data = authorization['options'] if authorization and data == {'resume': True} else data
        if running(runner.state_dir):
            from .application import start_options
            options = start_options(runner, effective_data, runner.load_state())
            with lock(runner.state_dir / 'goal-intent.lock'), _service_permission(catalog, ctx, expected_service_epoch):
                if authorization and not permitted(runner, authorization['generation'], authorization['epoch']):
                    raise ContractError('authorization_cancelled', 'goal authorization changed before adoption',
                                        details={'no_effect': True})
                return _adopt_running(runner, options, authorization)
        with lock(ctx.checkout_lock, blocking=False):
            previous_startup = read_json(runner.state_dir / 'startup.json')
            previous_launch = read_json(runner.state_dir / 'launch.json')
            startup = previous_startup or {}
            if startup.get('status') in ('starting', 'unknown'):
                raise ValueError('runner start unconfirmed; reconcile before another start')
            st = runner.load_state()
            restart_idle = (st.get('stop_requested') and not st.get('current')
                            and st.get('stop_status') == 'stopped' and not st.get('control_uncertain')
                            and not st.get('recovery_issues'))
            if (st.get('stop_requested') and not restart_idle) or (st.get('current') or {}).get('failed'):
                raise ValueError('retry stopped/failed attempt first')
            from .application import start_options
            options = start_options(runner, effective_data, st)
            if authorization:
                if authorization['desired_state'] != 'running':
                    raise ValueError('goal authorization cancelled')
                if data == {'resume': True}:
                    options = authorization['options']
                elif options != authorization['options']:
                    raise ValueError('launch options differ from authorized policy')
            goal = options['goal']
            nonce = str(uuid.uuid4())
            with lock(ctx.config_root / 'catalog.lock'):
                latest = catalog.context(ctx.project_id, ctx.workspace_id)
                if latest.identity() != ctx.identity(): raise ValueError('workspace identity changed')
                catalog.claim(ctx)
                # Start options and exact identity durable before any process/terminal creation.
                launch = {'nonce': nonce, 'identity': ctx.identity(), 'options': options}
                if authorization:
                    launch['authorization'] = {key: authorization[key] for key in ('generation', 'epoch')}
                atomic_json(runner.state_dir / 'launch.json', launch)
                atomic_json(runner.state_dir / 'startup.json', {'status': 'starting', 'nonce': nonce})
            effect_started = False
            verified = False
            try:
                ctx.verify(runner.orca)
                verified = True
                if st.get('goal') and st['goal'] != goal:
                    # Explicit new goal only after the old attempt settled. Preserve historical IDs.
                    if st.get('mailbox'):
                        from . import runner_operations as operations
                        key = 'close-main:' + st['run_id']
                        journal = runner.state_dir / 'operations.json'
                        recorded = read_json(journal, {}).get(key)
                        if not recorded:
                            terminal = runner.orca('terminal', 'show', '--terminal', st['mailbox'])['terminal']
                            if terminal.get('incarnationId') != st.get('mailbox_incarnation') or terminal.get('worktreeId') != ctx.orca_id:
                                raise ValueError('previous mailbox identity unknown')
                        effect_started = True
                        operations.mutation(journal, key, runner.orca, 'terminal', 'close', '--terminal', st['mailbox'],
                                            evidence={'terminal':st['mailbox'], 'incarnation':st['mailbox_incarnation']})
                        operations.terminal_evidence(read_json(journal)[key], runner.orca)
                    atomic_json(runner.state_dir / 'archives' / (str(uuid.uuid4()) + '.json'), st)
                    runner.save_state({'history': []})
                elif restart_idle:
                    # Explicit start after stopping between items; there is no attempt to retry.
                    for key in ('stop_requested', 'stop_status', 'stop_error'):
                        st.pop(key, None)
                    runner.save_state(st)
                from .recovery import settings
                with lock(runner.state_dir / 'recovery-settings.lock'):
                    recovery_settings = settings(runner)
                    if recovery and (not recovery_settings['enabled'] or recovery_settings['intent'] != 'running'):
                        raise ValueError('automatic resume cancelled by user intent')
                    if not (runner.state_dir / 'recovery-settings.json').exists():
                        recovery_settings['enabled'] = True
                    recovery_settings['intent'] = 'running'
                    atomic_json(runner.state_dir / 'recovery-settings.json', recovery_settings)
                argv = ['env', *[k+'='+v for k,v in ctx.environment().items()],
                        sys.executable, '-m', 'team_runner', '_runner', '--project', ctx.project_id,
                        '--workspace', ctx.workspace_id, '--nonce', nonce]
                # Cancellation and terminal creation serialize on this workspace only.
                # No manager/catalog-wide lock is acquired by cancellation.
                with lock(runner.state_dir / 'goal-intent.lock'), _service_permission(catalog, ctx, expected_service_epoch):
                    if authorization and not permitted(runner, authorization['generation'], authorization['epoch']):
                        raise ContractError('authorization_cancelled', 'goal authorization changed before launch',
                                            details={'no_effect': True})
                    effect_started = True
                    terminal = runner.launcher.create_terminal(runner.state_dir / 'startup', 'runner-' + nonce,
                                                               argv, 'team-runner ' + ctx.workspace_id, transport=runner.orca)
                result = {'status': 'starting', 'terminal': terminal['handle'], 'nonce': nonce}
                if read_json(runner.state_dir / 'startup.json', {}).get('status') != 'running':
                    atomic_json(runner.state_dir / 'startup.json', result)
                return result
            except Exception:
                cancelled = {'status': 'unknown' if effect_started else 'cancelled', 'nonce': nonce}
                if not verified:
                    # Verification is read-only: retain its cancelled reservation,
                    # then restore the pre-existing startup/launch projection.
                    atomic_json(runner.state_dir / 'start-reservations' / (nonce + '.json'), cancelled)
                    for name, previous in (('startup.json', previous_startup), ('launch.json', previous_launch)):
                        if previous is None:
                            (runner.state_dir / name).unlink(missing_ok=True)
                        else:
                            atomic_json(runner.state_dir / name, previous)
                else:
                    atomic_json(runner.state_dir / 'startup.json', cancelled)
                raise



def run_process(runner, nonce):
    from types import SimpleNamespace
    ctx = runner.context
    with lock(ctx.checkout_lock), lock(runner.state_dir / 'runner.lock', blocking=False):
        request = read_json(runner.state_dir / 'launch.json', {})
        if request.get('nonce') != nonce or request.get('identity') != ctx.identity():
            raise ValueError('runner launch identity/nonce mismatch')
        from .goal_intent import permitted, read as read_intent
        authorization = request.get('authorization')
        active_intent = read_intent(runner)
        if active_intent and not authorization:
            raise ValueError('legacy runner launch lacks goal authorization')
        if active_intent and request.get('options') != active_intent['options']:
            raise ValueError('runner launch policy differs from authorization')
        if authorization and not permitted(runner, authorization['generation'], authorization['epoch']):
            raise ValueError('runner launch authorization expired')
        # Single-consumption nonce survives process restarts; use run/resume for new launches.
        fd = os.open(str(runner.state_dir / ('launch-' + nonce + '.claim')), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        ctx.verify(runner.orca)
        Catalog(ctx.config_root, ctx.state_root).claim(ctx)
        atomic_json(runner.state_dir / 'runner-process.json', {'pid': os.getpid(),
                    'process_stamp': process_stamp(os.getpid()), 'boot_id': boot_id(), 'nonce': nonce, 'identity': ctx.identity(), 'recovery_protocol': 1})
        atomic_json(runner.state_dir / 'startup.json', {'status': 'running', 'nonce': nonce})
        defaults = dict(goal=None, mode='confirm', items=None, final=False, config=None,
                        agent=None, provider=None, profile=None, model=None, effort=None, handoff=None, fast=None)
        defaults.update(request['options'])
        try:
            from .recovery import settings
            if settings(runner)['intent'] != 'running' or authorization and not permitted(
                    runner, authorization['generation'], authorization['epoch']):
                return 0
            runner.authorization = authorization
            return runner.cmd_run(SimpleNamespace(**defaults)) or 0
        except Exception as exc:
            st = runner.load_state(); st['last_error'] = str(exc); runner.save_state(st)
            raise
        finally:
            st = runner.load_state(); st['runner_status'] = 'idle'; runner.save_state(st)
            atomic_json(runner.state_dir / 'startup.json', {'status': 'idle', 'nonce': nonce})
