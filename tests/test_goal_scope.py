"""Queue scope replacement is explicit, paused, journaled and never launches."""
import contextlib
import copy
import io
import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, cli, goal_controller, goal_execute, goal_intent, goal_scope
from team_runner.protocol import ContractError
from team_runner.runner_store import atomic_json, lock, read_json


class GoalScope(unittest.TestCase):
    def setUp(self):
        fixtures.Platform.setUp(self)
        self.goal = 'feat/goal-fixture'
        self.feature = self.a.repo / self.goal / 'feat-one/Tasks.json'
        feature = read_json(self.feature)
        feature['queue'][0]['status'] = 'completed'
        atomic_json(self.feature, feature)
        self.options = {'goal': self.goal, 'items': 'feature/T1', 'mode': 'auto', 'final': False}
        self.old = goal_intent.authorize(self.a, self.options, str(uuid.uuid4()))
        self.previous = goal_intent.read(self.a)
        self.state = {'goal': self.goal, 'options': self.options, 'runner_status': 'idle',
                      'current': None, 'history': [{'item': 'feature/T1', 'outcome': 'succeeded',
                                                  'problems': []}]}
        self.a.save_state(self.state)
        self.identifier = str(uuid.uuid4())

    def prepare(self, **overrides):
        args = {'items': 'feature/T2,feature/T3', 'identifier': self.identifier,
                'generation': self.old['generation'], 'epoch': self.old['epoch']}
        args.update(overrides)
        return goal_scope.prepare(self.a, **args)

    def test_archives_old_authorization_preserves_history_and_never_launches(self):
        before = self.a.state_file.read_bytes()
        with patch.object(self.a, 'orca') as orca, \
                patch('team_runner.service.start_runner') as start, \
                patch('team_runner.project_services.ProjectServices') as services:
            result = self.prepare()
            goal_controller.tick(self.a, 100)
            orca.assert_not_called(); start.assert_not_called(); services.assert_not_called()
        saved = goal_intent.read(self.a)
        self.assertEqual(saved['desired_state'], 'paused')
        self.assertEqual(saved['epoch'], 1)
        self.assertNotEqual(saved['generation'], self.old['generation'])
        self.assertEqual(saved['options'], {**self.options, 'items': 'feature/T2,feature/T3'})
        self.assertEqual(saved['policy'], self.previous['policy'])
        self.assertEqual(read_json(result['archive']), self.previous)
        self.assertEqual(self.a.state_file.read_bytes(), before)
        self.assertFalse(goal_intent.permitted(self.a, self.old['generation'], self.old['epoch']))
        self.assertFalse(goal_intent.permitted(self.a, saved['generation'], saved['epoch']))
        self.assertFalse((self.a.state_dir / 'launch.json').exists())
        self.assertFalse((self.a.state_dir / 'startup.json').exists())

    def test_repeat_receipt_does_not_undo_later_resume_or_stop(self):
        first = self.prepare()
        resumed = goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                                        generation=first['generation'], epoch=first['epoch'])
        self.assertEqual(resumed['epoch'], 2)
        self.assertEqual(goal_intent.read(self.a)['options']['items'], 'feature/T2,feature/T3')
        goal_intent.change(self.a, desired_state='stopped')
        before = (self.a.state_dir / 'goal-intent.json').read_bytes()
        with lock(self.a.state_dir / 'runner.lock'):
            self.assertEqual(self.prepare(), first)
        self.assertEqual((self.a.state_dir / 'goal-intent.json').read_bytes(), before)

    def test_explicit_execute_uses_new_scope_and_rejects_old_generation(self):
        from test_goal_execute import ManagerFixture, Services
        result = self.prepare()
        with patch('team_runner.service_manager.Manager', return_value=ManagerFixture()), \
                patch('team_runner.goal_execute.ProjectServices', return_value=Services()), \
                patch('team_runner.service.start_runner', return_value={'status': 'starting'}) as start:
            with self.assertRaisesRegex(ContractError, 'Authorization changed'):
                application.execute(self.a, {'request_id': str(uuid.uuid4()), 'options': {'resume': True},
                                             'generation': self.old['generation'], 'epoch': self.old['epoch']})
            start.assert_not_called()
            executed = application.execute(self.a, {'request_id': str(uuid.uuid4()),
                'options': {'resume': True}, 'generation': result['generation'], 'epoch': result['epoch']})
            self.assertEqual(executed['status'], 'succeeded')
            self.assertEqual(start.call_args.args[1]['items'], 'feature/T2,feature/T3')
            self.assertEqual(start.call_args.kwargs['expected_authorization']['generation'], result['generation'])
            self.assertEqual(start.call_count, 1)

    def test_conflicting_archive_and_incomplete_final_review_fail_closed(self):
        archive = self.a.state_dir / 'goal-authorizations' / (self.old['generation'] + '.json')
        atomic_json(archive, {'foreign': True})
        with self.assertRaisesRegex(ContractError, 'Archived authorization differs'):
            self.prepare()
        self.assertEqual(goal_intent.read(self.a), self.previous)
        self.assertEqual(read_json(archive), {'foreign': True})
        archive.unlink()
        final = copy.deepcopy(self.previous)
        final['options']['final'] = True
        final['initial_options']['final'] = True
        final['policy']['final'] = True
        for receipt in final['requests'].values(): receipt['request']['options']['final'] = True
        atomic_json(self.a.state_dir / 'goal-intent.json', final)
        with self.assertRaisesRegex(ContractError, 'final review'):
            self.prepare(identifier=str(uuid.uuid4()))

    def test_stale_scope_unknown_items_duplicates_and_invalid_arguments_preserve_intent(self):
        before = (self.a.state_dir / 'goal-intent.json').read_bytes()
        for args in ({'epoch': 99}, {'generation': str(uuid.uuid4())}, {'epoch': True},
                     {'items': 'feature/missing'}, {'items': 'feature/T2,T2'},
                     {'items': ''}, {'items': 'feature/T2,'}, {'items': ' feature/T2'}):
            with self.subTest(args=args), self.assertRaises((ContractError, ValueError)):
                self.prepare(**args)
            self.assertEqual((self.a.state_dir / 'goal-intent.json').read_bytes(), before)
        self.assertFalse((self.a.state_dir / 'goal-authorizations').exists())

    def test_same_id_other_payload_or_command_is_rejected(self):
        first = self.prepare()
        with self.assertRaisesRegex(ContractError, 'already used'):
            self.prepare(items='feature/T4')
        with self.assertRaisesRegex(ContractError, 'already used'):
            application.execute(self.a, {'request_id': self.identifier, 'options': {'resume': True},
                                         'generation': first['generation'], 'epoch': first['epoch']})
        with self.assertRaisesRegex(ValueError, 'already used'):
            application.submit_command(self.a, 'start', {'resume': True}, self.identifier)

    def test_existing_command_request_id_is_rejected(self):
        atomic_json(self.a.state_dir / 'ui/commands' / (self.identifier + '.json'),
                    {'id': self.identifier, 'action': 'execute', 'status': 'pending'})
        with self.assertRaisesRegex(ContractError, 'already used'):
            self.prepare()

    def test_old_queue_requires_both_completed_tracking_and_accepted_history(self):
        for kind in ('tracking', 'history', 'failed', 'problems'):
            with self.subTest(kind=kind):
                feature = read_json(self.feature)
                feature['queue'][0]['status'] = 'not_started' if kind == 'tracking' else 'completed'
                atomic_json(self.feature, feature)
                state = copy.deepcopy(self.state)
                if kind == 'history': state['history'] = []
                if kind == 'failed': state['history'][0]['outcome'] = 'failed'
                if kind == 'problems': state['history'][0]['problems'] = ['not accepted']
                self.a.save_state(state)
                with self.assertRaisesRegex(ContractError, 'not accepted'):
                    self.prepare()

    def test_current_unknown_effects_and_unreleased_agents_block_replacement(self):
        for field, value in [('current', {'item': 'feature/T2'}), ('control_uncertain', True),
                             ('recovery_issues', ['unknown']), ('stop_status', 'unknown'),
                             ('last_error', 'unresolved')]:
            with self.subTest(field=field):
                self.a.save_state({**self.state, field: value})
                with self.assertRaises(ContractError): self.prepare()
        self.a.save_state(self.state)
        for status in ('starting', 'unknown'):
            atomic_json(self.a.state_dir / 'startup.json', {'status': status})
            with self.assertRaises(ContractError): self.prepare()
        atomic_json(self.a.state_dir / 'startup.json', {'status': 'idle'})
        path = self.a.state_dir / 'operations.json'
        atomic_json(path, {'pending': {'status': 'unknown'}})
        with self.assertRaises(ContractError): self.prepare()
        atomic_json(path, {})
        execute = self.a.state_dir / 'execute' / (str(uuid.uuid4()) + '.json')
        atomic_json(execute, {'status': 'unknown'})
        with self.assertRaises(ContractError): self.prepare()
        execute.unlink()
        attempt = self.a.state_dir / 'attempts' / str(uuid.uuid4())
        atomic_json(attempt / 'agents.json', {'agents': [{'status': 'ready'}]})
        state = copy.deepcopy(self.state); state['history'][-1]['attempt_dir'] = str(attempt)
        self.a.save_state(state)
        with self.assertRaisesRegex(ContractError, 'agents'): self.prepare()

    def test_resource_locks_reject_active_owner(self):
        for path in (self.a.state_dir / 'runner.lock', self.a.state_dir / 'start.lock',
                     self.a.state_dir / 'execute.lock', self.a.context.checkout_lock):
            with self.subTest(path=path), lock(path), self.assertRaises(RuntimeError):
                self.prepare()

    def test_child_and_disabled_workspace_cannot_replace_scope(self):
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': '/fixture/attempt'}):
            with self.assertRaisesRegex(ContractError, 'Child attempts'): self.prepare()
        self.catalog.disable(self.a.context.project_id, self.a.context.workspace_id)
        with self.assertRaises(ValueError): self.prepare()

    def test_concurrent_duplicate_creates_one_generation(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.prepare(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(list((self.a.state_dir / 'goal-authorizations').glob('*.json'))), 1)

    def test_interrupted_archive_or_intent_write_can_finish_same_request(self):
        real_write = goal_scope.atomic_json
        for boundary in ('archive', 'intent'):
            # A single interrupted pending preparation may be resumed, never
            # replaced by another request UUID.
            def write(path, value):
                if ((boundary == 'archive' and path.parent.name == 'goal-authorizations')
                        or (boundary == 'intent' and path.name == 'goal-intent.json')):
                    raise OSError('fixture crash')
                return real_write(path, value)
            with self.subTest(boundary=boundary), patch.object(goal_scope, 'atomic_json', side_effect=write):
                with self.assertRaises(OSError): self.prepare()
            with self.assertRaisesRegex(ContractError, 'previous scope preparation'):
                self.prepare(identifier=str(uuid.uuid4()))
        result = self.prepare()
        self.assertEqual(goal_intent.read(self.a)['generation'], result['generation'])

    def test_crash_after_intent_commit_never_overwrites_subsequent_pause(self):
        real_write = goal_scope.atomic_json
        def write(path, value):
            if path.parent.name == 'commands' and value.get('status') == 'succeeded':
                raise OSError('fixture crash')
            return real_write(path, value)
        with patch.object(goal_scope, 'atomic_json', side_effect=write):
            with self.assertRaises(OSError): self.prepare()
        prepared = goal_intent.read(self.a)
        resumed = goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                                        generation=prepared['generation'], epoch=prepared['epoch'])
        goal_intent.change(self.a, desired_state='stopped')
        before = (self.a.state_dir / 'goal-intent.json').read_bytes()
        result = self.prepare()
        self.assertEqual(result['generation'], resumed['generation'])
        self.assertEqual((self.a.state_dir / 'goal-intent.json').read_bytes(), before)

    def test_cli_prepares_paused_scope_without_live_smoke(self):
        output = io.StringIO()
        with patch.object(cli, 'Catalog', return_value=self.catalog), \
                contextlib.redirect_stdout(output):
            code = cli.main(['prepare-scope', '--project', self.a.context.project_id,
                             '--workspace', self.a.context.workspace_id, '--request-id', self.identifier,
                             '--generation', self.old['generation'], '--epoch', str(self.old['epoch']),
                             '--items', 'feature/T2,feature/T3', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['desired_state'], 'paused')


if __name__ == '__main__':
    unittest.main()
