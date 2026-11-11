"""Explicit cleanup needs exact ownership; UI retry uses saved role settings."""
import copy
from unittest.mock import patch
from test_goal_runner import Fixture, runner, launch, store, config


class StopRefresh(Fixture):
    def test_conflicting_command_uuid_does_not_change_recovery_intent(self):
        from team_runner.recovery import set_intent, settings
        set_intent(runner, 'running')
        command = store.enqueue(runner.state_dir, 'reconcile')
        with self.assertRaisesRegex(ValueError, 'already used'):
            runner.submit_control('pause', request_id=command['id'])
        self.assertEqual(settings(runner)['intent'], 'running')

    def test_new_completed_work_requires_another_final_review(self):
        from types import SimpleNamespace
        store.atomic_json(runner.repo / 'feat/goal/Tasks.json', {'kind': 'goal', 'queue': []})
        args = SimpleNamespace(goal='feat/goal', config=None, mode='auto', items=None, final=True,
                               **{k: None for k in config.FIELDS})
        for later_work in (False, True):
            with self.subTest(later_work=later_work):
                history = [{'item': 'GOAL-FINAL-REVIEW', 'problems': []}]
                if later_work: history.append({'item': 'new-feature/step-1', 'problems': []})
                runner.save_state({'history': history, 'current': None})
                with patch.object(runner, 'ensure_run', return_value='run'), \
                        patch.object(runner, 'process_controls'), patch.object(runner, 'notify'), \
                        patch.object(runner, 'final_spec', return_value='review'), \
                        patch.object(runner, 'start_coordinator', return_value='paused_gate') as start:
                    runner.cmd_run(args)
                self.assertEqual(start.call_count, int(later_work))
                if later_work: self.assertEqual(start.call_args.args[2], 'GOAL-FINAL-REVIEW')

    def test_operator_closed_terminal_is_reconciled_without_mutations(self):
        for mutation in (None, 'owner', 'process', 'receipt', 'terminal', 'present', 'missing-list'):
            with self.subTest(mutation=mutation):
                attempt, st = self.attempt()
                agent = dict(id='a', role='coordinator', dispatch_id='owned', status='unknown',
                             terminal='t', incarnation='i', terminal_owned=True, error='old error')
                launch.save_registry(attempt, {'agents': [agent], 'runs': [], 'stop_requested': True})
                terminal = {'handle': 't', 'incarnationId': 'i', 'ptyId': 'p', 'worktreeId': 'fixture'}
                store.atomic_json(launch.journal(attempt), {'a-terminal': {
                    'status': 'completed', 'result': {'terminal': terminal}}})
                data = {'dispatch': {'id': 'owned', 'status': 'failed', 'terminationReason': 'operator_close',
                                     'assigneeHandle': 't', 'processIncarnation': 'p:i'},
                        'worker': {'stage': 'process_exited', 'worktreeId': 'fixture'},
                        'projection': {'liveness': {'verdict': 'exited'}}, 'terminal': None,
                        'terminalResource': {'ownershipState': 'external', 'ownerDispatchId': 'owned',
                            'terminalHandle': 't', 'worktreeId': 'fixture', 'endpointIncarnation': 'p:i'}}
                if mutation == 'owner': data['terminalResource']['ownerDispatchId'] = 'foreign'
                if mutation == 'process': data['dispatch']['processIncarnation'] = 'p:foreign'
                if mutation == 'receipt': store.atomic_json(launch.journal(attempt), {})
                if mutation == 'terminal': data['terminal'] = terminal
                def orca(*args, **kwargs):
                    if args[:2] == ('terminal', 'list'):
                        return {} if mutation == 'missing-list' else {'terminals': [terminal] if mutation == 'present' else []}
                    return data
                with patch.object(launch, 'orca', side_effect=orca), patch.object(launch, 'effect') as effect:
                    self.assertEqual(launch.confirm_operator_closed(agent, data, attempt), mutation in (None, 'terminal', 'present'))
                    if mutation in (None, 'terminal', 'present'):
                        self.assertEqual(launch.reconcile_attempt(attempt), [])
                        recovered = launch.registry(attempt)['agents'][0]
                        self.assertEqual(recovered['status'], 'stopped')
                        self.assertTrue(recovered['terminal_closed'])
                        self.assertNotIn('error', recovered)
                        self.assertTrue(launch.stop_attempt(attempt))
                    effect.assert_not_called()

    def test_restored_external_worker_stop_requires_exact_live_identity(self):
        for mutation in (None, 'owner', 'incarnation', 'worktree', 'observation', 'dispatch'):
            with self.subTest(mutation=mutation):
                attempt, _ = self.attempt()
                agent = dict(id='a', role='coordinator', dispatch_id='owned', status='unknown',
                             terminal='t', incarnation='i', terminal_owned=True)
                launch.save_registry(attempt, {'agents': [agent], 'runs': []})
                data = {'dispatch': {'status': 'dispatched'},
                        'observation': {'status': 'live', 'exactWorker': True},
                        'terminal': {'handle': 't', 'incarnationId': 'i', 'worktreeId': 'fixture'},
                        'terminalResource': {'ownershipState': 'external', 'ownerDispatchId': 'owned'}}
                if mutation == 'owner': data['terminalResource']['ownerDispatchId'] = 'foreign'
                if mutation == 'incarnation': data['terminal']['incarnationId'] = 'foreign'
                if mutation == 'worktree': data['terminal']['worktreeId'] = 'foreign'
                if mutation == 'observation': data['observation']['exactWorker'] = False
                if mutation == 'dispatch': data['dispatch']['status'] = 'starting'
                def orca(*args, **kwargs):
                    return {'terminals': []} if args[:2] == ('terminal', 'list') else data
                with patch.object(launch, 'liveness', return_value='unverifiable'), \
                        patch.object(launch, 'orca', side_effect=orca), patch.object(launch, 'effect') as effect:
                    self.assertEqual(launch.stop_attempt(attempt), mutation is None)
                    if mutation is None:
                        self.assertEqual([c.args[2:4] for c in effect.call_args_list],
                                         [('orchestration', 'worker-abandon'), ('terminal', 'close')])
                    else:
                        effect.assert_not_called()

    def test_unconfirmed_stop_returns_error_and_confirmed_stop_clears_it(self):
        from team_runner.runner_ui import state_view
        attempt, st = self.attempt()
        launch.save_registry(attempt, {'agents': [{'role': 'coordinator', 'dispatch_id': 'owned',
                             'status': 'unknown', 'error': 'liveness unconfirmed'}], 'runs': []})
        st['last_error'] = 'old runtime failure'
        runner.save_state(st)
        with patch.object(launch, 'stop_attempt', return_value=False):
            result = runner.submit_control('stop')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['result'], {'state': 'error', 'error': 'coordinator: liveness unconfirmed'})
        self.assertEqual(state_view(runner)['health'], result['result'])
        with patch.object(launch, 'stop_attempt', return_value=True):
            result = runner.submit_control('stop')
        self.assertEqual(result['result'], {'state': 'ok', 'error': None})
        self.assertEqual(runner.load_state()['stop_status'], 'stopped')
        self.assertEqual(state_view(runner)['health'], result['result'])

    def test_task_not_startable_is_rejected_without_replay(self):
        from team_runner import runner_operations as ops
        path = self.root / 'rejected.json'
        store.atomic_json(path, {'start': {'args': ['orchestration', 'worker-start'], 'status': 'unknown',
                           'failure_receipt': {'ok': False, 'error': {'code': 'task_not_startable'}}}})
        with patch.object(launch, 'orca') as call:
            self.assertEqual(ops.reconcile(path, call), [])
            call.assert_not_called()
        self.assertEqual(store.read_json(path)['start']['status'], 'rejected')

    def test_cleanup_exited_external_worker_without_status_telemetry(self):
        for mutation in (None, 'owner', 'incarnation', 'worktree', 'observation', 'dispatch'):
            with self.subTest(mutation=mutation):
                attempt, _ = self.attempt()
                agent = dict(id='a', role='tester', dispatch_id='owned', status='unknown',
                             terminal='t', incarnation='i', terminal_owned=True)
                launch.save_registry(attempt, {'fenced': True, 'runs': [], 'agents': [agent]})
                data = {'dispatch': {'status': 'failed'}, 'observation': {'status': 'exited', 'exactWorker': True},
                        'terminal': {'handle': 't', 'incarnationId': 'i', 'worktreeId': runner.context.orca_id},
                        'terminalResource': {'ownershipState': 'external', 'ownerDispatchId': 'owned'}}
                if mutation == 'owner': data['terminalResource']['ownerDispatchId'] = 'foreign'
                if mutation == 'incarnation': data['terminal']['incarnationId'] = 'foreign'
                if mutation == 'worktree': data['terminal']['worktreeId'] = 'foreign'
                if mutation == 'observation': data['observation']['exactWorker'] = False
                if mutation == 'dispatch': data['dispatch']['status'] = 'dispatched'
                def orca(*args, **kwargs):
                    return {'terminals': []} if args[:2] == ('terminal', 'list') else data
                with patch.object(launch, 'liveness', return_value='unverifiable'), \
                        patch.object(launch, 'orca', side_effect=orca), patch.object(launch, 'effect') as effect:
                    self.assertEqual(launch.stop_attempt(attempt), mutation is None)
                    if mutation is None:
                        self.assertEqual(effect.call_args.args[2:4], ('terminal', 'close'))
                        self.assertTrue(launch.registry(attempt)['agents'][0]['terminal_closed'])
                    else:
                        effect.assert_not_called()

    def test_ui_refresh_clears_old_overrides_cli_refresh_preserves_them(self):
        for saved_settings, versioned in ((False, False), (True, False), (False, True), (True, True)):
            attempt, state = self.attempt()
            before = (attempt / 'config.json').read_bytes()
            saved = config.defaults()
            saved['roles']['coordinator'] = copy.deepcopy(saved['roles']['planner'])
            store.atomic_json(runner.context.settings_dir / 'config.json', saved)
            state.update(stop_status='stopped', options={'agent': 'claude', 'model': 'old-model', 'mode': 'auto'})
            payload = {'refresh': True, 'saved_settings': saved_settings}
            if versioned:
                import uuid
                from team_runner import application
                runner.save_state(state)
                view = application.summary(runner)
                result = application.submit_v1(runner, {'schema_version': 1, 'request_id': str(uuid.uuid4()),
                    'action': 'retry', 'payload': payload, 'scope': {'identity': view['identity'],
                    'generation': view['generation'], 'attempt': view['current']['attempt'], 'revision': view['revision']}})
                self.assertEqual(result['status'], 'succeeded', result)
                state = runner.load_state()
            else:
                runner.apply_control(state, 'retry', payload)
            actual = state['current']['retry_config']['roles']['coordinator']
            self.assertEqual(actual['model'], saved['roles']['coordinator']['model'] if saved_settings else 'old-model')
            self.assertEqual(state['options']['mode'], 'auto')
            self.assertEqual('agent' in state['options'], not saved_settings)
            self.assertEqual((attempt / 'config.json').read_bytes(), before)

    def test_linux_screen_reader_is_never_selected_as_orca_cli(self):
        import os
        from team_runner.orca import executable
        with patch.dict(os.environ, {'ORCA_TERMINAL_HANDLE': 'terminal'}, clear=True), \
                patch('team_runner.orca.sys.platform', 'linux'), \
                patch('team_runner.orca.shutil.which', return_value='/usr/bin/orca'):
            self.assertEqual(executable(), 'orca-ide')
            with patch.dict(os.environ, {'ORCA_CLI_COMMAND': '/custom/orca'}):
                self.assertEqual(executable(), '/custom/orca')

    def test_retry_creates_fresh_task_without_orca_retry_of(self):
        from types import SimpleNamespace
        attempt, state = self.attempt()
        state.update(stop_status='stopped', mailbox='main')
        runner.apply_control(state, 'retry', {})
        self.assertNotIn('task_id', state['current'])
        args = SimpleNamespace(mode='auto', config=None)
        with patch.object(runner, 'effect', return_value={'task': {'id': 'fresh-task'}}) as create, \
                patch.object(launch, 'launch', return_value={'dispatch_id': 'fresh-dispatch'}) as start, \
                patch.object(runner, 'process_controls'), patch.object(runner, 'notify'):
            runner.start_coordinator(args, state, 'X', 'spec', 'title')
        self.assertIn('task-create', create.call_args.args)
        self.assertEqual(start.call_args.args[2], 'fresh-task')
        self.assertNotIn('retry_of', start.call_args.kwargs)
        self.assertEqual(state['attempts'][-1]['task_id'], 'task')
        self.assertEqual(state['current']['previous_dispatch'], 'owned')

    def test_settled_live_external_stop_with_stale_telemetry(self):
        for outcome in ('completed', 'failed'):
            for mutation in (None, 'owner', 'incarnation', 'worktree', 'observation', 'managed'):
                with self.subTest(outcome=outcome, mutation=mutation):
                    attempt, _ = self.attempt()
                    agent = dict(id='a', role='implementer_complex', dispatch_id='owned',
                                 status='unknown', terminal='t', incarnation='i', terminal_owned=True)
                    launch.save_registry(attempt, {'agents': [agent], 'runs': []})
                    data = {'dispatch': {'status': outcome},
                            'observation': {'status': 'live', 'exactWorker': True},
                            'terminal': {'handle': 't', 'incarnationId': 'i', 'worktreeId': 'fixture'},
                            'terminalResource': {'ownershipState': 'external', 'ownerDispatchId': 'owned'}}
                    if mutation == 'owner': data['terminalResource']['ownerDispatchId'] = 'foreign'
                    if mutation == 'incarnation': data['terminal']['incarnationId'] = 'foreign'
                    if mutation == 'worktree': data['terminal']['worktreeId'] = 'foreign'
                    if mutation == 'observation': data['observation']['exactWorker'] = False
                    if mutation == 'managed': data['terminalResource']['ownershipState'] = 'owned'
                    def orca(*args, **kwargs):
                        return {'terminals': []} if args[:2] == ('terminal', 'list') else data
                    with patch.object(launch, 'liveness', return_value='unverifiable'), \
                            patch.object(launch, 'orca', side_effect=orca), patch.object(launch, 'effect') as effect:
                        self.assertEqual(launch.stop_attempt(attempt), mutation is None)
                        if mutation is None:
                            self.assertEqual([c.args[2:4] for c in effect.call_args_list], [('terminal', 'close')])
                            self.assertEqual(launch.registry(attempt)['agents'][0]['status'], 'stopped')
                        else:
                            effect.assert_not_called()

