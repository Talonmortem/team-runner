"""Observer workflow tests use isolated catalogs, files and exact fake Orca receipts."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from team_runner.catalog import Catalog
from team_runner.infra_observer import Observer, digest, file_digest
from team_runner.observer_workflow import Workflow, ObserverLauncher
from team_runner.runner_config import defaults
from team_runner.runner_launch import Launcher
from team_runner.runner_store import atomic_json, read_json


def uid():
    return str(uuid.uuid4())


class FakeLauncher(Launcher):
    def __init__(self, context, observer):
        super().__init__(context)
        self.observer = observer
        self.fail_review = False
        self.fail_result = False
        self.launches = []

    def orca(self, *args, **kwargs):
        raise AssertionError('unexpected transport: ' + str(args))

    def dispatch_role(self, attempt, role, spec, title, state_dir, **kwargs):
        attempt = Path(attempt)
        incident = self.observer.load()['incidents'][self.observer.load()['active']]
        stage = self.stage
        self.launches.append(stage)
        output = Path(incident['stages'][stage]['output'])
        check = output.parent / 'check.txt'; check.write_text('independent evidence')
        checks = [{'argv': ['python3', 'isolated-check.py'], 'exit_code': 0, 'evidence': str(check.relative_to(attempt))}]
        if stage == 'diagnose':
            data = {'action': 'repair', 'diagnosis': 'fixture infrastructure input broken', 'scope': 'test',
                    'files': [str(self.observer.fixture)], 'reason': 'remove confirmed fixture failure'}
        elif stage == 'candidate':
            artifact = output.parent / 'replacement'
            artifact.write_text('fixed\n')
            data = {'files': [{'path': str(self.observer.fixture), 'before': incident['plan']['files'][0]['before'],
                              'artifact': str(artifact.relative_to(attempt)), 'after': file_digest(artifact)}], 'explanation': 'fixture correction'}
        elif stage == 'review':
            data = {'verdict': 'FAIL' if self.fail_review else 'PASS', 'candidate': digest(incident['candidate']), 'correctness': 'independent proof',
                    'checks': checks, 'safe': True, 'users_absent': True, 'users_evidence': 'complete isolated fixture inventory'}
        else:
            data = {'verdict': 'FAIL' if self.fail_result else 'PASS', 'candidate': digest(incident['candidate']),
                    'correctness': True, 'fault_removed': True, 'before': 1, 'after': 0, 'metric': 'fixture validation failures', 'checks': checks}
        atomic_json(output, data)
        record = {'id': uid(), 'role': role, 'task_id': uid(), 'run_id': uid(), 'dispatch_id': uid(),
                  'terminal': 'owned-terminal', 'status': 'active', 'function': kwargs.get('function', stage)}
        reg = self.registry(attempt); reg['agents'].append(record); self.save_registry(attempt, reg)
        payload = {'outcome': 'succeeded', 'summary': 'stage complete', 'checks': [str(output.relative_to(attempt))]}
        atomic_json(attempt / 'reports' / (record['id'] + '-summary.json'), payload)
        atomic_json(attempt / 'reports' / (record['id'] + '-summary.completion.json'), {'worker_done': {
            'status': 'completed', 'args': ['orchestration', 'send', '--from', record['terminal'], '--task-id', record['task_id'], '--dispatch-id', record['dispatch_id']]}})
        return record

    def observe_agent(self, record):
        return {'dispatch': {'id': record['dispatch_id'], 'taskId': record['task_id'], 'runId': record['run_id'], 'status': 'completed'}}

    def release_agent(self, record, attempt=None, accepted_done=None):
        record['status'] = 'released'


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        repo = self.root / 'runner'; repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        marker = repo / 'src/team_runner/infra_observer.py'; marker.parent.mkdir(parents=True); marker.write_text('# fixture')
        self.fixture = self.root / 'infrastructure' / 'fixture.txt'; self.fixture.parent.mkdir(); self.fixture.write_text('broken\n')
        self.catalog = Catalog(self.root / 'config', self.root / 'state')
        self.project, self.workspace, self.observed, self.observed_workspace = uid(), uid(), uid(), uid()
        def project(name, workspace):
            return {'id': name, 'name': name, 'profile': {}, 'roles': {}, 'workspaces': {workspace: {
                'id': workspace, 'name': workspace, 'path': str(repo), 'disabled': False, 'profile': {}, 'roles': {},
                'orca_id': 'explicit-fixture', 'orca_instance': 'fixture-incarnation'}}}
        atomic_json(self.catalog.path, {'version': 1, 'projects': {self.project: project(self.project, self.workspace),
                                                               self.observed: project(self.observed, self.observed_workspace)}})
        cfg = defaults()['roles']['explorer']
        self.config = {'project_id': self.project, 'workspace_id': self.workspace, 'projects': [self.observed], 'mode': 'auto',
                       'repair_roots': [{'path': str(self.fixture.parent), 'kind': 'test', 'shared': False}],
                       'roles': {stage: copy.deepcopy(cfg) for stage in ('diagnose', 'candidate', 'review', 'result')}}
        self.observer = Observer(self.catalog, clock=lambda: 1000)
        self.observer.fixture = self.fixture
        with patch('team_runner.runner_config.validate'):
            self.command('configure', self.config)
        self.command('start')
        state = self.observer.load()
        self.incident = self.observer._incident(state, {'project': self.observed}, 'fixture_failure', {'error': 'broken fixture'})['id']
        atomic_json(self.observer.path, state)
        self.launcher = FakeLauncher(self.observer.context(), self.observer)
        self.workflow = Workflow(self.observer, self.launcher)

    def command(self, action, payload=None, request=None):
        return self.observer.command(action, request or uid(), self.observer.scope(), payload)

    def advance(self, times=1):
        for _ in range(times):
            self.workflow.advance()
        return self.observer.load()['incidents'][self.incident]

    def test_complete_cycle_changes_only_after_independent_review(self):
        self.advance(6)
        self.assertEqual(self.fixture.read_text(), 'broken\n')
        incident = self.advance(1)
        self.assertEqual(incident['status'], 'checking_result')
        self.assertEqual(self.fixture.read_text(), 'fixed\n')
        incident = self.advance(2)
        self.assertEqual(incident['status'], 'closed')
        self.assertEqual(self.launcher.launches, ['diagnose', 'candidate', 'review', 'result'])
        self.assertEqual(incident['attempts'], 1)

    def test_diagnostic_never_prepares_or_applies(self):
        self.command('mode', {'mode': 'diagnostic'})
        incident = self.advance(5)
        self.assertEqual(incident['status'], 'suppressed')
        self.assertEqual(self.fixture.read_text(), 'broken\n')
        self.assertEqual(self.launcher.launches, ['diagnose'])

    def test_approval_before_candidate_and_stale_approval_rejected(self):
        self.command('mode', {'mode': 'approval'})
        incident = self.advance(3)
        self.assertEqual(incident['status'], 'approval')
        self.assertEqual(self.launcher.launches, ['diagnose'])
        with self.assertRaises(ValueError):
            self.command('approve', {'incident': self.incident, 'plan': 'wrong'})
        self.command('approve', {'incident': self.incident, 'plan': digest(incident['plan'])})
        self.assertEqual(self.advance(7)['status'], 'closed')

    def test_pause_after_candidate_prevents_apply(self):
        self.advance(6)
        self.command('pause')
        self.advance(3)
        self.assertEqual(self.fixture.read_text(), 'broken\n')
        self.assertEqual(self.launcher.launches, ['diagnose', 'candidate', 'review'])

    def test_changed_input_blocks_apply(self):
        self.advance(6)
        self.fixture.write_text('concurrent user edit\n')
        incident = self.advance(1)
        self.assertEqual(incident['status'], 'blocked')
        self.assertEqual(self.fixture.read_text(), 'concurrent user edit\n')

    def test_failed_result_not_closed(self):
        self.launcher.fail_result = True
        incident = self.advance(9)
        self.assertEqual(incident['status'], 'blocked')
        self.assertIn('not successful', incident['blocker'])

    def test_result_source_changed_before_acceptance_not_closed(self):
        self.advance(8)
        self.fixture.write_text('changed after result report\n')
        self.assertEqual(self.advance(1)['status'], 'blocked')

    def test_stop_after_apply_still_independently_confirms_result(self):
        self.advance(7)
        self.command('stop')
        self.assertEqual(self.advance(2)['status'], 'closed')
        self.assertEqual(self.launcher.launches[-1], 'result')

    def test_repeated_command_id_conflicts_and_stale_scope(self):
        scope = self.observer.scope(); request = uid()
        result = self.observer.command('pause', request, scope)
        self.assertEqual(result, self.observer.command('pause', request, scope))
        with self.assertRaises(ValueError):
            self.observer.command('start', request, scope)
        with self.assertRaises(ValueError):
            self.observer.command('start', uid(), scope)

    def test_new_evidence_requires_fresh_candidate_and_independent_review(self):
        old = self.advance(6)
        self.assertIn('review', old)
        state = self.observer.load()
        self.observer._incident(state, {'project': self.observed}, 'fixture_failure', {'error': 'new fixture evidence'})
        atomic_json(self.observer.path, state)
        self.advance(1)
        incident = self.observer.load()['incidents'][self.incident]
        self.assertNotIn('candidate', incident)
        self.assertNotIn('review', incident)
        self.assertNotIn('application', incident)
        self.assertEqual(self.advance(9)['status'], 'closed')
        self.assertEqual(self.launcher.launches, ['diagnose', 'candidate', 'review', 'diagnose', 'candidate', 'review', 'result'])

    def test_lost_dispatch_receipt_rebinds_exact_registered_worker(self):
        original = self.launcher.dispatch_role
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('lost transport receipt')
        with patch.object(self.launcher, 'dispatch_role', side_effect=lost):
            incident = self.advance(1)
        self.assertEqual(incident['stages']['diagnose']['status'], 'dispatching')
        self.advance(1)
        self.assertEqual(self.launcher.launches, ['diagnose'])
        self.assertEqual(self.observer.load()['incidents'][self.incident]['status'], 'ready')

    def test_real_lost_worker_start_receipt_rehydrates_dispatch_registration(self):
        original = self.launcher.dispatch_role
        dispatches = []
        def lost(*args, **kwargs):
            record = original(*args, **kwargs)
            dispatches.append(record['dispatch_id'])
            attempt = Path(args[0])
            reg = self.launcher.registry(attempt)
            agent = reg['agents'][-1]
            agent.pop('dispatch_id'); agent.pop('terminal'); agent['status'] = 'unknown'
            reg['fenced'] = True
            reg['launch_requests'] = [{'id': uid(), 'task_id': record['task_id'], 'status': 'ready', 'params': {'function': kwargs['function']}}]
            self.launcher.save_registry(attempt, reg)
            atomic_json(self.launcher.journal(attempt), {
                record['id'] + '-terminal': {'status': 'completed', 'result': {'terminal': {
                    'handle': record['terminal'], 'incarnationId': 'exact-incarnation', 'worktreeId': self.launcher.context.orca_id}}},
                record['id'] + '-dispatch': {'status': 'unknown', 'request_id': uid(),
                    'args': ['orchestration', 'worker-start', '--task', record['task_id'], '--run', record['run_id'], '--terminal', record['terminal']]}})
            raise RuntimeError('worker-start receipt lost before registry save')
        with patch.object(self.launcher, 'dispatch_role', side_effect=lost):
            self.advance(1)
        def receipt(*args, **kwargs):
            if args[:2] == ('orchestration', 'request-show'):
                return {'state': 'completed', 'receipt': {'dispatchId': dispatches[0]}}
            raise AssertionError(args)
        with patch.object(self.launcher, 'orca', side_effect=receipt):
            incident = self.advance(1)
        self.assertEqual(incident['status'], 'ready')
        agent = self.launcher.registry(incident['attempt'])['agents'][0]
        self.assertEqual(agent['dispatch_id'], dispatches[0])
        self.assertEqual(agent['status'], 'released')
        self.assertEqual(self.launcher.launches, ['diagnose'])
        self.assertEqual(self.launcher.registry(incident['attempt'])['launch_requests'][0]['status'], 'done')
        self.advance(1)
        self.assertEqual(self.launcher.launches, ['diagnose', 'candidate'])

    def test_lost_worker_close_receipt_settles_without_second_close(self):
        incident = self.advance(1)
        attempt = Path(incident['attempt'])
        agent = self.launcher.registry(attempt)['agents'][0]
        reg = self.launcher.registry(attempt); reg['agents'][0]['incarnation'] = 'close-incarnation'; self.launcher.save_registry(attempt, reg)
        atomic_json(self.launcher.journal(attempt), {agent['id'] + '-close': {
            'status': 'unknown', 'request_id': uid(), 'args': ['terminal', 'close', '--terminal', agent['terminal']],
            'evidence': {'terminal': agent['terminal'], 'incarnation': 'close-incarnation'}}})
        def observed(*args, **kwargs):
            if args[:2] == ('terminal', 'list'):
                return {'terminals': []}
            raise AssertionError(args)
        with patch.object(self.launcher, 'orca', side_effect=observed), patch.object(self.launcher, 'release_agent') as release:
            incident = self.advance(1)
        release.assert_not_called()
        self.assertEqual(incident['status'], 'ready')
        self.assertEqual(self.launcher.registry(attempt)['agents'][0]['status'], 'released')

    def test_second_candidate_lost_receipt_never_binds_old_agent(self):
        original = self.launcher.dispatch_role
        candidate_count = 0
        def candidate(*args, **kwargs):
            nonlocal candidate_count
            record = original(*args, **kwargs)
            if self.launcher.stage == 'candidate':
                candidate_count += 1
                if candidate_count == 1:
                    path = Path(args[0]) / 'reports' / (record['id'] + '-summary.json')
                    payload = read_json(path); payload['outcome'] = 'failed'; atomic_json(path, payload)
                elif candidate_count == 2:
                    raise RuntimeError('second attempt receipt lost')
            return record
        with patch.object(self.launcher, 'dispatch_role', side_effect=candidate):
            self.advance(5)
        incident = self.advance(1)
        self.assertEqual(incident['stages']['candidate']['status'], 'completed')
        self.assertEqual(incident['stage'], 'review')
        self.assertEqual(self.advance(5)['status'], 'closed')
        self.assertEqual(candidate_count, 2)

    def test_stop_during_dispatched_diagnosis_settles_without_candidate(self):
        self.advance(1)
        self.command('stop')
        self.advance(2)
        self.assertEqual(self.launcher.launches, ['diagnose'])
        self.assertEqual(self.fixture.read_text(), 'broken\n')

    def test_terminal_incident_closes_owned_service_mailbox(self):
        self.command('mode', {'mode': 'diagnostic'})
        incident = self.advance(1)
        reg = self.launcher.registry(incident['attempt'])
        reg['runs'] = [{'run_id': uid(), 'mailbox': 'owned-mailbox', 'incarnation': uid(), 'status': 'ready'}]
        self.launcher.save_registry(incident['attempt'], reg)
        with patch.object(self.launcher, 'close_mailbox') as close:
            self.advance(2)
        close.assert_called_once()
        self.assertEqual(self.launcher.registry(incident['attempt'])['runs'][0]['status'], 'closed')

    def test_missing_registered_agent_inventory_blocks_application(self):
        incident = self.advance(6)
        context = self.catalog.context(self.observed, self.observed_workspace)
        attempt = context.state_dir / 'attempts' / uid()
        attempt.mkdir(parents=True)
        atomic_json(context.state_dir / 'state.json', {'current': {'attempt_dir': str(attempt)}})
        atomic_json(attempt / 'context.json', {'identity': context.identity()})
        atomic_json(attempt / 'agents.json', {})
        incident = self.advance(1)
        self.assertEqual(incident['status'], 'waiting_safe_point')
        self.assertEqual(self.fixture.read_text(), 'broken\n')

    def test_busy_shared_resource_waits_without_modifying_file(self):
        incident = self.advance(6)
        state = self.observer.load()
        state['incidents'][self.incident]['plan']['files'][0]['root']['shared'] = True
        atomic_json(self.observer.path, state)
        with patch('team_runner.heavy_resource.available', return_value=False):
            incident = self.advance(1)
        self.assertEqual(incident['status'], 'waiting_safe_point')
        self.assertEqual(self.fixture.read_text(), 'broken\n')

    def test_retry_budget_persists_and_stage_artifacts_remain_distinct(self):
        original = self.launcher.dispatch_role
        def failed(*args, **kwargs):
            record = original(*args, **kwargs)
            if self.launcher.stage == 'candidate':
                attempt = Path(args[0])
                path = attempt / 'reports' / (record['id'] + '-summary.json')
                payload = read_json(path); payload['outcome'] = 'failed'; atomic_json(path, payload)
            return record
        with patch.object(self.launcher, 'dispatch_role', side_effect=failed):
            incident = self.advance(9)
        self.assertEqual(incident['attempts'], 2)
        self.assertEqual(incident['status'], 'blocked')
        self.assertEqual(self.launcher.launches, ['diagnose', 'candidate', 'candidate'])
        self.assertTrue(Path(incident['attempt'], 'work/1-candidate/result.json').exists())
        self.assertTrue(Path(incident['attempt'], 'work/2-candidate/result.json').exists())
        self.assertEqual(Observer(self.catalog).load()['incidents'][self.incident]['attempts'], 2)
        self.assertEqual(self.fixture.read_text(), 'broken\n')

    def test_lost_command_receipt_preserves_exact_atomic_transition(self):
        request = uid(); scope = self.observer.scope()
        result = self.observer.command('pause', request, scope)
        (self.observer.directory / 'commands' / (request + '.json')).unlink()
        self.assertEqual(result, self.observer.command('pause', request, scope))
        self.assertTrue(self.observer.load()['paused'])

    def test_observer_process_singleton_and_cooperative_stop(self):
        import time
        from team_runner.observer_service import ObserverProcess
        self.command('pause')
        process = ObserverProcess(self.catalog, wait_timeout=2)
        try:
            first = process.ensure(uid())
            second = process.ensure(uid())
            self.assertEqual(first['state'], 'running')
            self.assertTrue(first['confirmed'])
            self.assertEqual(first['nonce'], second['nonce'])
            self.assertEqual(len(process.children), 1)
            self.command('stop')
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                status = process.status()
                if status['state'] == 'stopped' and status['confirmed']:
                    break
                time.sleep(.05)
            self.assertEqual(status['state'], 'stopped')
            self.assertTrue(status['confirmed'])
            self.assertFalse((self.catalog.context(self.observed, self.observed_workspace).state_dir / 'state.json').exists())
        finally:
            for child in process.children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=3)

    def test_cli_observer_parser_and_read_only_status(self):
        from team_runner.cli import parser
        args = parser().parse_args(['observer', 'mode', '--mode', 'approval', '--scope', 'S', '--request-id', uid(), '--json'])
        self.assertEqual(args.action, 'mode')
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.observer.status()
        after = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_service_attempt_isolation_and_snapshot_tamper(self):
        incident = self.advance(1)
        product = self.catalog.context(self.observed, self.observed_workspace)
        self.assertFalse((product.state_dir / 'state.json').exists())
        with self.assertRaises(ValueError):
            product.check_attempt(incident['attempt'])
        Path(incident['attempt'], 'observer-input.json').write_text('{}')
        self.assertEqual(self.advance(1)['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
