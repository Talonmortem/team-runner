"""Engine queue/final progression across a proved-dead between-item crash."""
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import goal_intent, goal_controller, goal_completion, application
from team_runner.runner_store import atomic_json, read_json


class AutonomousQueue(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def test_between_item_crash_resumes_remaining_queue_then_final(self):
        runner = self.a
        goal = 'feat/goal-fixture'
        feature_path = runner.repo / goal / 'feat-one/Tasks.json'
        feature = read_json(feature_path)
        feature['queue'] = feature['queue'][:2]
        atomic_json(feature_path, feature)
        ids = [item['id'] for item in feature['queue']]
        auth = goal_intent.authorize(runner, {'goal': goal}, str(uuid.uuid4()))
        runner.authorization = {k: auth[k] for k in ('generation', 'epoch')}
        args = SimpleNamespace(**dict(goal=goal, mode='auto', final=True, items=None, config=None, model=None, handoff=None))
        dispatched = []
        def coordinator(args, state, item, spec, title):
            dispatched.append(item)
            state['current'] = {'item': item, 'dispatch_id': 'dispatch-' + str(len(dispatched)),
                                'start_head': 'fixture', 'dirty_before': [], 'attempt': 1}
            runner.save_state(state)
        def done(state):
            item = state['current']['item']
            if item != goal_completion.FINAL_ID:
                feature = read_json(feature_path)
                for task in feature['queue']:
                    if item.endswith('/' + task['id']):
                        task['status'] = 'completed'
                atomic_json(feature_path, feature)
                artifact = feature_path.parent / runner.attempt_profile()['evidence_dir'] / item.split('/')[-1].lower() / 'acceptance.md'
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text('fixture acceptance')
                if all(task['status'] == 'completed' for task in feature['queue']):
                    root_path = runner.repo / goal / 'Tasks.json'
                    root = read_json(root_path)
                    root['queue'][0]['status'] = 'completed'
                    atomic_json(root_path, root)
            else:
                artifact = runner.repo / goal / runner.attempt_profile()['evidence_dir'] / 'goal-final-review.md'
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text('fixture final acceptance')
            return {'outcome': 'succeeded', 'body': 'fixture acceptance'}
        original_finish = runner.finish_item
        crashed = False
        def finish(*args):
            nonlocal crashed
            result = original_finish(*args)
            if not crashed:
                crashed = True
                raise RuntimeError('fixture crash after accepted state write')
            return result
        patches = [patch.object(runner, 'ensure_run', return_value='fixture-run'),
                   patch.object(runner, 'process_controls'), patch.object(runner, 'notify'),
                   patch.object(runner, 'start_coordinator', side_effect=coordinator),
                   patch.object(runner, 'wait_coordinator', side_effect=done),
                   patch.object(runner, 'verify_item', return_value=[]),
                   patch.object(runner, 'verify_final', return_value=[]),
                   patch.object(runner, 'poll_children'),
                   patch.object(runner, 'child_acceptance_problems', return_value=([], [])),
                   patch.object(runner, 'git', return_value='fixture-head'),
                   patch.object(runner, 'effect', return_value={}),
                   patch.object(runner, 'finish_item', side_effect=finish)]
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        with self.assertRaisesRegex(RuntimeError, 'fixture crash'):
            runner.cmd_run(args)
        self.assertEqual(len(runner.load_state()['history']), 1)
        self.assertIsNone(runner.load_state()['current'])
        nonce = str(uuid.uuid4())
        atomic_json(runner.state_dir / 'launch.json', {'nonce': nonce, 'identity': runner.context.identity(),
                    'authorization': runner.authorization})
        atomic_json(runner.state_dir / 'runner-process.json', {'nonce': nonce, 'identity': runner.context.identity(),
                    'pid': 999, 'process_stamp': 'fixture'})
        (runner.state_dir / ('launch-' + nonce + '.claim')).touch()
        def resume(runner, data, **kwargs):
            self.assertEqual(kwargs['expected_authorization'], runner.authorization)
            runner.cmd_run(args)
            return {'status': 'starting'}
        with patch('team_runner.project_services.departed', return_value=True), \
                patch('team_runner.service.start_runner', side_effect=resume) as start:
            goal_controller.tick(runner, 100)
            goal_controller.tick(runner, 103)
            goal_controller.tick(runner, 200)
        start.assert_called_once()
        self.assertEqual([item.split('/')[-1] for item in dispatched], ids + [goal_completion.FINAL_ID])
        self.assertEqual(goal_completion.read(runner)['status'], 'completed')
        self.assertEqual(application.summary(runner)['observed_state'], 'completed')
