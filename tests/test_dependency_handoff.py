"""Dependency handoff through the real queue, with isolated Orca transport."""
from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import test_platform as fixtures
from runner_browser_fixture import make_runner
from test_goal_execute import ManagerFixture, Services
from team_runner import application, artifacts, cli, goal_execute, goal_intent, goal_scope, retention, service
from team_runner.protocol import ContractError, summary
from team_runner.runner_store import atomic_json, lock, read_json


class DependencyHandoff(unittest.TestCase):
    def setUp(self):
        fixtures.Platform.setUp(self)
        self.a = make_runner(self.catalog, self.a.context.project_id, self.a.context.workspace_id)
        self.goal = 'feat/goal-fixture'
        self.feature = self.a.repo / self.goal / 'feat-one/Tasks.json'
        self.gate = 'feature/WA-GATE'
        self.predecessor = 'feature/WA-G04'
        # Listing the gate first exercises dependency order rather than file order.
        atomic_json(self.feature, {'queue': [
            {'id': 'WA-GATE', 'status': 'not_started', 'depends_on': [], 'check_scope': 'stage',
             'gate_roots': ['src'], 'gate_command': ['fixture-gate']},
            {'id': 'WA-G04', 'status': 'not_started', 'depends_on': []},
        ]})
        self.options = {'goal': self.goal, 'items': self.gate, 'mode': 'auto', 'final': False}
        self.a.authorization = goal_intent.authorize(self.a, self.options, str(uuid.uuid4()))
        self.args = SimpleNamespace(**self.options, config=None, model=None)
        self.a.save_state({'goal': self.goal, 'current': {'item': self.gate, 'attempt': 2},
                           'options': self.options, 'history': []})
        state = self.a.load_state()
        state['run_id'] = self.a.ensure_run(state)
        self.assertEqual(self.a.start_coordinator(self.args, state, self.gate, 'old spec', self.gate), 'start')
        self.old = self.a.load_state()['current']
        self.attempt = Path(self.old['attempt_dir'])
        self.identifier = str(uuid.uuid4())
        self.set_dependencies(['WA-G04'])

    def set_dependencies(self, depends_on):
        tasks = read_json(self.feature)
        tasks['queue'][0]['depends_on'] = depends_on
        atomic_json(self.feature, tasks)

    def stop(self):
        state = summary(self.a)
        receipt = application.submit_v1(self.a, {
            'schema_version': 1, 'action': 'stop', 'request_id': str(uuid.uuid4()), 'payload': {},
            'scope': {'identity': state['identity'], 'generation': state['generation'],
                      'attempt': state['current']['attempt'], 'revision': state['revision']},
        }, explicit_stop=True)
        self.assertEqual(receipt['status'], 'succeeded', receipt)
        self.a.close_main_mailbox(self.a.load_state())

    def scope(self):
        state = summary(self.a)
        return {'generation': state['generation'], 'epoch': state['epoch'],
                'attempt': state['current']['attempt'], 'revision': state['revision']}

    def prepare(self, *, scope=None, dry_run=False, identifier=None):
        return goal_scope.prepare_dependency_handoff(
            self.a, self.predecessor, identifier or self.identifier,
            dry_run=dry_run, **(scope or self.scope()))

    def test_live_dry_run_stop_resume_and_normal_predecessor_acceptance(self):
        log = self.attempt / 'gate.log'
        log.write_text('R3 fixture log')
        payload = self.attempt / 'warehouse/node_modules'
        payload.mkdir(parents=True)
        (payload.parent / 'package.json').write_text('{}')
        (payload / 'fixture').write_text('warehouse artifact')
        delta = self.a.repo / 'r3.delta'
        delta.write_text('uncommitted R3 fixture changes')
        old_scope = self.scope()
        with lock(self.a.state_dir / 'runner.lock'):
            active = self.scope()
            plan = self.prepare(scope=active, dry_run=True)
            self.assertEqual(plan['items'], f'{self.predecessor},{self.gate}')
            with self.assertRaisesRegex(RuntimeError, 'runner.lock'):
                self.prepare(scope=active)
        self.assertFalse((self.attempt / '.keep').exists())
        self.stop()
        registry = self.a.launcher.registry(self.attempt)
        coordinator = registry['agents'][0]
        registry['agents'].append({**coordinator, 'id': str(uuid.uuid4()), 'dispatch_id': 'prior-assignment',
                                   'status': 'superseded', 'terminal_closed': False,
                                   'superseded_by': coordinator['dispatch_id'],
                                   'error': 'Dispatch startup/settlement not confirmed'})
        self.a.launcher.save_registry(self.attempt, registry)
        with self.assertRaises(ContractError) as stale:
            self.prepare(scope=old_scope)
        self.assertEqual(stale.exception.code, 'stale_scope')

        stopped_scope = self.scope()
        result = self.prepare(scope=stopped_scope)
        self.assertEqual(goal_intent.read(self.a)['desired_state'], 'paused')
        self.assertEqual(application.operation(self.a, self.identifier)['result'], result)
        state = self.a.load_state()
        self.assertIsNone(state['current'])
        self.assertEqual(state['attempts'][-1]['outcome'], 'deferred')
        self.assertEqual(state['attempts'][-1]['attempt'], 2)
        self.assertEqual(self.a.launcher.registry(self.attempt)['agents'][0]['status'], 'stopped')
        self.assertTrue((self.attempt / '.keep').is_file())
        subrun = self.attempt / 'verification' / 'r3'
        subrun.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'attempt pinned'):
            artifacts.compact(self.a, subrun)
        self.assertEqual(artifacts.sweep(self.a, attempt=self.attempt)['runs'], [])
        with patch.object(retention, 'process_references', return_value=([], [])):
            inventory = retention.cleanup(self.a, days=0)
        self.assertIn({'path': str(self.attempt), 'reason': 'pinned with .keep'}, inventory['skipped'])

        def worker_done(state):
            cur = state['current']
            if cur['item'] == self.gate:
                return None  # Observe the new dispatch without accepting the gate.
            self.assertEqual(cur['item'], self.predecessor)
            self.a.orca('orchestration', 'reply', '--id', 'question-' + cur['dispatch_id'],
                        '--body', 'complete', '--run', state['run_id'])
            return {'outcome': 'succeeded', 'body': 'fixture accepted'}

        fixture_orca = self.a.orca
        binding = {'coordinator_handle': state['mailbox'], 'id': state['run_id']}

        def orca(*args, **kwargs):
            if args[:2] == ('orchestration', 'run-show'):
                return {'run': dict(binding)}
            if args[:2] == ('orchestration', 'run-use') and kwargs.get('mailbox'):
                binding['coordinator_handle'] = kwargs['mailbox']
            return fixture_orca(*args, **kwargs)

        create_terminal = self.a.launcher.create_terminal

        def terminal(directory, key, *args, **kwargs):
            if directory == self.a.state_dir / 'startup':
                return {'handle': 'isolated-runner-terminal'}
            return create_terminal(directory, key, *args, **kwargs)

        with patch('team_runner.service_manager.Manager', return_value=ManagerFixture()), \
                patch('team_runner.goal_execute.ProjectServices', return_value=Services()), \
                patch.object(self.a, 'orca', orca), patch.object(self.a.launcher, 'orca', orca), \
                patch.object(self.a.launcher, 'create_terminal', side_effect=terminal), \
                patch.object(self.a, 'wait_coordinator', side_effect=worker_done):
            receipt = application.execute(self.a, {'request_id': str(uuid.uuid4()),
                'options': {'resume': True}, 'generation': result['generation'], 'epoch': result['epoch']})
            self.assertEqual(receipt['status'], 'succeeded', receipt)
            self.assertIsNone(self.a.load_state()['current'])
            self.assertEqual(service.run_process(self.a, receipt['result']['launch']['nonce']), 1)
        self.assertEqual(receipt['status'], 'succeeded', receipt)
        state = self.a.load_state()
        self.assertEqual([(entry['item'], entry['problems']) for entry in state['history']], [(self.predecessor, [])])
        current = state['current']
        self.assertEqual(current['item'], self.gate)
        self.assertEqual(current['attempt'], 2)
        self.assertNotEqual(current['dispatch_id'], self.old['dispatch_id'])
        self.assertNotEqual(current['attempt_dir'], str(self.attempt))
        self.assertEqual(read_json(Path(current['attempt_dir']) / 'item-contract.json')['depends_on'], ['WA-G04'])
        self.assertEqual(read_json(self.attempt / 'item-contract.json')['depends_on'], [])
        self.assertEqual((payload / 'fixture').read_text(), 'warehouse artifact')
        self.assertEqual(log.read_text(), 'R3 fixture log')
        self.assertEqual(delta.read_text(), 'uncommitted R3 fixture changes')
        self.assertEqual(self.prepare(scope=stopped_scope), result)

    def test_reject_missing_snapshot_known_dependency_and_unsafe_execution(self):
        path = self.attempt / 'item-contract.json'
        original = read_json(path)
        for saved, code in ((None, 'missing_dependency_snapshot'), (['WA-G04'], 'dependency_changed')):
            atomic_json(path, {**original, 'depends_on': saved})
            with self.subTest(saved=saved), self.assertRaises(ContractError) as caught:
                self.prepare(dry_run=True)
            self.assertEqual(caught.exception.code, code)
        atomic_json(path, original)
        with self.assertRaisesRegex(ContractError, 'Explicitly stop'):
            self.prepare()
        self.stop()
        registry = self.a.launcher.registry(self.attempt)
        path = self.attempt / 'agents.json'
        atomic_json(path, {**registry, 'runs': [{'status': 'ready'}]})
        with self.assertRaisesRegex(ContractError, 'resources are not confirmed closed'):
            self.prepare()
        atomic_json(path, {**registry, 'agents': [{**registry['agents'][0], 'status': 'active'}]})
        with self.assertRaisesRegex(ContractError, 'assignments'):
            self.prepare()
        atomic_json(path, registry)
        check = self.attempt / 'verification/check.json'
        atomic_json(check, {'attempt': str(self.attempt), 'status': 'running'})
        with self.assertRaisesRegex(ContractError, 'verification resource cleanup'):
            self.prepare()
        check.unlink()
        operations = self.attempt / 'operations.json'
        prior = read_json(operations)
        atomic_json(operations, {**prior, 'fixture-unknown': {'status': 'unknown'}})
        with self.assertRaises(ContractError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'unknown_effect')
        atomic_json(operations, prior)
        old_scope = self.scope()
        self.set_dependencies([])
        with self.assertRaises(ContractError) as stale:
            self.prepare(scope=old_scope)
        self.assertEqual(stale.exception.code, 'stale_revision')
        with self.assertRaises(ContractError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'dependency_changed')
        self.assertFalse((self.attempt / '.keep').exists())

    def test_old_execute_receipt_requires_proven_retirement(self):
        running = goal_intent.read(self.a)
        generation, epoch = running['generation'], running['epoch']
        identity = self.a.context.identity()
        old_path = goal_execute._shared_path(self.a, generation, epoch, 1)
        old = {'identity': identity, 'generation': generation, 'epoch': epoch,
               'service_epoch': 1, 'options': running['options'], 'status': 'unknown', 'phase': 'launch'}
        atomic_json(old_path, old)
        paused = goal_intent.change(self.a, desired_state='paused')
        running = goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                                        generation=generation, epoch=paused['epoch'])
        nonce = str(uuid.uuid4())
        launch = {'nonce': nonce, 'identity': identity, 'options': self.options,
                  'authorization': {'generation': generation, 'epoch': running['epoch']}}
        atomic_json(self.a.state_dir / 'launch.json', launch)
        (self.a.state_dir / ('launch-' + nonce + '.claim')).touch()
        self.stop()
        proof = {'process': {'pid': 42}}
        atomic_json(self.a.state_dir / 'startup.json', {'status': 'idle', 'nonce': nonce, 'close_evidence': proof})
        with self.assertRaisesRegex(ContractError, 'previous execute'):
            self.prepare()
        key = 'runner-' + nonce
        atomic_json(self.a.launcher.journal(self.a.state_dir / 'startup'), {
            key + '-close': {'status': 'completed', 'result': proof, 'evidence': {'launch': key}},
        })
        atomic_json(self.a.state_dir / 'runner-process.json', {
            'nonce': nonce, 'identity': identity, 'pid': 42, 'process_stamp': 'fixture',
        })
        with patch('team_runner.project_services.departed', return_value=False):
            with self.assertRaisesRegex(ContractError, 'Runner exit'):
                self.prepare()
        with patch('team_runner.project_services.departed', return_value=True):
            self.assertEqual(self.prepare()['desired_state'], 'paused')
        self.assertEqual(read_json(old_path), old)

    def test_unknown_control_requires_matching_follow_up_delivery(self):
        self.stop()
        identifier = str(uuid.uuid4())
        request = str(uuid.uuid4())
        guard = {'attempt': str(self.attempt)}
        path = self.a.state_dir / 'commands' / (identifier + '.json')
        atomic_json(path, {'id': identifier, 'action': 'resolve-escalation', 'status': 'unknown',
                           'guard': guard})
        reconcile = str(uuid.uuid4())
        atomic_json(path.with_name(reconcile + '.json'),
                    {'id': reconcile, 'action': 'reconcile', 'status': 'unknown'})
        registry = self.a.launcher.registry(self.attempt)
        agent = registry['agents'][0]
        dispatch = agent['dispatch_id']
        effect = {'status': 'completed', 'evidence': {'scope': guard}, 'confirmed_follow_up': dispatch,
                  'request_id': request, 'args': ['orchestration', 'send', '--run', agent['run_id'],
                                                  '--to', 'dispatch:origin'],
                  'result': {'message': {'id': 'message', 'to_handle': 'dispatch:' + dispatch,
                                         'run_id': agent['run_id']},
                             'mutation': {'requestId': request}}}
        operations = self.a.state_dir / 'operations.json'
        journal = read_json(operations)
        key = 'escalation-resume:' + identifier
        atomic_json(operations, {**journal, key: effect})
        with self.assertRaisesRegex(ContractError, 'Resolve pending controls'):
            self.prepare()
        agent['follow_up_operation'] = identifier
        self.a.launcher.save_registry(self.attempt, registry)
        atomic_json(operations, {**journal, key: {**effect, 'result': {}}})
        with self.assertRaisesRegex(ContractError, 'Resolve pending controls'):
            self.prepare()
        atomic_json(operations, {**journal, key: effect})
        self.assertEqual(self.prepare()['desired_state'], 'paused')

    def test_changed_dependencies_and_manual_completion_cannot_release_gate(self):
        self.stop()
        result = self.prepare()
        state = self.a.load_state()
        self.set_dependencies([])
        with self.assertRaisesRegex(ContractError, 'dependencies changed'):
            goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                                  generation=result['generation'], epoch=result['epoch'])
        self.set_dependencies([self.predecessor])
        tasks = read_json(self.feature)
        tasks['queue'][1]['status'] = 'completed'
        atomic_json(self.feature, tasks)
        with self.assertRaisesRegex(ValueError, 'requires runner acceptance after handoff'):
            self.a.start_coordinator(self.args, state, self.gate, 'spec', self.gate)
        self.assertIsNone(self.a.load_state()['current'])

    def test_pending_writes_resume_with_same_uuid(self):
        self.stop()
        scope = self.scope()
        state = self.a.load_state()
        real_write = goal_scope.atomic_json
        for boundary in ('pin', 'journal', 'archive', 'state', 'intent', 'receipt'):
            def write(path, value):
                if (boundary == 'archive' and path.parent.name == 'goal-authorizations'
                        or boundary == 'pin' and path.name == '.keep'
                        or boundary == 'journal' and path.parent.name == 'commands' and value.get('status') == 'pending'
                        or boundary == 'intent' and path.name == 'goal-intent.json'
                        or boundary == 'receipt' and value.get('status') == 'succeeded'):
                    raise OSError('fixture interruption')
                return real_write(path, value)
            with self.subTest(boundary=boundary), patch.object(goal_scope, 'atomic_json', side_effect=write):
                if boundary == 'state':
                    with patch.object(self.a, 'save_state', side_effect=OSError('fixture interruption')):
                        with self.assertRaisesRegex(OSError, 'fixture interruption'):
                            self.prepare(scope=scope)
                else:
                    with self.assertRaisesRegex(OSError, 'fixture interruption'):
                        self.prepare(scope=scope)
            if boundary in ('pin', 'journal'):
                self.assertEqual((self.attempt / '.keep').exists(), boundary == 'journal')
                self.assertEqual(self.a.load_state(), state)
                with self.assertRaisesRegex(ContractError, 'No operation'):
                    application.operation(self.a, self.identifier)
                continue
            receipt = application.operation(self.a, self.identifier)
            self.assertEqual(receipt['status'], 'pending')
            if boundary != 'receipt':
                self.assertIsNone(receipt['result'])
                with self.assertRaisesRegex(ContractError, 'pending dependency handoff'):
                    goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                                          generation=scope['generation'], epoch=scope['epoch'])
        prepared = goal_intent.read(self.a)
        resumed = goal_intent.authorize(self.a, {'resume': True}, str(uuid.uuid4()),
                                        generation=prepared['generation'], epoch=prepared['epoch'])
        with lock(self.a.state_dir / 'runner.lock'):
            result = self.prepare(scope=scope)
        self.assertEqual(result['generation'], prepared['generation'])
        self.assertTrue(goal_intent.permitted(self.a, resumed['generation'], resumed['epoch']))
        self.assertEqual(self.a.load_state()['history'], state['history'])
        self.assertEqual(len(self.a.load_state()['attempts']), 1)

    def test_cli_dry_run_and_prepare(self):
        self.stop()
        scope = self.scope()
        args = ['prepare-dependency-handoff', '--project', self.a.context.project_id,
                '--workspace', self.a.context.workspace_id, '--predecessor', self.predecessor, '--json']
        for key, value in scope.items():
            args.extend(('--' + key, str(value)))
        with patch.object(cli, 'Catalog', return_value=self.catalog), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main([*args, '--dry-run']), 0, output.getvalue())
            self.assertEqual(cli.main([*args, '--request-id', self.identifier]), 0, output.getvalue())
        self.assertIn('"desired_state": "paused"', output.getvalue())


if __name__ == '__main__':
    unittest.main()
