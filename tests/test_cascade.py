"""Cascade lease, shutdown fencing and dependency scheduling through the CLI."""

from argparse import Namespace
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from team_runner import cascade, queue_plan, service
from team_runner.cascade_store import Session
from team_runner.engine import Runner
from team_runner.runner_operations import UncertainOperation
from team_runner.runner_store import atomic_json, lock, read_json
import test_platform


CARD = '# Item\n\n' + '\n'.join(f'## {section.title()}\nFrozen {section}\n' for section in sorted(queue_plan.SECTIONS))


class Backend:
    def __init__(self):
        self.manager = {'mailbox': 'manager', 'session': 'codex-manager'}
        self.actions = []
        self.receipts = {}
        self.unsupported = False

    def role_snapshots(self, context):
        if self.unsupported:
            raise ValueError('unsupported role')
        return {}

    def preflight(self, context):
        self.actions.append('identity')
        return self.manager_identity(context)

    def manager_identity(self, context):
        return self.manager.copy()

    def ensure_manager(self, session):
        with session.transaction() as state:
            state['manager_run'] = 'manager-run'

    def ensure_attempt(self, session, fid):
        with session.transaction() as state:
            feature = state['features'][fid]
            if 'checkout' not in feature:
                self.actions.append(('checkout', fid, feature['base_sha']))
                feature['checkout'] = str(session.context.repo)

    def ensure_role(self, session, fid, role):
        with session.transaction() as state:
            feature = state['features'][fid]
            feature['roles'].setdefault(role, dict(
                role=role, run='manager-run', task='task-' + fid, dispatch='dispatch-' + fid,
                checkout=feature['checkout'], attempt=feature['attempt'], execution=session.execution,
            ))

    def reconcile(self, session, operation):
        try:
            return self.receipts[operation['request_id']]
        except KeyError:
            raise UncertainOperation('request-show absent') from None

    def cleanup(self, session, *, stopping, operator=False):
        return {'resources_exited': True, 'dispatches': []}


class Cascade(unittest.TestCase):
    base_goal = 'feat/goal-fixture'

    def setUp(self):
        test_platform.Platform.setUp(self)
        self.catalog.project(project_id=self.a.context.project_id,
                             values={'profile': {'workflow': {'allow_commits': True}}})
        self.context = self.catalog.context(self.a.context.project_id, self.a.context.workspace_id)
        self.project = self.context.project_id
        self.workspace = self.context.workspace_id
        self.execution = str(uuid4())
        self.backend = Backend()
        self.goal = self.context.repo / 'cascade-fixture'
        self.queue = {'schema_version': 1, 'queue': []}
        for fid, deps in [('feat-a', []), ('feat-b', ['feat-a'])]:
            folder = self.goal / fid
            folder.mkdir(parents=True)
            (folder / 'item.md').write_text(CARD)
            self.queue['queue'].append({'id': fid, 'path': fid, 'depends_on': deps, 'items': [
                {'id': 'item', 'task': fid, 'depends_on': [], 'step_card': 'item.md'},
            ]})
        atomic_json(self.goal / 'Queue.json', self.queue)
        self.git('add', '--all')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'fixture')
        self.base_sha = self.git('rev-parse', 'HEAD')
        self.session = Session(self.catalog, self.context, self.execution, self.backend)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.context.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def action(self, action, **override):
        fields = dict(project=self.project, workspace=self.workspace, execution=self.execution,
                      goal=self.goal, allow_commits=True, operator=False, feature=None, dispatch=None)
        args = Namespace(cascade_action=action, **{**fields, **override})
        return cascade.execute(args, self.catalog, self.backend)

    def test_queue_and_profile_reject_before_effects(self):
        with self.assertRaisesRegex(ValueError, 'requires --allow-commits'):
            self.action('start', allow_commits=False)
        self.backend.unsupported = True
        with self.assertRaisesRegex(ValueError, 'unsupported role'):
            self.action('start')
        self.backend.unsupported = False
        self.queue['queue'][1]['items'][0]['depends_on'] = ['feat-a/item']
        atomic_json(self.goal / 'Queue.json', self.queue)
        with self.assertRaisesRegex(ValueError, 'no item dependencies'):
            self.action('start')
        self.assertFalse(self.context.lease_path.exists())
        self.assertEqual(self.backend.actions, [])

    def test_snapshot_competing_owner_and_pinned_dependency_after_branch_moves(self):
        first = self.action('start')
        self.assertEqual(set(first['features']), {'feat-a'})
        self.assertEqual(first['features']['feat-a']['base_sha'], self.base_sha)
        self.assertNotIn('feat-b', self.action('resume')['features'])
        with self.assertRaisesRegex(ValueError, 'leased by cascade'):
            self.action('start', execution=str(uuid4()))
        (self.goal / 'Queue.json').write_text('source no longer exists as valid JSON')
        self.assertEqual(self.action('start')['features'], first['features'])
        self.assertEqual(self.session.read()['snapshot']['cards']['feat-a/item'], CARD)
        with self.assertRaisesRegex(ValueError, 'requires successful'):
            self.action('accept', feature='feat-a', dispatch='dispatch-feat-a')
        self.assertNotIn('feat-b', self.action('resume')['features'])
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.test',
                 'commit', '--allow-empty', '-qm', 'candidate')
        candidate = self.git('rev-parse', 'HEAD')
        with self.session.transaction() as state:
            state['features']['feat-a']['accepted'] = {'sha': candidate}
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.test',
                 'commit', '--allow-empty', '-qm', 'later HEAD')
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), candidate)
        self.action('resume')
        self.action('resume')
        self.assertEqual(self.backend.actions.count(('checkout', 'feat-b', candidate)), 1)

    def test_stop_unknown_blocks_normal_runner_and_releases_only_after_proof(self):
        self.action('start')
        with self.session.transaction() as state:
            started = cascade.messages.intent(state, 'uncertain', ['orchestration', 'send'], mailbox='manager')
            request = started['request_id']
            started['status'] = 'calling'
            cascade.messages.intent(state, 'prepared', ['orchestration', 'send'], mailbox='manager')
        checkout_runner = Runner(self.context)
        with patch.object(checkout_runner, 'orca', self.a.orca), \
                patch.object(checkout_runner.launcher, 'create_terminal', return_value={'handle': 'fixture'}) as launch:
            with self.assertRaisesRegex(UncertainOperation, 'request-show absent'):
                self.action('stop')
            self.assertEqual(read_json(self.context.lease_path), self.session.lease)
            with self.assertRaisesRegex(ValueError, 'leased by cascade'):
                service.start_runner(checkout_runner, {'goal': self.base_goal})
            launch.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'cascade is stopping'):
                self.session.effect('prepared')
            self.backend.receipts[request] = {'proved': True}
            # Simulate lost process after durable terminal record, before unlink.
            with patch('team_runner.cascade_store.runner_operations.checkpoint', side_effect=RuntimeError('lost response')):
                # Only the in-store checkpoint is relevant for this simulation.
                with patch('team_runner.cascade.checkpoint'):
                    with self.assertRaisesRegex(RuntimeError, 'lost response'):
                        self.action('stop')
            self.assertEqual(self.session.read()['status'], 'stopped')
            self.assertTrue(self.context.lease_path.exists())
            self.assertEqual(self.action('stop')['status'], 'stopped')
            self.assertFalse(self.context.lease_path.exists())
            self.assertEqual(service.start_runner(checkout_runner, {'goal': self.base_goal})['status'], 'starting')
            launch.assert_called_once()
        self.assertEqual(self.session.read()['operations']['prepared']['status'], 'cancelled')

    def test_operator_must_own_state_and_be_outside_child(self):
        self.action('start')
        self.backend.manager['session'] = 'unrelated'
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            self.action('stop')
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': '/fixture/child'}):
            with self.assertRaisesRegex(ValueError, 'outside a child'):
                self.action('stop', operator=True)
        self.assertEqual(self.action('stop', operator=True)['status'], 'stopped')

    def test_stop_fences_before_waiting_for_running_operation(self):
        self.action('start')
        with lock(self.session.directory / 'effect.lock'):
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                self.action('stop')
            self.assertEqual(self.session.read()['status'], 'stopping')
            with self.assertRaisesRegex(ValueError, 'cascade is stopping'):
                self.action('resume')
        self.assertEqual(self.action('stop')['status'], 'stopped')

    def test_repeat_stop_preserves_admitted_cleanup_intent(self):
        self.action('start')
        self.session.begin_stop()
        with self.session.transaction(stopping=True) as state:
            cascade.messages.intent(state, 'cleanup', ['orchestration', 'worker-stop'], mailbox='manager')
        self.session.begin_stop()
        self.assertEqual(self.session.read()['operations']['cleanup']['status'], 'intent')

    def test_inherited_effect_lock_survives_parent_context(self):
        marker = self.root / 'operation.lock'
        with lock(marker, inherited=True) as fd:
            process = subprocess.Popen(['cat'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, pass_fds=(fd,))
        try:
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                with lock(marker, blocking=False):
                    self.fail('inherited lock was released while its subprocess was live')
        finally:
            process.communicate(timeout=5)
        with lock(marker, blocking=False):
            self.assertEqual(process.returncode, 0)

    def test_child_checkout_uses_the_same_runner_lease_contract(self):
        self.action('start')
        child_path = self.root / 'cascade-child'
        self.git('worktree', 'add', '--detach', str(child_path), self.base_sha)
        child = self.catalog.workspace(self.project, {
            'path': str(child_path), 'orca_id': 'child', 'orca_instance': 'child-incarnation',
        })
        context = self.catalog.context(self.project, child['id'])
        with self.session.transaction() as state:
            state['features']['feat-a']['checkout'] = str(child_path)
        self.session.claim_child('feat-a')
        with lock(context.checkout_lock), self.assertRaisesRegex(ValueError, 'leased by cascade'):
            self.catalog.claim(context)
        self.action('stop')
        with lock(context.checkout_lock):
            self.catalog.claim(context)
        self.assertEqual(read_json(context.lease_path), {'identity': context.identity()})

    def test_report_recovers_original_worker_done_after_lost_receipt(self):
        self.action('start')
        with self.session.transaction() as state:
            feature = state['features']['feat-a']
            attempt = Path(feature['directory'])
            attempt.mkdir(parents=True)
            feature['roles']['implementer'] = dict(
                role='implementer', run='child-run', task='task-implementation',
                dispatch='dispatch-implementer', terminal='term-implementer', attempt=feature['attempt'],
                checkout=str(self.context.repo), execution=self.execution,
            )
        path = attempt / 'input.json'
        atomic_json(path, dict(role='implementer', outcome='succeeded', sha=self.base_sha,
                               summary='Implemented', evidence=[]))

        def send(*args, mailbox):
            self.assertEqual(mailbox, 'term-implementer')
            self.assertEqual(args[:2], ('orchestration', 'send'))
            request = args[args.index('--retry-request') + 1]
            receipt = {'message': {'subject': 'Cascade worker result', 'body': 'Implemented',
                                   'payload': {'outcome': 'succeeded'}},
                       'mutation': {'requestId': request}}
            self.backend.receipts[request] = receipt
            return receipt

        observed = {'dispatch': {'status': 'dispatched'}}
        with (patch.object(self.backend, 'call', side_effect=send, create=True) as call,
              patch.object(self.backend, 'assignment_identity', return_value=observed, create=True),
              patch.object(self.backend, 'validate_report', side_effect=lambda _s, _a, r: r['message'], create=True)):
            with patch.dict(os.environ, {'TEAM_RUNNER_CRASH_AT': ''}):
                with patch('team_runner.cascade_store.runner_operations.checkpoint',
                           side_effect=[None, RuntimeError('lost receipt')]):
                    with self.assertRaisesRegex(RuntimeError, 'lost receipt'):
                        self.action('report', feature='feat-a', dispatch='dispatch-implementer', input=path)
                observed['dispatch']['status'] = 'completed'
                receipt = self.action('report', feature='feat-a', dispatch='dispatch-implementer', input=path)
            call.assert_called_once()
        state = self.session.read()
        operation = state['operations']['feat-a:report:dispatch-implementer']
        self.assertEqual(operation['status'], 'completed')
        self.assertEqual(receipt['mutation']['requestId'], operation['request_id'])


if __name__ == '__main__':
    unittest.main()
