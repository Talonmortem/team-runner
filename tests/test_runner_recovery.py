"""Process-death regressions using durable receipts; no live Orca or providers."""
import copy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_goal_runner import Fixture, runner, launch, config, store
from team_runner import runner_operations as ops
from team_runner.runner_entry import process_stamp
from runner_browser_fixture import OrcaFixture


class Recovery(Fixture):
    def crash(self, stage, work):
        pid = os.fork()
        if pid == 0:
            os.environ['TEAM_RUNNER_CRASH_AT'] = stage
            try:
                work()
            except BaseException:
                os._exit(99)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 86, stage)

    def test_effect_crash_boundaries_and_absent_never_replayed(self):
        for verb in ('worker-start', 'worker-stop', 'worker-abandon', 'worker-release', 'reply', 'check'):
            for stage in ('before_effect', 'after_effect', 'after_receipt'):
                with self.subTest(verb=verb, stage=stage):
                    path = self.root / (verb + stage + '.json')
                    receipt_path = self.root / (verb + stage + '-remote.json')
                    def call(*args, **kw):
                        if args[1] == 'request-show':
                            value = store.read_json(receipt_path)
                            return {'state': 'completed', 'receipt': value} if value else {'state': 'absent'}
                        if receipt_path.exists():
                            raise AssertionError('duplicate external effect')
                        value = {'dispatchId': 'owned', 'mutation': {'requestId': args[-1]}}
                        store.atomic_json(receipt_path, value)
                        return value
                    self.crash(verb + ':' + stage, lambda: ops.mutation(path, 'x', call, 'orchestration', verb))
                    issues = ops.reconcile(path, call)
                    if stage == 'before_effect':
                        self.assertTrue(issues)
                        with self.assertRaises(ops.UncertainOperation):
                            ops.continue_operation(path, store.read_json(path)['x']['request_id'], call)
                        self.assertFalse(receipt_path.exists())
                    else:
                        self.assertFalse(issues)
                        self.assertEqual(ops.mutation(path, 'x', call, 'orchestration', verb)['dispatchId'], 'owned')

    def test_pending_continuation_preserves_request_and_excludes_live_call(self):
        path = self.root / 'ops.json'
        with self.assertRaises(ops.UncertainOperation):
            ops.mutation(path, 'x', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('lost')),
                         'orchestration', 'reply', '--id', 'q')
        request = store.read_json(path)['x']['request_id']
        calls = []
        def call(*args, **kw):
            calls.append(args)
            return {'state': 'pending'} if args[1] == 'request-show' else {'accepted': True}
        with store.lock(str(path) + '.' + request + '.lock'):
            with self.assertRaises(RuntimeError):
                ops.continue_operation(path, request, call)
        self.assertFalse(calls)
        ops.continue_operation(path, request, call)
        self.assertEqual(calls[-1][-2:], ('--retry-request', request))

    def test_terminal_creation_marker_and_close_reconcile(self):
        for verb in ('create', 'close'):
            for stage in ('before_effect', 'after_effect', 'after_receipt'):
                path = self.root / (verb + stage + '.json')
                marker = self.root / (verb + stage + '-marker.json')
                terminal = {'handle':'own', 'incarnationId':'inc', 'ptyId':'pty', 'worktreeId':'work'}
                parent = os.getpid()
                effect_file = self.root / (verb + stage + '-effect')
                def call(*args, **kw):
                    if args[1] == 'show':
                        return {'terminal': terminal}
                    if args[1] == 'list':
                        return {'terminals': [] if effect_file.exists() else [terminal]}
                    effect_file.touch()
                    store.atomic_json(marker, {'nonce':'once','terminal':terminal,'pid':parent,
                                               'process_stamp':process_stamp(parent)})
                    return {'terminal':terminal}
                evidence = {'marker':str(marker),'nonce':'once','terminal':'own','incarnation':'inc'}
                self.crash(verb + ':' + stage, lambda: ops.mutation(path, 'x', call, 'terminal', verb, evidence=evidence))
                issues = ops.reconcile(path, call)
                self.assertEqual(bool(issues), stage == 'before_effect')
                if verb == 'create' and stage == 'after_effect':
                    op = store.read_json(path)['x']; terminal['incarnationId'] = 'foreign'
                    with self.assertRaises(ops.UncertainOperation):
                        ops.terminal_evidence(op, call)

    def test_command_state_receipt_atomic_across_retry_crash(self):
        attempt, state = self.attempt()
        state['stop_status'] = 'stopped'; runner.save_state(state)
        rec = store.enqueue(self.root, 'retry')
        self.crash('command:after_save', lambda: runner.process_controls(runner.load_state()))
        restored = runner.load_state()
        self.assertEqual(restored['current']['attempt'], 2)
        runner.process_controls(restored)
        self.assertEqual(runner.load_state()['current']['attempt'], 2)
        self.assertEqual(store.read_json(self.root / 'commands' / (rec['id']+'.json'))['status'], 'done')

    def test_message_crash_never_acks_unsaved_message(self):
        _, state = self.attempt()
        receipt = {'messages':[{'id':'m','type':'question'}]}
        for stage in ('message:before_save', 'message:after_save'):
            self.crash(stage, lambda: runner.persist_messages(runner.load_state(), receipt, 'r'))
            restored = runner.load_state()
            runner.persist_messages(restored, receipt, 'r')
            self.assertEqual(len(restored['current']['messages']), 1)

    def test_missing_legacy_registry_cannot_authorize_retry(self):
        attempt, state = self.attempt()
        with self.assertRaises(ValueError):
            runner.apply_control(state, 'stop', {})
        with patch.object(runner, 'orca') as call, patch.object(launch, 'orca') as worker:
            runner.recover(state)
        self.assertEqual(state['stop_status'], 'unknown')
        self.assertTrue(state['recovery_issues'])
        call.assert_not_called(); worker.assert_not_called()
        with self.assertRaises(ValueError):
            runner.apply_control(state, 'retry', {})

    def test_stop_crash_after_fence_still_closes_external_terminal(self):
        attempt, state = self.attempt()
        fake = OrcaFixture(self.root / 'transport', runner.repo)
        with patch.object(launch, 'orca', side_effect=fake):
            record = launch.dispatch_role(attempt, 'tester', 'first', 'T1', self.root, function='f')
            self.crash('worker-abandon:after_receipt', lambda: launch.stop_attempt(attempt))
            self.assertIn(record['terminal'], store.read_json(fake.root / 'transport.json')['terminals'])
            self.assertFalse(launch.reconcile_attempt(attempt))
            self.assertTrue(launch.stop_attempt(attempt))
            self.assertFalse(store.read_json(fake.root / 'transport.json')['terminals'])

    def test_reconcile_never_performs_external_mutations(self):
        attempt, state = self.attempt()
        state['current'].pop('dispatch_id')
        state['stop_requested'] = True
        runner.save_state(state)
        record = store.enqueue(self.root, 'stop')
        record['status'] = 'applying'
        store.atomic_json(self.root / 'commands' / (record['id'] + '.json'), record)
        runner.process_controls(state)
        with patch.object(runner, 'orca') as remote, patch.object(launch, 'orca') as worker:
            runner.recover(state)
        remote.assert_not_called()
        worker.assert_not_called()
        self.assertEqual(state['stop_status'], 'stopped')
        self.assertNotIn('control_uncertain', state)

    def test_lost_dispatch_response_recovers_without_new_process_or_task(self):
        attempt, state = self.attempt()
        fake = OrcaFixture(self.root / 'transport', runner.repo)
        fake.root.mkdir(parents=True)
        (fake.root / 'lose-dispatch').touch()
        with patch.object(launch, 'orca', side_effect=fake):
            with self.assertRaises(ops.UncertainOperation):
                launch.dispatch_role(attempt, 'tester', 'first', 'T1', self.root, function='f')
            before = store.read_json(fake.root / 'transport.json')
            self.assertFalse(launch.reconcile_attempt(attempt))
            record = launch.dispatch_role(attempt, 'tester', 'first', 'T1', self.root, function='f')
            after = store.read_json(fake.root / 'transport.json')
        self.assertEqual(before['tasks'], after['tasks'])
        self.assertEqual(before['serial'], after['serial'])
        self.assertIn(record['dispatch_id'], before['workers'])

    def test_technical_request_reconciliation_never_replays_pending_or_absent(self):
        for remote in ('pending', 'absent', 'unknown'):
            with self.subTest(remote=remote):
                path = self.root / (remote + '.json')
                store.atomic_json(path, {'dispatch': {'request_id': 'stable-request',
                    'status': 'unknown', 'args': ['orchestration', 'worker-start']}})
                with patch.object(runner, 'orca', return_value={'state': remote}) as call:
                    self.assertTrue(ops.reconcile(path, runner.orca))
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args.args[:2], ('orchestration', 'request-show'))
                self.assertEqual(store.read_json(path)['dispatch']['status'], 'unknown')

    def test_stale_terminal_wait_continues_exact_launch_once(self):
        attempt, state = self.attempt()
        fake = OrcaFixture(self.root / 'transport', runner.repo)
        stale = RuntimeError('terminal temporarily unavailable')
        stale.receipt = {'error': {'code': 'terminal_handle_stale'}}
        first_wait = True
        def transport(*args, **kwargs):
            nonlocal first_wait
            if args[:2] == ('terminal', 'wait') and first_wait:
                first_wait = False
                raise stale
            return fake(*args, **kwargs)
        with patch.object(launch, 'orca', side_effect=transport):
            with self.assertRaisesRegex(RuntimeError, 'temporarily'):
                launch.dispatch_role(attempt, 'tester', 'first', 'T1', self.root)
            before = store.read_json(fake.root / 'transport.json')
            self.assertEqual(launch.reconcile_attempt(attempt), [])
            launch.resume_ready_launches(attempt, self.root)
            launch.resume_ready_launches(attempt, self.root)
        after = store.read_json(fake.root / 'transport.json')
        self.assertEqual(before['serial'], after['serial'])
        self.assertEqual(before['tasks'], after['tasks'])
        self.assertEqual(len(after['workers']), 1)
        self.assertEqual(launch.registry(attempt)['agents'][0]['status'], 'active')

    def test_ready_launch_rejects_foreign_incarnation_and_cancelled_intent(self):
        attempt, state = self.attempt()
        fake = OrcaFixture(self.root / 'transport', runner.repo)
        def transport(*args, **kwargs):
            if args[:2] == ('terminal', 'wait'):
                raise RuntimeError('wait interrupted')
            return fake(*args, **kwargs)
        with patch.object(launch, 'orca', side_effect=transport):
            with self.assertRaises(RuntimeError):
                launch.dispatch_role(attempt, 'tester', 'first', 'T1', self.root)
            self.assertEqual(launch.reconcile_attempt(attempt), [])
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                launch.resume_ready_launches(attempt, self.root, permitted=lambda: False)
            data = store.read_json(fake.root / 'transport.json')
            record = launch.registry(attempt)['agents'][0]
            data['terminals'][record['terminal']]['incarnationId'] = 'foreign'
            store.atomic_json(fake.root / 'transport.json', data)
            with self.assertRaisesRegex(ops.UncertainOperation, 'identity mismatch'):
                launch.resume_ready_launches(attempt, self.root)
        self.assertEqual(store.read_json(fake.root / 'transport.json')['workers'], {})

    def test_technical_recovery_does_not_schedule_acceptance_rework(self):
        attempt, state = self.attempt()
        state['current'].update(failed=True, incomplete_checks=['C12 failed'])
        with patch.object(launch, 'reconcile_attempt', return_value=[]), \
                patch.object(runner, 'child_acceptance_problems') as acceptance:
            runner.recover(state, technical_only=True)
        acceptance.assert_not_called()
        self.assertNotIn('acceptance_rework', state['current'])
        self.assertTrue(state['current']['failed'])



class Reuse(Fixture):
    def test_both_runtimes_reuse_and_stop_once(self):
        for runtime in ('codex', 'claude'):
            with self.subTest(runtime=runtime):
                attempt = self.root / 'attempts' / runtime
                runner.context.snapshot(attempt)
                runner.save_state({'current': {'attempt_dir': str(attempt)}})
                cfg = config.defaults()
                cfg['roles']['tester'] = config.load_config(self.root, overrides={'agent':runtime})['roles']['coordinator']
                store.atomic_json(attempt / 'config.json', cfg)
                fake = OrcaFixture(self.root / runtime / 'transport', runner.repo)
                with patch.object(launch, 'orca', side_effect=fake):
                    first = launch.dispatch_role(attempt, 'tester', 'first', 'T1', self.root, function='f')
                    data = store.read_json(fake.root / 'transport.json')
                    data['workers'][first['dispatch_id']]['done'] = True
                    store.atomic_json(fake.root / 'transport.json', data)
                    second = launch.dispatch_role(attempt, 'tester', 'second', 'T2', self.root,
                                                  function='f', reuse=first['dispatch_id'], context_percent=30)
                    self.assertEqual(first['terminal'], second['terminal'])
                    self.assertNotEqual(first['task_id'], second['task_id'])
                    self.assertNotEqual(first['dispatch_id'], second['dispatch_id'])
                    self.assertTrue(launch.stop_attempt(attempt))
                    self.assertTrue(launch.stop_attempt(attempt))
                self.assertEqual(launch.registry(attempt)['agents'][0]['status'], 'superseded')
                data = store.read_json(fake.root / 'transport.json')
                self.assertFalse(data['terminals'])
                self.assertEqual(data['serial'], 2)  # mailbox and one session

    def test_reuse_refusals_never_create_a_task_or_terminal(self):
        attempt, _ = self.attempt()
        fake = OrcaFixture(self.root / 'transport', runner.repo)
        with patch.object(launch, 'orca', side_effect=fake):
            first = launch.dispatch_role(attempt, 'tester', 'first', 'T1', self.root, function='f')
            data = store.read_json(fake.root / 'transport.json')
            data['workers'][first['dispatch_id']]['done'] = True
            store.atomic_json(fake.root / 'transport.json', data)
            for role, function, percent in [('tester','other',20),('implementer_standard','f',20),
                    ('final_reviewer','f',20),('tester','f',None),('tester','f',51),('tester','f',float('nan'))]:
                with self.assertRaises(ValueError):
                    launch.dispatch_role(attempt, role, 'bad', 'bad', self.root,
                                         function=function, reuse=first['dispatch_id'], context_percent=percent)
            for field in ('agent', 'profile', 'provider', 'model', 'effort'):
                cfg = store.read_json(attempt / 'config.json')
                altered = copy.deepcopy(cfg); altered['roles']['tester'][field] = 'changed'
                store.atomic_json(attempt / 'config.json', altered)
                with self.assertRaises(ValueError):
                    launch.dispatch_role(attempt, 'tester', 'bad', 'bad', self.root,
                                         function='f', reuse=first['dispatch_id'], context_percent=20)
                store.atomic_json(attempt / 'config.json', cfg)
            with patch.object(launch, 'connection_stamp', return_value='changed'), self.assertRaises(ValueError):
                launch.dispatch_role(attempt, 'tester', 'bad', 'bad', self.root,
                                     function='f', reuse=first['dispatch_id'], context_percent=20)
        after = store.read_json(fake.root / 'transport.json')
        self.assertEqual(after['tasks'], data['tasks'])
        self.assertEqual(after['serial'], data['serial'])

    def test_invalid_next_overrides_do_not_hide_active_state(self):
        attempt, state = self.attempt()
        state['options'] = {'effort': 'ultra'}  # invalid for saved Claude coordinator
        view = config.projections(self.root, state, launch.registry(attempt))['coordinator']
        self.assertIn('effort', view['error'])
        self.assertIsNone(view['next']['model'])
        self.assertEqual(view['snapshot']['agent'], 'claude')

    def test_projection_distinguishes_override_snapshot_and_unknown(self):
        attempt, state = self.attempt()
        state['options'] = {'agent':'codex', 'model':'override'}
        view = config.projections(self.root, state, launch.registry(attempt))['coordinator']
        self.assertEqual(view['next']['model'], 'override')
        self.assertEqual(view['sources']['model'], 'CLI --model')
        self.assertNotEqual(view['saved'], view['next'])
        self.assertEqual(view['snapshot']['agent'], 'claude')
        self.assertEqual(view['facts'], [])


if __name__ == '__main__':
    unittest.main()
