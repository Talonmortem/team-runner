"""Background polling observes detached panes without changing Orca focus."""
import copy
from unittest.mock import patch

from test_goal_runner import Fixture, launch, runner, store


class TerminalPanels(Fixture):
    def setUp(self):
        super().setUp()
        self.attempt_dir, self.state = self.attempt()
        self.record = {'id': 'agent', 'status': 'active', 'dispatch_id': 'dispatch',
                       'terminal': 'term', 'terminal_owned': True, 'incarnation': 'inc',
                       'mailbox': 'mailbox', 'attempt': str(self.attempt_dir)}
        self.terminal = {'handle': 'term', 'incarnationId': 'inc', 'worktreeId': 'fixture',
                         'orphaned': True, 'connected': True, 'writable': True}
        self.data = {'projection': {'liveness': {'verdict': 'live'}},
                     'terminalResource': {'ownerDispatchId': 'dispatch'}}
        self.calls = []
        self.switch_result = 'attached'
        self.reg = {'agents': [self.record], 'runs': [], 'fenced': False}
        launch.save_registry(self.attempt_dir, self.reg)

    def transport(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.assertEqual(kwargs.get('mailbox'), 'mailbox')
        if args[:2] == ('orchestration', 'worker-show'):
            return {**copy.deepcopy(self.data), 'terminal': copy.deepcopy(self.terminal)}
        if args[:2] == ('terminal', 'show'):
            return {'terminal': copy.deepcopy(self.terminal)}
        if args == ('terminal', 'switch', '--terminal', 'term'):
            if self.switch_result != 'detached':
                self.terminal['orphaned'] = False
            if self.switch_result == 'lost':
                raise RuntimeError('lost switch receipt')
            return {'focus': {'handle': 'term', 'navigated': True}}
        raise AssertionError(f'unexpected operation: {args}')

    def poll(self):
        self.state['current'].pop('output_polled_at', None)
        with patch.object(launch, 'orca', side_effect=self.transport), patch.object(runner, 'orca', return_value={}):
            runner.poll_outputs(self.state)
        return launch.registry(self.attempt_dir)['agents'][0]

    def switches(self):
        return [args for args, _ in self.calls if args[:2] == ('terminal', 'switch')]

    def test_poll_observes_detached_panel_without_switching_or_journaling(self):
        store.atomic_json(launch.journal(self.attempt_dir), {'prior': {'status': 'completed'}})
        for _ in range(2):
            self.assertEqual(self.poll()['panel_status'], 'orphaned')
        self.assertEqual(self.switches(), [])
        self.assertEqual(store.read_json(launch.journal(self.attempt_dir)), {'prior': {'status': 'completed'}})
        self.assertFalse((self.attempt_dir / 'panel-operations.json').exists())

    def test_visible_panel_never_changes_focus(self):
        self.terminal['orphaned'] = False
        self.assertEqual(self.poll()['panel_status'], 'visible')
        self.assertEqual(self.switches(), [])
        self.assertFalse((self.attempt_dir / 'panel-operations.json').exists())

    def test_foreign_or_missing_worker_identity_never_restores(self):
        for field, value in (('handle', 'foreign'), ('incarnationId', 'foreign'), ('worktreeId', 'foreign')):
            with self.subTest(field=field):
                previous = self.terminal[field]
                self.terminal[field] = value
                self.assertIn('mismatch', self.poll()['panel_error'])
                self.terminal[field] = previous
        self.data['terminalResource']['ownerDispatchId'] = 'foreign'
        self.assertIn('mismatch', self.poll()['panel_error'])
        self.assertEqual(self.switches(), [])

    def test_terminal_show_rechecks_incarnation_after_worker_show(self):
        def stale(*args, **kwargs):
            result = self.transport(*args, **kwargs)
            if args[:2] == ('terminal', 'show'):
                result['terminal']['incarnationId'] = 'replacement'
            return result
        with patch.object(launch, 'orca', side_effect=stale), patch.object(runner, 'orca', return_value={}):
            runner.poll_outputs(self.state)
        self.assertEqual(self.switches(), [])
        self.assertIn('incarnation', launch.registry(self.attempt_dir)['agents'][0]['panel_error'])

    def test_dead_unknown_disconnected_and_operator_closed_terminals_are_untouched(self):
        for verdict in ('exited', 'unknown', None):
            with self.subTest(liveness=verdict):
                self.data['projection']['liveness']['verdict'] = verdict
                self.poll()
        self.data['projection']['liveness']['verdict'] = 'live'
        for changes in ({'connected': False}, {'writable': False}, {'exitCause': {'kind': 'operator_close'}},
                        {'orphaned': None}):
            with self.subTest(changes=changes):
                previous = copy.deepcopy(self.terminal)
                self.terminal.update(changes)
                self.poll()
                self.terminal = previous
        self.assertEqual(self.switches(), [])

    def test_fenced_stopping_paused_and_inactive_records_are_untouched(self):
        for changes in ({'fenced': True}, {'stop_requested': True}):
            launch.save_registry(self.attempt_dir, {**self.reg, **changes})
            self.poll()
        launch.save_registry(self.attempt_dir, self.reg)
        for flag in ('stop_requested', 'paused'):
            self.state[flag] = True
            self.poll()
            self.state.pop(flag)
        for changes in ({'status': 'released'}, {'status': 'stopped'}, {'status': 'superseded'},
                        {'superseded_by': 'other'}, {'terminal_owned': False}, {'incarnation': None}):
            launch.save_registry(self.attempt_dir, {**self.reg, 'agents': [{**self.record, **changes}]})
            self.poll()
        self.assertEqual(self.switches(), [])

    def test_historical_panel_operations_are_not_replayed_or_rewritten(self):
        journal = self.attempt_dir / 'panel-operations.json'
        for status in ('pending', 'unknown', 'completed'):
            with self.subTest(status=status):
                history = {'agent-panel-switch': {'status': status, 'stage': 'intent',
                    'request_id': 'prior', 'args': ['terminal', 'switch', '--terminal', 'term'],
                    'mailbox': 'mailbox'}}
                store.atomic_json(journal, history)
                self.assertEqual(self.poll()['panel_status'], 'orphaned')
                launch.check_operations(self.attempt_dir)
                self.assertEqual(store.read_json(journal), history)
        self.assertEqual(self.switches(), [])

    def test_manual_reattachment_is_observed_without_automatic_switch(self):
        self.assertEqual(self.poll()['panel_status'], 'orphaned')
        self.terminal['orphaned'] = False
        self.assertEqual(self.poll()['panel_status'], 'visible')
        self.terminal['orphaned'] = True
        self.assertEqual(self.poll()['panel_status'], 'orphaned')
        self.assertEqual(self.switches(), [])

    def test_observe_stays_read_only_even_for_orphaned_panels(self):
        with patch.object(launch, 'orca', side_effect=self.transport):
            launch.observe_agent(self.record)
        self.assertEqual([args[:2] for args, _ in self.calls], [('orchestration', 'worker-show')])
