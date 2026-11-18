"""Isolated supervisor evidence/intent tests. Never contacts Orca."""
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

from team_runner import recovery
from team_runner.runner_store import atomic_json, read_json


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.runner = Mock()
        self.runner.state_dir = self.path
        self.runner.context.identity.return_value = {'workspace': 'fixture'}
        self.runner.launcher.journal.side_effect = lambda p: Path(p) / 'operations.json'
        self.state = {'current': {'item': 'one', 'attempt_dir': str(self.path / 'attempt'), 'dispatch_id': 'dispatch'}, 'options': {'goal': 'fixture'}}
        self.runner.load_state.side_effect = lambda: self.state
        self.runner.accepted_child_reports.return_value = {}
        self.runner.launcher.liveness.return_value = 'running'
        self.runner.launcher.registry.return_value = {'agents': [], 'runs': []}
        self.now = 100
        self.supervisor = recovery.Supervisor(None, 'fixture', clock=lambda: self.now)
        recovery.configure(self.runner, True)
        recovery.set_intent(self.runner, 'running')
        atomic_json(self.path / 'runner-process.json', {'pid': 123, 'process_stamp': 'fixture', 'nonce': 'nonce', 'identity': {'workspace': 'fixture'}, 'recovery_protocol': 1})
        atomic_json(self.path / 'launch.json', {'nonce': 'nonce', 'identity': {'workspace': 'fixture'}})
        self.departed = patch('team_runner.project_services.departed', return_value=True).start()
        self.start = patch('team_runner.service.start_runner', return_value={'status': 'starting'}).start()
        self.addCleanup(patch.stopall)

    def check(self):
        self.supervisor.tick(self.runner)
        self.now += 200
        self.supervisor.tick(self.runner)
        return read_json(self.path / 'recovery.json')

    def test_dead_owner_deferral_preserves_attempt_incident_and_check_budget(self):
        from team_runner.memory_admission import WaitingMemory
        self.runner.launcher.resume_ready_launches.side_effect = WaitingMemory({'reason': 'low_available_memory'})
        self.runner.launcher.liveness.return_value = 'live'
        self.supervisor.tick(self.runner)
        path = self.path / 'recovery.json'
        initial = read_json(path)
        for _ in range(8):
            self.now += 2
            self.supervisor.tick(self.runner)
        waiting = read_json(path)
        self.assertEqual(waiting['status'], 'waiting_memory')
        self.assertEqual(waiting['incident_id'], initial['incident_id'])
        self.assertEqual(waiting['target'], initial['target'])
        self.assertEqual(waiting['checks'], 0)
        self.runner.apply_control.assert_not_called()
        self.start.assert_not_called()
        self.runner.launcher.resume_ready_launches.side_effect = None
        self.now += 2
        self.supervisor.tick(self.runner)
        self.start.assert_called_once()
        self.assertEqual(read_json(path)['checks'], 1)

    def test_runner_death_resumes_same_attempt(self):
        journal = self.check()
        self.start.assert_called_once_with(self.runner, {'resume': True}, recovery=True)
        self.runner.recover.assert_called_with(self.state, technical_only=True)
        self.runner.apply_control.assert_not_called()
        self.assertEqual(journal['phase'], 'completed')

    def test_failed_relaunch_uses_its_entry_marker_instead_of_departed_old_pid(self):
        identity = self.runner.context.identity()
        nonce = 'new-launch'
        self.runner.context.orca_id = 'fixture-worktree'
        atomic_json(self.path / 'launch.json', {'nonce': nonce, 'identity': identity})
        marker = {'nonce': 'runner-' + nonce + ':' + str((self.path / 'startup').resolve()),
                  'pid': 456, 'process_stamp': 'new-stamp', 'boot_id': 'fixture-boot',
                  'terminal': {'worktreeId': 'fixture-worktree', 'incarnationId': 'new-incarnation'}}
        atomic_json(self.path / 'startup' / ('runner-' + nonce + '.entry.json'), marker)
        journal = self.check()
        self.assertEqual(journal['status'], 'observing', journal)
        self.assertEqual(journal['evidence']['runner']['pid'], 456)
        self.assertEqual(journal['evidence']['runner']['nonce'], nonce)
        self.start.assert_called_once()

    def test_foreign_or_live_old_process_cannot_be_replaced_by_entry_marker(self):
        identity = self.runner.context.identity()
        self.runner.context.orca_id = 'fixture-worktree'
        atomic_json(self.path / 'launch.json', {'nonce': 'new-launch', 'identity': identity})
        atomic_json(self.path / 'startup' / 'runner-new-launch.entry.json', {
            'nonce': 'runner-new-launch:' + str((self.path / 'startup').resolve()),
            'pid': 456, 'process_stamp': 'new-stamp',
            'terminal': {'worktreeId': 'fixture-worktree', 'incarnationId': 'new-incarnation'}})
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                (self.path / 'recovery.json').unlink(missing_ok=True)
                atomic_json(self.path / 'runner-process.json', {'nonce': 'old', 'pid': 123,
                            'process_stamp': 'old-stamp', 'identity': {'foreign': True} if foreign else identity})
                self.departed.return_value = foreign
                journal = self.check()
                self.assertIn('unconfirmed', journal['reason'])
                self.start.assert_not_called()

    def test_unknown_liveness_never_restarts(self):
        self.runner.launcher.liveness.return_value = 'unverifiable'
        journal = self.check()
        self.start.assert_not_called()
        self.assertEqual(journal['status'], 'waiting')

    def test_acceptance_and_user_decisions_do_not_retry(self):
        for extra in ({'failed': True}, {'acceptance_rework': {}}, {'gate_id': 'gate'},
                      {'messages': [{'type': 'question', 'run_id': 'parent', 'payload': {'dispatchId': 'dispatch'}}]}):
            with self.subTest(extra=extra):
                self.state['current'].update(extra)
                self.runner.launcher.registry.return_value = {'agents': [{'id': 'coordinator', 'role': 'coordinator',
                    'dispatch_id': 'dispatch', 'run_id': 'parent', 'status': 'active'}], 'runs': []}
                self.check()
                self.start.assert_not_called()
                self.runner.apply_control.assert_not_called()
                for key in extra:
                    self.state['current'].pop(key)
                (self.path / 'recovery.json').unlink()

    def test_accepted_worker_done_does_not_retry_exited_coordinator(self):
        for outcome in ('succeeded', 'failed'):
            with self.subTest(outcome=outcome):
                self.runner.accepted_child_reports.return_value = {'dispatch': {'outcome': outcome}}
                self.runner.launcher.liveness.return_value = 'exited'
                self.check()
                self.runner.apply_control.assert_not_called()
                (self.path / 'recovery.json').unlink()

    def test_bounded_checks_survive_new_supervisor(self):
        self.departed.return_value = False
        self.supervisor.tick(self.runner)
        self.assertEqual(read_json(self.path / 'recovery.json')['next_check_at'], 102)
        for index in range(6):
            self.now += 200
            self.supervisor = recovery.Supervisor(None, 'fixture', clock=lambda: self.now)
            self.supervisor.tick(self.runner)
            journal = read_json(self.path / 'recovery.json')
            self.assertEqual(journal['checks'], index + 1)
            if index < 5:
                self.assertEqual(journal['next_check_at'], self.now + recovery.DELAYS[index + 1])
        self.assertEqual(journal['status'], 'needs_action')
        self.now += 1000
        self.supervisor.tick(self.runner)
        self.assertEqual(read_json(self.path / 'recovery.json')['checks'], 6)
        self.start.assert_not_called()

    def test_proved_lost_launch_is_automatically_queued_for_live_owner(self):
        from team_runner.runner_store import lock
        self.runner.launcher.registry.return_value = {
            'agents': [{'status': 'restart_ready'}], 'runs': [], 'fenced': False}
        with lock(self.path / 'runner.lock'):
            self.check()
        journal = read_json(self.path / 'recovery.json')
        self.assertEqual(journal['phase'], 'queued')
        command = read_json(self.path / 'commands' / (journal['command_id'] + '.json'))
        self.assertEqual(command['action'], 'technical-reconcile')
        self.start.assert_not_called()

    def test_live_runner_queues_one_durable_technical_command(self):
        from team_runner.runner_store import lock
        self.runner.launcher.registry.return_value = {'agents': [{'status': 'unknown'}], 'runs': []}
        with lock(self.path / 'runner.lock'):
            self.check()
            journal = read_json(self.path / 'recovery.json')
            command_id = journal['command_id']
            command = read_json(self.path / 'commands' / (command_id + '.json'))
            self.assertEqual(command['action'], 'technical-reconcile')
            self.now += 200
            self.supervisor = recovery.Supervisor(None, 'fixture', clock=lambda: self.now)
            self.supervisor.tick(self.runner)
            self.assertEqual(read_json(self.path / 'recovery.json')['command_id'], command_id)
            self.assertEqual(len(list((self.path / 'commands').glob('*.json'))), 1)
        self.runner.recover.assert_not_called()

    def test_live_legacy_runner_requires_action_only_when_unsettled(self):
        from team_runner.runner_store import lock
        atomic_json(self.path / 'runner-process.json', {})
        with lock(self.path / 'runner.lock'):
            self.supervisor.tick(self.runner)
            self.assertFalse((self.path / 'recovery.json').exists())
            self.runner.launcher.registry.return_value = {'agents': [{'status': 'unknown'}], 'runs': []}
            self.check()
            journal = read_json(self.path / 'recovery.json')
            self.assertEqual(journal['status'], 'needs_action')
            self.assertIn('lacks technical recovery command', journal['reason'])
            self.assertFalse((self.path / 'commands').exists())
        self.supervisor.tick(self.runner)
        self.assertEqual(read_json(self.path / 'recovery.json')['mode'], 'dead')

    def test_closed_historical_reply_does_not_trigger_live_incident(self):
        from team_runner.runner_store import lock
        self.state['current']['messages'] = [{'id': 'q', 'run_id': 'run', 'closed': True}]
        atomic_json(self.path / 'operations.json', {'reply': {'status': 'rejected',
                    'error': 'dispatch_inactive', 'args': ['orchestration', 'reply', '--id', 'q', '--run', 'run']}})
        with lock(self.path / 'runner.lock'):
            self.check()
        self.assertFalse((self.path / 'recovery.json').exists())

    def test_answered_question_closes_only_resolved_technical_incident(self):
        from team_runner.runner_store import lock
        question = {'id': 'q', 'type': 'question', 'run_id': 'parent',
                    'payload': {'dispatchId': 'dispatch'}}
        self.state['current']['messages'] = [question]
        registry = {'agents': [{'id': 'coordinator', 'role': 'coordinator',
                    'dispatch_id': 'dispatch', 'run_id': 'parent', 'status': 'active'}], 'runs': []}
        self.runner.launcher.registry.return_value = registry
        error = 'acceptance or user decision required; technical cleanup completed'
        command = {'id': 'cleanup', 'status': 'error', 'error': error}
        atomic_json(self.path / 'commands/cleanup.json', command)
        incident = {'target': recovery.target(self.state), 'mode': 'live', 'status': 'needs_action',
                    'checks': 1, 'reason': error, 'command_id': 'cleanup'}
        atomic_json(self.path / 'recovery.json', incident)
        with lock(self.path / 'runner.lock'):
            self.supervisor.tick(self.runner)
            self.assertEqual(read_json(self.path / 'recovery.json')['status'], 'needs_action')
            question['answered'] = True
            for extra in ({'failed': True}, {'problems': ['pending']}, {'acceptance_rework': {}},
                          {'gate_id': 'gate'}):
                with self.subTest(extra=extra):
                    self.state['current'].update(extra)
                    self.supervisor.tick(self.runner)
                    self.assertEqual(read_json(self.path / 'recovery.json')['status'], 'needs_action')
                    for key in extra:
                        self.state['current'].pop(key)
            registry['fenced'] = True
            self.supervisor.tick(self.runner)
            self.assertEqual(read_json(self.path / 'recovery.json')['status'], 'needs_action')
            registry['fenced'] = False
            self.supervisor.tick(self.runner)
            journal = read_json(self.path / 'recovery.json')
            self.assertEqual(journal['status'], 'observing')
            self.assertEqual(journal['completed_command_id'], 'cleanup')
            self.assertIsNone(journal['reason'])
            self.supervisor.tick(self.runner)
            self.assertEqual(read_json(self.path / 'recovery.json')['status'], 'observing')
        self.assertEqual(read_json(self.path / 'commands/cleanup.json'), command)
        self.assertEqual(len(list((self.path / 'commands').glob('*.json'))), 1)
        self.start.assert_not_called()

    def test_confirmed_live_owner_closes_only_resolved_dead_owner_incident(self):
        from team_runner.runner_store import lock
        self.departed.return_value = False
        process = read_json(self.path / 'runner-process.json')
        process['boot_id'] = 'boot'
        atomic_json(self.path / 'runner-process.json', process)
        incident = {'target': recovery.target(self.state), 'mode': 'dead', 'status': 'needs_action',
                    'checks': 6, 'reason': 'runner no longer owns process lock'}
        atomic_json(self.path / 'recovery.json', incident)
        question = {'id': 'q', 'type': 'question', 'run_id': 'parent',
                    'payload': {'dispatchId': 'dispatch'}}
        self.state['current']['messages'] = [question]
        self.runner.launcher.registry.return_value = {'agents': [{'id': 'coordinator',
                    'role': 'coordinator', 'dispatch_id': 'dispatch', 'run_id': 'parent',
                    'status': 'active'}], 'runs': []}
        with lock(self.path / 'runner.lock'), \
                patch('team_runner.runner_entry.process_stamp', return_value='fixture'), \
                patch('team_runner.runner_entry.boot_id', return_value='boot'):
            self.supervisor.tick(self.runner)
            self.assertEqual(read_json(self.path / 'recovery.json'), incident)
            question['answered'] = True
            atomic_json(self.path / 'launch.json', {'nonce': 'wrong', 'identity': {'workspace': 'fixture'}})
            self.supervisor.tick(self.runner)
            self.assertEqual(read_json(self.path / 'recovery.json'), incident)
            atomic_json(self.path / 'launch.json', {'nonce': 'nonce', 'identity': {'workspace': 'fixture'}})
            for changed in ({'pid': None}, {'process_stamp': 'stale'}, {'boot_id': 'old-boot'}):
                with self.subTest(changed=changed):
                    atomic_json(self.path / 'runner-process.json', {**process, **changed})
                    self.supervisor.tick(self.runner)
                    self.assertEqual(read_json(self.path / 'recovery.json'), incident)
            atomic_json(self.path / 'runner-process.json', process)
            atomic_json(self.path / 'operations.json', {'effect': {'status': 'unknown'}})
            self.supervisor.tick(self.runner)
            self.assertNotEqual(read_json(self.path / 'recovery.json')['status'], 'observing')
            atomic_json(self.path / 'operations.json', {})
            atomic_json(self.path / 'recovery.json', incident)
            self.supervisor.tick(self.runner)
            journal = read_json(self.path / 'recovery.json')
        self.assertEqual(journal['status'], 'observing')
        self.assertEqual(journal['mode'], 'live')
        self.assertEqual(journal['resolved_owner_nonce'], 'nonce')
        self.assertEqual(journal['checks'], 6)
        self.assertFalse((self.path / 'commands').exists())
        self.start.assert_not_called()

    def test_live_enqueue_crash_uses_same_uuid(self):
        from team_runner.runner_store import lock
        self.runner.launcher.registry.return_value = {'agents': [{'status': 'ready'}], 'runs': []}
        with lock(self.path / 'runner.lock'):
            self.supervisor.tick(self.runner)
            self.now += 200
            with patch('team_runner.recovery.enqueue', side_effect=OSError('crash')):
                with self.assertRaises(OSError): self.supervisor.tick(self.runner)
            command_id = read_json(self.path / 'recovery.json')['command_id']
            self.now += 200
            self.supervisor.tick(self.runner)
            self.assertTrue((self.path / 'commands' / (command_id + '.json')).exists())

    def test_crash_after_retry_state_save_does_not_retry_twice(self):
        old = recovery.target(self.state)
        atomic_json(self.path / 'recovery.json', {'target': old, 'checks': 1, 'status': 'recovering',
                    'coordinator_retry': True, 'action': 'retry with saved snapshot'})
        self.state['current'] = {'item': 'one', 'attempt': 2, 'retry_config': {'saved': True},
                                 'retry_context_from': old}
        self.check()
        self.runner.apply_control.assert_not_called()
        self.assertEqual(self.state['current']['attempt'], 2)
        self.start.assert_called_once()

    def test_interrupted_final_check_is_not_repeated(self):
        atomic_json(self.path / 'recovery.json', {'target': recovery.target(self.state),
                    'checks': 6, 'status': 'recovering', 'next_check_at': None})
        self.supervisor.tick(self.runner)
        journal = read_json(self.path / 'recovery.json')
        self.assertEqual(journal['status'], 'needs_action')
        self.assertEqual(journal['checks'], 6)
        self.runner.recover.assert_not_called()

    def test_live_runner_lock_prevents_recovery(self):
        from team_runner.runner_store import lock
        with lock(self.path / 'runner.lock'):
            self.check()
        self.runner.recover.assert_not_called()

    def test_disabled_setting_fences_callback(self):
        self.runner.recover.side_effect = lambda *a, **k: recovery.configure(self.runner, False)
        self.check()
        permitted = self.runner.cleanup_accepted_reports.call_args.kwargs['permitted']
        self.assertFalse(permitted())
        self.start.assert_not_called()

    def test_unconfirmed_stop_remains_visible(self):
        recovery.set_intent(self.runner, 'stopped')
        atomic_json(self.path / 'recovery.json', {'status': 'needs_action', 'reason': 'stop unknown'})
        view = recovery.recovery_view(self.runner)
        self.assertEqual(view['status'], 'needs_action')
        self.assertEqual(view['reason'], 'stop unknown')

    def test_confirmed_stop_supersedes_historical_incident(self):
        recovery.set_intent(self.runner, 'stopped')
        self.state['stop_status'] = 'stopped'
        incident = {'status': 'needs_action', 'reason': 'stop unknown', 'checks': 6}
        atomic_json(self.path / 'recovery.json', incident)
        view = recovery.recovery_view(self.runner)
        self.assertEqual(view['status'], 'observing')
        self.assertEqual(view['next_step'], 'stopped')
        self.assertIsNone(view['reason'])
        self.assertEqual(read_json(self.path / 'recovery.json'), incident)

    def test_pause_after_confirmed_stop_does_not_restore_old_failure(self):
        recovery.set_intent(self.runner, 'paused')
        self.state['stop_status'] = 'stopped'
        atomic_json(self.path / 'recovery.json', {'reason': 'old terminal mismatch',
                    'action': 'clean up accepted reports', 'status': 'needs_action'})
        view = recovery.recovery_view(self.runner)
        self.assertIsNone(view['reason'])
        self.assertEqual(view['action'], 'stop confirmed')
        self.state['current']['attempt'] = 3
        view = recovery.recovery_view(self.runner)
        self.assertEqual(view['status'], 'needs_action')
        self.assertIn('трёх попыток', view['reason'])
        self.assertNotIn('mismatch', view['reason'])

    def test_transport_timeout_consumes_durable_check(self):
        self.runner.recover.side_effect = subprocess.TimeoutExpired('fixture', 1)
        journal = self.check()
        self.assertEqual(journal['checks'], 1)
        self.assertEqual(journal['status'], 'waiting')
        self.assertEqual(journal['next_check_at'], self.now + 5)
        self.start.assert_not_called()

    def test_foreign_process_record_never_recovers(self):
        atomic_json(self.path / 'runner-process.json', {'pid': 123, 'process_stamp': 'fixture',
                    'nonce': 'other', 'identity': {'workspace': 'foreign'}})
        journal = self.check()
        self.assertIn('identity unconfirmed', journal['reason'])
        self.runner.recover.assert_not_called()

    def test_unknown_effect_never_replays_or_resumes(self):
        atomic_json(self.path / 'operations.json', {'unknown': {'status': 'pending'}})
        self.check()
        self.runner.launcher.resume_ready_launches.assert_not_called()
        self.start.assert_not_called()

    def test_cleanup_honors_pause_arriving_during_reconciliation(self):
        self.runner.recover.side_effect = lambda *a, **k: recovery.set_intent(self.runner, 'paused')
        self.check()
        permitted = self.runner.cleanup_accepted_reports.call_args.kwargs['permitted']
        self.assertFalse(permitted())
        self.start.assert_not_called()

    def test_pause_and_stop_event_fence_actions(self):
        recovery.set_intent(self.runner, 'paused')
        self.check()
        self.runner.recover.assert_not_called()
        recovery.set_intent(self.runner, 'running')
        self.supervisor.stop_event.set()
        self.check()
        self.runner.recover.assert_not_called()

    def test_coordinator_retry_requires_stop_and_keeps_snapshot(self):
        self.runner.launcher.liveness.return_value = 'exited'
        def control(st, action, data):
            if action == 'stop': st['stop_status'] = 'stopped'
            if action == 'retry': st['current'] = {'item': 'one', 'attempt': 2}
        self.runner.apply_control.side_effect = control
        self.check()
        self.assertEqual([call.args[1:] for call in self.runner.apply_control.call_args_list], [('stop', {}), ('retry', {})])
        self.start.assert_called_once()

    def test_missing_coordinator_proof_allows_bounded_retry(self):
        self.runner.launcher.liveness.return_value = 'unverifiable'
        self.runner.launcher.registry.return_value = {'agents': [
            {'dispatch_id': 'dispatch', 'status': 'stopped', 'terminal_closed': True,
             'close_evidence': {'reason': 'exact launch process departed; complete local terminal list absent'}}]}
        def control(st, action, data):
            if action == 'stop': st['stop_status'] = 'stopped'
            if action == 'retry': st['current'] = {'item': 'one', 'attempt': 2}
        self.runner.apply_control.side_effect = control
        self.check()
        self.assertEqual([call.args[1] for call in self.runner.apply_control.call_args_list], ['stop', 'retry'])
        self.start.assert_called_once()

    def test_three_attempt_limit(self):
        self.state['current']['attempt'] = 3
        self.runner.launcher.liveness.return_value = 'exited'
        self.check()
        self.runner.apply_control.assert_not_called()
        self.start.assert_not_called()
        journal = read_json(self.path / 'recovery.json')
        self.assertEqual(journal['status'], 'needs_action')
        self.assertIsNone(journal['next_check_at'])
        self.assertEqual(journal['action'], 'review attempt limit')
        self.assertIn('three attempts exhausted', journal['reason'])
        self.check()
        self.runner.apply_control.assert_not_called()
        self.start.assert_not_called()

    def test_crash_after_own_stop_continues_retry(self):
        self.state.update(stop_requested=True, stop_status='stopped')
        atomic_json(self.path / 'recovery.json', {'incident_id': 'incident', 'target': recovery.target(self.state),
                    'checks': 1, 'coordinator_retry': True, 'action': 'retry with saved snapshot', 'next_check_at': 0})
        self.runner.launcher.liveness.return_value = 'unknown'
        self.check()
        self.assertIn('retry', [call.args[1] for call in self.runner.apply_control.call_args_list])


class RealRunnerRecovery(unittest.TestCase):
    def setUp(self):
        from memory_fixtures import isolated_admission
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        isolated_admission(self, directory.name)

    def test_live_owner_recovers_ready_terminal_without_second_creation(self):
        from team_runner.catalog import Catalog
        from team_runner.engine import Runner
        from team_runner.runner_store import lock
        from runner_browser_fixture import OrcaFixture, prepare_checkout
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            prepare_checkout(repo)
            catalog = Catalog(root / 'config', root / 'state')
            project = catalog.project('Live fixture')['id']
            workspace = catalog.workspace(project, {'path': str(repo), 'orca_id': 'fixture', 'orca_instance': 'instance'})['id']
            runner = Runner(catalog.context(project, workspace))
            fixture = OrcaFixture(repo / '.transport', repo)
            runner.launcher.orca = fixture
            attempt = runner.state_dir / 'attempts' / 'live'
            runner.context.snapshot(attempt)
            atomic_json(attempt / 'config.json', runner.load_config())
            runner.save_state({'history': [], 'runner_status': 'running', 'run_id': 'local-run',
                               'current': {'item': 'T1', 'attempt_dir': str(attempt), 'attempt': 1}})
            task = fixture('orchestration', 'task-create', '--run', 'local-run', '--spec', 'fixture', '--task-title', 'fixture')['task']
            def fail_wait(*args, **kwargs):
                if args[:2] == ('terminal', 'wait'):
                    raise RuntimeError('terminal_handle_stale')
                return fixture(*args, **kwargs)
            with patch.object(runner.launcher, 'orca', side_effect=fail_wait):
                with self.assertRaisesRegex(RuntimeError, 'terminal_handle_stale'):
                    runner.launcher.launch(attempt, 'coordinator', task['id'], 'local-run', 'fixture', runner.state_dir)
            before = read_json(fixture.root / 'transport.json')['serial']
            recovery.configure(runner, True)
            recovery.set_intent(runner, 'running')
            atomic_json(runner.state_dir / 'runner-process.json', {'recovery_protocol': 1})
            now = [100]
            supervisor = recovery.Supervisor(catalog, project, clock=lambda: now[0])
            with lock(runner.state_dir / 'runner.lock'):
                supervisor.tick(runner)
                now[0] += 2
                supervisor.tick(runner)
                runner.process_controls(runner.load_state())
                supervisor.tick(runner)
            registry = runner.launcher.registry(attempt)
            self.assertEqual(registry['agents'][0]['status'], 'active')
            self.assertTrue(registry['agents'][0]['dispatch_id'])
            self.assertEqual(read_json(fixture.root / 'transport.json')['serial'], before)
            self.assertEqual(len(read_json(fixture.root / 'transport.json')['workers']), 1)
            self.assertEqual(read_json(runner.state_dir / 'recovery.json')['status'], 'observing')

    def test_pause_after_launch_prevents_runner_entry(self):
        from team_runner.catalog import Catalog
        from team_runner.engine import Runner
        from team_runner.service import run_process
        from runner_browser_fixture import OrcaFixture, prepare_checkout
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            prepare_checkout(repo)
            catalog = Catalog(root / 'config', root / 'state')
            project = catalog.project('Pause fixture')['id']
            workspace = catalog.workspace(project, {'path': str(repo), 'orca_id': 'fixture', 'orca_instance': 'instance'})['id']
            runner = Runner(catalog.context(project, workspace))
            runner.launcher.orca = OrcaFixture(repo / '.transport', repo)
            atomic_json(runner.state_dir / 'launch.json', {'nonce': 'nonce', 'identity': runner.context.identity(),
                        'options': {'goal': 'feat/goal-fixture'}})
            recovery.set_intent(runner, 'paused')
            with patch.object(runner, 'cmd_run') as run:
                self.assertEqual(run_process(runner, 'nonce'), 0)
            run.assert_not_called()
            self.assertEqual(recovery.settings(runner)['intent'], 'paused')

    def test_exited_coordinator_retries_saved_snapshot_and_settles_startup(self):
        from team_runner.catalog import Catalog
        from team_runner.engine import Runner
        from runner_browser_fixture import OrcaFixture, prepare_checkout
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            prepare_checkout(repo)
            catalog = Catalog(root / 'config', root / 'state')
            project = catalog.project('Recovery fixture')['id']
            workspace = catalog.workspace(project, {'path': str(repo), 'orca_id': 'fixture', 'orca_instance': 'instance'})['id']
            runner = Runner(catalog.context(project, workspace))
            transport_fixture = OrcaFixture(repo / '.transport', repo)
            def transport_call(*args, **kwargs):
                result = transport_fixture(*args, **kwargs)
                if args[:2] == ('orchestration', 'worker-show') and result.get('observation', {}).get('status') == 'exited':
                    result['dispatch']['status'] = 'failed'
                    result['observation']['exactWorker'] = True
                    result['worker'] = {'stage': 'process_exited', 'worktreeId': 'fixture'}
                return result
            runner.launcher.orca = transport_call
            attempt = runner.state_dir / 'attempts' / 'original'
            runner.context.snapshot(attempt)
            snapshot = runner.load_config()
            atomic_json(attempt / 'config.json', snapshot)
            runner.save_state({'history': [], 'run_id': 'local-run', 'current': {'item': 'T1', 'attempt_dir': str(attempt), 'attempt': 1}})
            task = runner.launcher.orca('orchestration', 'task-create', '--run', 'local-run', '--spec', 'fixture', '--task-title', 'fixture')['task']
            record = runner.launcher.launch(attempt, 'coordinator', task['id'], 'local-run', 'fixture', runner.state_dir)
            runner.save_state({'history': [], 'runner_status': 'running', 'run_id': 'local-run',
                               'goal': 'feat/goal-fixture', 'options': {'goal': 'feat/goal-fixture'},
                               'current': {'item': 'T1', 'attempt_dir': str(attempt), 'attempt': 1,
                                           'dispatch_id': record['dispatch_id']}})
            transport = read_json(transport_fixture.root / 'transport.json')
            transport['workers'][record['dispatch_id']]['status'] = 'exited'
            atomic_json(transport_fixture.root / 'transport.json', transport)
            recovery.configure(runner, True)
            recovery.set_intent(runner, 'running')
            identity = runner.context.identity()
            atomic_json(runner.state_dir / 'launch.json', {'nonce': 'nonce', 'identity': identity})
            atomic_json(runner.state_dir / 'runner-process.json', {'nonce': 'nonce', 'identity': identity, 'pid': 123, 'process_stamp': 'fixture'})
            atomic_json(runner.state_dir / 'startup.json', {'status': 'unknown'})
            now = [100]
            supervisor = recovery.Supervisor(catalog, project, clock=lambda: now[0])
            with patch('team_runner.project_services.departed', return_value=True), patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start:
                supervisor.tick(runner)
                now[0] += 2
                supervisor.tick(runner)
            journal = read_json(runner.state_dir / 'recovery.json')
            self.assertEqual(journal['status'], 'observing', journal)
            state = runner.load_state()
            self.assertEqual(state['current']['attempt'], 2)
            self.assertEqual(state['current']['retry_config'], snapshot)
            self.assertEqual(state['current']['retry_context_from'], str(attempt))
            self.assertEqual(state['runner_status'], 'idle')
            self.assertEqual(read_json(runner.state_dir / 'startup.json')['status'], 'idle')
            start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
