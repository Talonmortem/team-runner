"""Bounded, opt-in technical recovery. No acceptance decisions or effect replay."""
from pathlib import Path
import hashlib
import json
import threading
import subprocess
import time
import uuid

from .runner_store import atomic_json, enqueue, lock, read_json, running

DELAYS = (2, 5, 15, 30, 60, 120)


def settings(runner):
    from .goal_intent import read
    intent = read(runner)
    if intent:
        return {'enabled': intent['policy']['recovery'], 'intent': intent['desired_state']}
    return read_json(runner.state_dir / 'recovery-settings.json', {'enabled': False, 'intent': 'paused'})


def configure(runner, enabled):
    if not isinstance(enabled, bool):
        raise ValueError('enabled must be boolean')
    from .goal_intent import change
    change(runner, recovery=enabled)
    with lock(runner.state_dir / 'recovery-settings.lock'):
        value = settings(runner)
        if not (runner.state_dir / 'recovery-settings.json').exists():
            st = runner.load_state()
            value['intent'] = 'stopped' if st.get('stop_requested') else 'paused' if st.get('paused') else 'running'
        value['enabled'] = enabled
        atomic_json(runner.state_dir / 'recovery-settings.json', value)
    return recovery_view(runner)


def set_intent(runner, intent, *, new_start=False):
    if intent not in ('running', 'paused', 'stopped'):
        raise ValueError('invalid recovery intent')
    from .goal_intent import change
    change(runner, desired_state=intent)
    with lock(runner.state_dir / 'recovery-settings.lock'):
        path = runner.state_dir / 'recovery-settings.json'
        value = settings(runner)
        if new_start and not path.exists():
            value['enabled'] = True
        value['intent'] = intent
        atomic_json(path, value)


def recovery_view(runner):
    value = settings(runner)
    journal = read_json(runner.state_dir / 'recovery.json', {})
    result = {k: journal.get(k) for k in ('reason', 'action', 'next_step', 'next_check_at', 'incident_id', 'checks')}
    result.update(enabled=value['enabled'], intent=value['intent'], status=journal.get('status', 'observing'))
    if not value['enabled']:
        result['status'] = 'disabled'
    elif value['intent'] == 'paused' and (runner.load_state().get('current') or {}).get('retry_config'):
        result.update(status='observing', reason=None, action='Повтор подготовлен',
                      next_check_at=None, next_step='Нажмите «Продолжить»')
    elif value['intent'] in ('stopped', 'paused') and runner.load_state().get('stop_status') == 'stopped':
        result.update(status='observing', reason=None, action='stop confirmed',
                      next_check_at=None, next_step='stopped')
        if (runner.load_state().get('current') or {}).get('attempt', 1) >= 3:
            result.update(status='needs_action',
                          reason='Достигнут предел трёх попыток текущего пункта',
                          next_step='Для ручного повтора нажмите «Разрешить ещё одну попытку»')
    elif value['intent'] == 'paused' or (value['intent'] == 'stopped' and journal.get('status') not in ('waiting', 'recovering', 'needs_action')):
        result.update(status='observing', next_check_at=None, next_step=value['intent'])
    return result


def target(st):
    cur = st.get('current') or {}
    return cur.get('attempt_dir') or cur.get('dispatch_id') or cur.get('item')


def _unknown(runner, st):
    paths = [runner.state_dir / 'operations.json', runner.launcher.journal(runner.state_dir / 'startup')]
    cur = st.get('current') or {}
    if st.get('mailbox_dir'):
        paths.append(runner.launcher.journal(st['mailbox_dir']))
    if cur.get('attempt_dir'):
        paths.append(runner.launcher.journal(cur['attempt_dir']))
    return any(op.get('status') not in ('completed', 'rejected')
               for path in paths for op in read_json(path, {}).values())


class Supervisor:
    def __init__(self, catalog, project_id, runner_factory=None, clock=time.time):
        if runner_factory is None:
            from .engine import Runner
            runner_factory = Runner
        self.catalog, self.project_id, self.runner_factory, self.clock = catalog, project_id, runner_factory, clock
        self.stop_event = threading.Event()
        self.threads = {}
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._watch, name='recovery-project', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        # Calls are bounded by their Orca transport; never hold up HTTP shutdown.
        for thread in self.threads.values():
            thread.join(timeout=.1)

    def _watch(self):
        while not self.stop_event.is_set():
            try:
                workspaces = self.catalog.read()['projects'][self.project_id]['workspaces']
                for workspace_id in workspaces:
                    if workspace_id not in self.threads or not self.threads[workspace_id].is_alive():
                        thread = threading.Thread(target=self._workspace, args=(workspace_id,), daemon=True,
                                                  name='recovery-' + workspace_id)
                        self.threads[workspace_id] = thread
                        thread.start()
            except (OSError, ValueError, KeyError):
                pass
            self.stop_event.wait(1)

    def _workspace(self, workspace_id):
        while not self.stop_event.is_set():
            try:
                context = self.catalog.context(self.project_id, workspace_id)
                self.tick(self.runner_factory(context))
            except (OSError, ValueError, RuntimeError, KeyError):
                pass
            self.stop_event.wait(1)

    def _live(self, runner, st):
        """Schedule technical work through the live owner's existing command queue."""
        cur = st.get('current') or {}
        attempt = target(st)
        registry = runner.launcher.registry(cur['attempt_dir']) if cur.get('attempt_dir') else {'agents': [], 'runs': []}
        unsettled = bool(registry.get('fenced')) or any(a.get('status') in ('launching', 'unknown', 'ready', 'restart_ready') or a.get('error') or a.get('cleanup_error')
                        for a in registry['agents'])
        unsettled = unsettled or bool(st.get('recovery_issues') or st.get('control_uncertain') or _unknown(runner, st))
        for op in read_json(runner.state_dir / 'operations.json', {}).values():
            args = op.get('args') or []
            if op.get('status') != 'rejected' or 'dispatch_inactive' not in str(op.get('error', '')) or '--id' not in args:
                continue
            message_id = args[args.index('--id') + 1]
            run_id = args[args.index('--run') + 1] if '--run' in args else None
            unsettled = unsettled or any(m.get('id') == message_id and not m.get('closed')
                and not m.get('answered') and (run_id is None or m.get('run_id') == run_id)
                for m in cur.get('messages', []))
        signature = hashlib.sha256(json.dumps({'registry': registry, 'issues': st.get('recovery_issues'),
                    'uncertain': st.get('control_uncertain'), 'operations': read_json(runner.state_dir / 'operations.json', {})},
                    sort_keys=True).encode()).hexdigest()
        journal_path = runner.state_dir / 'recovery.json'
        journal = read_json(journal_path, {})
        if (journal.get('target') != attempt or journal.get('mode') != 'live'
                or (journal.get('status') == 'observing' and journal.get('signature') != signature)):
            if not unsettled:
                return
            journal = {'incident_id': str(uuid.uuid4()), 'target': attempt, 'mode': 'live', 'checks': 0,
                       'status': 'waiting', 'reason': 'technical state requires reconciliation',
                       'next_check_at': self.clock() + DELAYS[0], 'action': None,
                       'next_step': 'queue technical reconciliation', 'signature': signature}
            atomic_json(journal_path, journal)
            return
        if read_json(runner.state_dir / 'runner-process.json', {}).get('recovery_protocol') != 1:
            journal.update(status='needs_action', reason='runner lacks technical recovery command; resume after confirmed exit',
                           next_check_at=None, next_step='wait for confirmed runner exit')
            atomic_json(journal_path, journal)
            return
        command_id = journal.get('command_id')
        command = read_json(runner.state_dir / 'commands' / (command_id + '.json'), {}) if command_id else {}
        if command.get('status') == 'done':
            journal.update(status='observing', phase='completed', next_check_at=None, next_step='observe runner', signature=signature)
            journal.pop('command_id', None)
            atomic_json(journal_path, journal)
            return
        if command.get('error'):
            journal['reason'] = command['error']
            if 'acceptance or user decision required' in command['error'] or 'cancelled by user intent' in command['error']:
                journal.update(status='needs_action', next_check_at=None, next_step='review current attempt')
                atomic_json(journal_path, journal)
                return
        if journal.get('status') == 'observing':
            # Resolved rejected replies are historical; do not create another incident.
            return
        if journal.get('status') == 'needs_action' or self.clock() < (journal.get('next_check_at') or 0):
            return
        if journal.get('checks', 0) >= len(DELAYS):
            journal.update(status='needs_action', next_check_at=None, next_step='inspect queued recovery evidence')
            atomic_json(journal_path, journal)
            return
        count = journal.get('checks', 0) + 1
        if not command_id or command.get('status') in ('error', 'unknown'):
            command_id = str(uuid.uuid4())
        journal.update(checks=count, command_id=command_id, status='recovering', phase='intent',
                       action='queued technical reconciliation',
                       next_check_at=self.clock() + DELAYS[min(count, len(DELAYS) - 1)],
                       next_step='await runner command result')
        atomic_json(journal_path, journal)
        if self.stop_event.is_set() or not settings(runner)['enabled'] or settings(runner)['intent'] != 'running' or target(runner.load_state()) != attempt:
            return
        # An interrupted enqueue always uses the persisted UUID. No duplicate dispatch.
        enqueue(runner.state_dir, 'technical-reconcile', {'_target': attempt}, command_id)
        journal['phase'] = 'queued'
        atomic_json(journal_path, journal)

    def tick(self, runner):
        from .project_services import departed
        from .service import start_runner
        if self.stop_event.is_set() or not settings(runner)['enabled']:
            return
        with lock(runner.state_dir / 'recovery-supervisor.lock', blocking=False):
            st = runner.load_state()
            cur = st.get('current') or {}
            intent = settings(runner)['intent']
            from . import goal_intent, service_intent
            authorization = goal_intent.read(runner)
            launch_guards = {}
            if authorization:
                launch_guards['expected_authorization'] = {k: authorization[k] for k in ('generation', 'epoch')}
                from .catalog import Catalog
                service_catalog = Catalog(runner.context.config_root, runner.context.state_root)
                service = service_intent.read(service_catalog, runner.context.project_id)
                if service:
                    if service['desired_state'] != 'running':
                        return
                    launch_guards['expected_service_epoch'] = service['epoch']
            def authorization_current():
                current = goal_intent.read(runner) if authorization else {}
                return (not authorization or (all(current.get(k) == authorization[k] for k in ('generation', 'epoch'))
                        and current.get('desired_state') == intent)) and (
                    'expected_service_epoch' not in launch_guards or service_intent.permitted(
                        service_catalog, runner.context.project_id, launch_guards['expected_service_epoch']))
            if intent == 'paused' or (intent == 'stopped' and st.get('stop_status') == 'stopped'):
                return
            if running(runner.state_dir):
                if intent == 'running' and cur:
                    self._live(runner, st)
                return
            if not cur:
                from .goal_controller import tick
                tick(runner, self.clock(), self.stop_event.is_set)
                return
            journal_path = runner.state_dir / 'recovery.json'
            journal = read_json(journal_path, {})
            attempt = target(st)
            if journal.get('mode') == 'live' and journal.get('target') == attempt:
                journal.update(mode='dead', status='waiting', next_check_at=self.clock() + DELAYS[0],
                               reason='runner no longer owns process lock', next_step='verify runner process death')
                atomic_json(journal_path, journal)
                return
            if journal.get('target') != attempt:
                journal = {'incident_id': str(uuid.uuid4()), 'target': attempt, 'checks': 0,
                           'status': 'waiting', 'reason': 'runner not active',
                           'next_check_at': self.clock() + DELAYS[0], 'action': None,
                           'next_step': 'verify process and operation evidence'}
                atomic_json(journal_path, journal)
                return
            if journal.get('status') == 'needs_action' or self.clock() < (journal.get('next_check_at') or 0):
                return
            if journal.get('checks', 0) >= len(DELAYS):
                journal.update(status='needs_action', next_check_at=None, next_step='inspect interrupted recovery evidence')
                atomic_json(journal_path, journal)
                return
            journal.update(status='recovering', checks=journal['checks'] + 1, next_check_at=None)
            atomic_json(journal_path, journal)
            resume = False
            try:
                with lock(runner.state_dir / 'runner.lock', blocking=False):
                    st = runner.load_state()
                    if target(st) != attempt or settings(runner)['intent'] != intent:
                        return
                    runner.process_controls(st)
                    st = runner.load_state()
                    if target(st) != attempt or settings(runner)['intent'] != intent:
                        return
                    process = read_json(runner.state_dir / 'runner-process.json', {})
                    if not process:
                        launch = read_json(runner.state_dir / 'launch.json', {})
                        nonce = launch.get('nonce')
                        marker = read_json(runner.state_dir / 'startup' / ('runner-' + str(nonce) + '.entry.json'), {})
                        expected = 'runner-' + str(nonce) + ':' + str((runner.state_dir / 'startup').resolve())
                        terminal = marker.get('terminal') or {}
                        if (nonce and launch.get('identity') == runner.context.identity() and marker.get('nonce') == expected
                                and terminal.get('worktreeId') == runner.context.orca_id and terminal.get('incarnationId')):
                            process = {**marker, 'identity': launch['identity'], 'nonce': nonce}
                    launch = read_json(runner.state_dir / 'launch.json', {})
                    if (not launch.get('nonce') or process.get('nonce') != launch['nonce']
                            or process.get('identity') != runner.context.identity() or launch.get('identity') != runner.context.identity()):
                        raise ValueError('runner process launch identity unconfirmed')
                    journal['evidence'] = {'runner': process, 'process_departed': departed(process)}
                    if not departed(process):
                        raise ValueError('runner process death unconfirmed')
                    if self.stop_event.is_set():
                        return
                    st['runner_status'] = 'idle'
                    runner.save_state(st)
                    runner.recover(st, technical_only=True)
                    st = runner.load_state()
                    cur = st.get('current') or {}
                    permitted = lambda: (authorization_current() and not self.stop_event.is_set() and settings(runner)['enabled'] and settings(runner)['intent'] == intent
                                         and target(runner.load_state()) == attempt)
                    journal.update(action='clean up accepted reports', phase='intent')
                    atomic_json(journal_path, journal)
                    runner.cleanup_accepted_reports(st, permitted=permitted)
                    journal.update(phase='completed')
                    atomic_json(journal_path, journal)
                    if st.get('recovery_issues') or st.get('control_uncertain') or _unknown(runner, st):
                        raise ValueError('; '.join(st.get('recovery_issues') or ['operation outcome unknown']))
                    if not permitted():
                        return
                    with lock(runner.state_dir / 'start.lock'):
                        atomic_json(runner.state_dir / 'startup.json', {'status': 'idle', 'nonce': launch['nonce']})
                    if intent == 'stopped':
                        journal.update(action='finish requested stop', phase='intent')
                        atomic_json(journal_path, journal)
                        runner.apply_control(st, 'stop', {})
                        journal.update(status='observing', phase='completed', next_step='stopped')
                    else:
                        own_stop = (journal.get('coordinator_retry') and st.get('stop_requested'))
                        if (cur.get('failed') or cur.get('problems') or 'acceptance_rework' in cur or st.get('paused') or (st.get('stop_requested') and not own_stop)
                                or (cur.get('gate_id') and not cur.get('gate_resolved'))
                                or any(m.get('type') in ('question', 'escalation') and not m.get('closed') and not m.get('answered') for m in cur.get('messages', []))):
                            journal.update(status='needs_action', reason='acceptance or user decision required', next_step='review current attempt')
                            atomic_json(journal_path, journal)
                            return
                        if cur.get('attempt_dir') and not own_stop:
                            journal.update(action='continue proved terminal launch', phase='intent')
                            atomic_json(journal_path, journal)
                            with lock(runner.state_dir / 'goal-intent.lock'):
                                if not permitted():
                                    return
                                runner.launcher.resume_ready_launches(cur['attempt_dir'], runner.state_dir, permitted=permitted)
                            runner.recover(st, technical_only=True)
                            st = runner.load_state()
                            cur = st.get('current') or {}
                        if not permitted():
                            return
                        if st.get('recovery_issues') or st.get('control_uncertain') or _unknown(runner, st):
                            raise ValueError('; '.join(st.get('recovery_issues') or ['operation outcome unknown']))
                        reports = runner.accepted_child_reports(cur['attempt_dir']) if cur.get('attempt_dir') else {}
                        accepted = reports.get(cur.get('dispatch_id'))
                        verdict = runner.launcher.liveness(cur['dispatch_id'], st.get('mailbox')) if cur.get('dispatch_id') else None
                        if verdict not in ('live', 'exited') and cur.get('attempt_dir'):
                            registry = runner.launcher.registry(cur['attempt_dir'])
                            agents = registry.get('agents', []) if isinstance(registry, dict) else []
                            closed = next((a for a in agents if a.get('dispatch_id') == cur.get('dispatch_id')
                                and a.get('status') == 'stopped' and a.get('terminal_closed')
                                and isinstance(a.get('close_evidence'), dict)), None)
                            if closed:
                                verdict = 'exited'
                                journal['evidence']['coordinator_process'] = closed['close_evidence']
                        journal['evidence']['coordinator'] = verdict
                        if verdict not in (None, 'alive', 'running', 'live', 'exited') and not accepted and not own_stop:
                            raise ValueError('coordinator liveness unconfirmed: ' + str(verdict))
                        if (verdict == 'exited' and not accepted) or own_stop:
                            if cur.get('attempt', 1) >= 3:
                                journal.update(status='needs_action', next_check_at=None,
                                               reason='three attempts exhausted; review task',
                                               action='review attempt limit', phase='completed',
                                               next_step='explicitly authorize one extra attempt')
                                atomic_json(journal_path, journal)
                                return
                            journal.update(action='stop executors before retry', phase='intent', coordinator_retry=True)
                            atomic_json(journal_path, journal)
                            if not permitted():
                                return
                            runner.apply_control(st, 'stop', {})
                            if not permitted():
                                return
                            if st.get('stop_status') != 'stopped' or _unknown(runner, st):
                                raise ValueError('executors not confirmed stopped')
                            journal.update(action='retry with saved snapshot', phase='intent')
                            atomic_json(journal_path, journal)
                            runner.apply_control(st, 'retry', {})
                            journal['target'] = target(st)
                        journal.update(action='resume saved attempt', phase='intent')
                        atomic_json(journal_path, journal)
                        resume = True
                if resume and not self.stop_event.is_set() and settings(runner)['intent'] == 'running':
                    # start_runner obtains checkout/start locks and rechecks all durable fences.
                    result = start_runner(runner, {'resume': True}, recovery=True, **launch_guards)
                    journal.update(status='observing', phase='completed', result=result, next_step='observe runner')
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                count = journal['checks']
                journal.update(reason=str(exc), status='needs_action' if count >= len(DELAYS) else 'waiting',
                               next_check_at=None if count >= len(DELAYS) else self.clock() + DELAYS[count],
                               next_step='inspect evidence' if count >= len(DELAYS) else 'verify again')
            atomic_json(journal_path, journal)
