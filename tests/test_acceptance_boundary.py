"""The durable Runner transition, including crash recovery, opens dependencies."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_goal_runner import Fixture, launch, runner, store
from team_runner.tracking import for_goal


class AcceptanceBoundary(Fixture):
    def prepare(self, stage=False):
        attempt, state = self.attempt()
        goal = 'feat/boundary'
        directory = runner.repo / goal
        store.atomic_json(directory / 'Tasks.json', {'queue': [
            {'id': 'first', 'path': 'first', 'status': 'completed',
             **({'stage_gate': 'one'} if stage else {})},
            {'id': 'next', 'path': 'next', 'depends_on': ['first']},
        ]})
        item = {'id': 'one', 'status': 'completed', 'task': 'Original contract'}
        if stage:
            receipt = attempt / 'accepted.json'
            item.update(check_scope='stage', gate_evidence={'accepted': str(receipt)})
            store.atomic_json(receipt, {'valid': True})
        store.atomic_json(directory / 'first/Tasks.json', {'queue': [item]})
        store.atomic_json(directory / 'next/Tasks.json', {'queue': [{'id': 'two'}]})
        state['goal'] = goal
        state['current'].update(item='first/one', dispatch_id='accepted-dispatch')
        state['current']['messages'] = [{
            'id': 'done', 'type': 'worker_done', 'run_id': 'parent',
            'payload': {'dispatchId': 'accepted-dispatch', 'outcome': 'succeeded'}, 'body': 'report',
        }]
        runner.save_state(state)
        return goal, state

    def test_worker_done_stage_receipt_and_crashes_on_both_sides_of_state_write(self):
        goal, state = self.prepare(stage=True)
        attempt_id = Path(state['current']['attempt_dir']).name
        args = SimpleNamespace(goal=goal)

        def next_key():
            tracking = for_goal(runner, goal)
            feat, _, item = tracking.next_item()
            return tracking.key(feat, item)

        def stage_valid(_, item):
            return (item.get('check_scope') != 'stage'
                    or store.read_json(item['gate_evidence']['accepted']) == {'valid': True})

        with patch('team_runner.verification.gate_valid', side_effect=stage_valid), \
                patch('team_runner.verification.accept_gate'), patch.object(runner, 'poll_children'), \
                patch.object(runner, 'settle_children', return_value=True), \
                patch.object(launch, 'release_agent'), patch.object(runner, 'notify'), \
                patch.object(runner, 'git', return_value='head'):
            self.assertEqual(next_key(), 'first/one')
            for boundary in ('acceptance:before_save', 'acceptance:after_save'):
                state = runner.load_state()
                done = runner.wait_coordinator(state)
                self.assertEqual(done['outcome'], 'succeeded')

                def interrupt(point):
                    if point == boundary:
                        raise SystemExit(86)

                with patch('team_runner.runner_operations.checkpoint', side_effect=interrupt), \
                        self.assertRaises(SystemExit):
                    runner.finish_item(args, state, done, lambda: [])
                self.assertEqual(next_key(), 'first/one' if boundary.endswith('before_save') else 'next/two')
            persisted = runner.load_state()
            self.assertIsNone(persisted['current'])
            self.assertEqual(len(persisted['history']), 1)
            entry = persisted['history'][0]
            self.assertEqual(entry['item'], 'first/one')
            self.assertEqual(entry['dispatch_id'], 'accepted-dispatch')
            self.assertEqual(entry['attempt_id'], attempt_id)

    def test_legacy_history_and_new_contract_in_same_goal(self):
        goal, state = self.prepare()
        entry = {'item': 'one', 'dispatch_id': 'legacy', 'outcome': 'succeeded', 'problems': ['rejected']}
        state['history'] = [entry]
        runner.save_state(state)
        self.assertFalse(for_goal(runner, goal).feature_done('first'))
        entry['problems'] = []
        runner.save_state(state)
        self.assertTrue(for_goal(runner, goal).feature_done('first'))
        tracking = for_goal(runner, goal)
        key, feature, path, item = tracking.resolve('one')
        item['verification'] = {'checks': ['original check'], 'status': 'not_started', 'evidence': []}
        entry.update(item=key, attempt_id='new-attempt', item_contract=tracking.contract(feature, item))
        runner.save_state(state)
        store.atomic_json(path, {'queue': [{**item, 'status': 'not_started', 'verification': {
            'checks': ['original check'], 'status': 'passed', 'evidence': ['log.txt']}}]})
        self.assertTrue(for_goal(runner, goal).feature_done('first'))
        store.atomic_json(path, {'queue': [{**item, 'task': 'new requirement'}]})
        self.assertFalse(for_goal(runner, goal).feature_done('first'))
        state['goal'] = 'feat/other'
        runner.save_state(state)
        store.atomic_json(path, {'queue': [item]})
        self.assertFalse(for_goal(runner, goal).feature_done('first'))
