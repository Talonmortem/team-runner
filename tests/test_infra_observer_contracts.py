"""Observer safety contracts; isolated temporary catalogs, no live Orca or goals."""
import datetime
import copy
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from team_runner.catalog import Catalog
from team_runner.infra_observer import Observer, digest
from team_runner.observer_workflow import ObserverLauncher, Workflow
from team_runner.runner_store import atomic_json, read_json


class ObserverContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root / 'config', self.root / 'state')
        self.service_project, self.service_repo, self.service_workspace = self._workspace('observer')
        self.target_project, self.target_repo, self.target_workspace = self._workspace('target')
        self.now = 1200.0
        self.observer = Observer(self.catalog, clock=lambda: self.now)
        self.config = {
            'project_id': self.service_project, 'workspace_id': self.service_workspace,
            'projects': [self.target_project], 'mode': 'approval',
            'repair_roots': [{'path': str(self.target_repo), 'kind': 'test', 'shared': False}],
            'roles': {},
        }

    def _workspace(self, name):
        repo = self.root / name
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (repo / 'src/team_runner').mkdir(parents=True)
        (repo / 'src/team_runner/infra_observer.py').write_text('# fixture marker\n')
        project = self.catalog.project(name)
        workspace = self.catalog.workspace(project['id'], {
            'path': str(repo), 'orca_id': 'fixture-' + name, 'orca_instance': 'instance-' + name,
        })
        return project['id'], repo, workspace['id']

    def state(self):
        state = self.observer.load()
        state.update(config=self.config.copy(), desired='running', paused=False)
        return state

    def save_state(self, state):
        atomic_json(self.observer.path, state)

    def target_state_path(self):
        return self.catalog.state_dir(self.target_project, self.target_workspace) / 'state.json'

    def attempt(self):
        attempt = self.catalog.state_dir(self.target_project, self.target_workspace) / 'attempts' / str(uuid.uuid4())
        (attempt / 'verification').mkdir(parents=True)
        atomic_json(self.target_state_path(), {'current': {'attempt_dir': str(attempt)}})
        return attempt

    def test_malformed_and_non_object_workspace_state_stay_unknown(self):
        for body in ('{broken json', '[]'):
            with self.subTest(body=body):
                state = self.state()
                path = self.target_state_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                self.observer.observe(state)
                cursor = state['cursors'][str(path)]
                self.assertEqual(cursor['value'], 'unknown')
                self.assertTrue(any(row['path'] == str(path) and row['value'] == 'unknown'
                                    for row in state['summary']))

    def test_unchanged_state_and_check_files_are_not_read_again(self):
        attempt = self.attempt()
        check = attempt / 'verification/check.json'
        proof = Path(str(check) + '.preflight.json')
        atomic_json(check, {'inputs': {'command': ['python', '-m', 'unittest']},
                            'status': 'executed', 'started_at': 1100})
        atomic_json(proof, {'result': 'PASS'})
        state = self.state()
        self.observer.observe(state)
        targets = {self.target_state_path(), check, proof}
        original = Path.read_text
        seen = []

        def counted(path, *args, **kwargs):
            if path in targets:
                seen.append(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', counted):
            self.observer.observe(state)
        self.assertEqual(seen, [])

    def test_periodic_review_is_hourly_persists_pending_and_uses_only_new_sources(self):
        atomic_json(self.target_state_path(), {'paused': False})
        state = self.state()
        state['semantic_at'] = self.now
        self.observer.observe(state)
        self.assertTrue(state['semantic_pending'])
        self.save_state(state)
        state = self.observer.load()
        for elapsed in (600, 3599):
            self.now = 1200 + elapsed
            self.observer.observe(state)
            self.assertEqual(state['incidents'], {})
        self.now = 4800
        self.observer.observe(state)
        incident = next(iter(state['incidents'].values()))
        self.assertEqual(incident['kind'], 'semantic_review')
        self.assertEqual(len(incident['evidence']), 1)
        self.assertFalse(state['semantic_pending'])
        self.assertEqual(state['summary'], [])

        self.now = 8400
        self.observer.observe(state)
        self.assertEqual(len(incident['evidence']), 1)
        atomic_json(self.target_state_path(), {'paused': True})
        self.observer.observe(state)
        latest = incident['evidence'][-1]['summary']
        self.assertEqual(len(incident['evidence']), 2)
        self.assertEqual([row['path'] for row in latest], [str(self.target_state_path())])
        self.assertTrue(latest[0]['value']['paused'])

    def test_timer_only_updates_and_identical_rewrites_do_not_schedule_review(self):
        attempt = self.attempt()
        agents = attempt / 'agents.json'
        atomic_json(agents, {'agents': [{'id': 'worker', 'status': 'active', 'role': 'tester'}]})
        timing = self.target_state_path().with_name('state-time.json')
        original = {'last': {'at': self.now, 'kind': 'checks', 'scope': {'generation': 'one'}},
                    'seconds': {'checks': 10}}
        atomic_json(timing, original)
        state = self.state()
        state['semantic_at'] = self.now
        self.observer.observe(state)
        self.now = 4800
        self.observer.observe(state)
        before = copy.deepcopy(state['incidents'])
        atomic_json(agents, {'agents': [{'id': 'worker', 'status': 'active', 'role': 'tester'}],
                             'heartbeat_at': 5000})
        timer_only = copy.deepcopy(original)
        timer_only['last']['at'] = 5000
        timer_only['seconds']['checks'] = 3810
        atomic_json(timing, timer_only)
        self.now = 8400
        self.observer.observe(state)
        self.assertEqual(state['incidents'], before)
        self.assertFalse(state['semantic_pending'])
        self.assertEqual(state['summary'], [])
        self.assertEqual(state['cursors'][str(timing)]['value']['seconds']['checks'], 3810)

        timer_only['last']['kind'] = 'network_wait'
        atomic_json(timing, timer_only)
        self.observer.observe(state)
        incident = next(iter(state['incidents'].values()))
        self.assertEqual(len(incident['evidence']), 2)
        self.assertEqual(incident['evidence'][-1]['summary'][0]['value']['last']['kind'], 'network_wait')
        self.now += 1
        timer_only['last']['scope']['generation'] = 'two'
        atomic_json(timing, timer_only)
        self.observer.observe(state)
        self.assertTrue(state['semantic_pending'])

    def test_pending_summary_preserves_fault_and_clear_transition(self):
        state = self.state()
        state['semantic_at'] = self.now
        atomic_json(self.target_state_path(), {'last_error': 'first failure'})
        self.observer.observe(state)
        atomic_json(self.target_state_path(), {'last_error': None})
        self.observer.observe(state)
        rows = [row for row in state['summary'] if row['path'] == str(self.target_state_path())]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['value']['last_error'], 'first failure')
        self.assertIsNone(rows[1]['value']['last_error'])
        self.now += 3600
        self.observer.observe(state)
        evidence = next(iter(state['incidents'].values()))['evidence'][-1]['summary']
        self.assertIn(rows[0], evidence)
        self.assertIn(rows[1], evidence)

    def test_pending_summary_overflow_is_counted_until_durable_capture(self):
        state = self.state()
        state['semantic_at'] = self.now
        self.observer.observe(state)
        state.update(summary=[], summary_omitted=0, semantic_pending=False)
        for index in range(105):
            atomic_json(self.target_state_path(), {'last_error': f'fault-{index}'})
            self.observer.observe(state)
        self.assertEqual(len(state['summary']), 100)
        self.assertEqual(state['summary_omitted'], 5)
        self.save_state(state)
        state = self.observer.load()
        self.assertEqual(state['summary_omitted'], 5)
        self.now += 3600
        self.observer.observe(state)
        evidence = next(iter(state['incidents'].values()))['evidence'][-1]
        self.assertEqual(len(evidence['summary']), 100)
        self.assertEqual(evidence['omitted_summary_rows'], 5)
        self.assertEqual(evidence['unavailable'], 'unknown')
        self.assertEqual(state['summary_omitted'], 0)
        self.assertEqual(state['summary'], [])
        self.observer.observe(state)
        self.assertEqual(next(iter(state['incidents'].values()))['evidence'][-1], evidence)

    def test_repeated_checks_require_native_preflight_and_keep_execution_times(self):
        attempt = self.attempt()
        inputs = {'manifest_inputs': 'same-frozen-inputs', 'command': ['python', '-m', 'unittest'],
                  'toolchain': 'python-3.11', 'environment': {}}
        for index, started in enumerate((900, 1000, 1100)):
            check = attempt / 'verification' / f'check-{index}.json'
            atomic_json(check, {'id': f'check-{index}', 'inputs': inputs, 'status': 'executed',
                                'result': 'FAIL', 'started_at': started, 'finished_at': started + 5})
            atomic_json(str(check) + '.preflight.json', {'result': 'PASS', 'inputs': 'same-frozen-inputs'})
        state = self.state()
        self.observer.observe(state)
        repeated = [row for row in state['incidents'].values() if row['kind'].startswith('repeated_check:')]
        self.assertEqual(len(repeated), 1)
        executions = repeated[0]['evidence'][0]['executions']
        self.assertEqual([row['at'] for row in executions], [900, 1000, 1100])

        state = self.state()
        attempt = self.attempt()
        for index, started in enumerate((900, 1000, 1100)):
            check = attempt / 'verification' / f'check-{index}.json'
            atomic_json(check, {'id': f'check-{index}', 'inputs': inputs, 'status': 'executed',
                                'result': 'FAIL', 'started_at': started})
            atomic_json(str(check) + '.preflight.json', {'result': 'FAIL'})
        self.observer.observe(state)
        self.assertFalse(any(row['kind'].startswith('repeated_check:') for row in state['incidents'].values()))

    def test_network_failure_signal_keeps_producer_times_and_ignores_opaque_ids(self):
        state = self.state()
        path = self.catalog.state_dir(self.target_project, self.target_workspace) / 'network-recovery.json'
        path.parent.mkdir(parents=True, exist_ok=True)

        def timestamp(value):
            return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()

        atomic_json(path, {'sessions': {'one': {'failure': {'id': timestamp(900), 'error': 'network_error'}}}})
        self.observer.observe(state)
        self.now = 1201
        atomic_json(path, {'sessions': {
            'one': {'failure': {'id': timestamp(900), 'error': 'network_error'}},
            'two': {'failure': {'id': timestamp(1100), 'error': 'network_error'}},
        }})
        self.observer.observe(state)
        incident = next(row for row in state['incidents'].values() if row['kind'].startswith('infrastructure:'))
        self.assertEqual([row['at'] for row in incident['evidence'][0]['failures']], [900, 1100])

        atomic_json(path, {'sessions': {
            'one': {'failure': {'id': timestamp(900), 'error': 'network_error'}},
            'two': {'failure': {'id': timestamp(1100), 'error': 'network_error'}},
            'three': {'failure': {'id': 'opaque-worker-error-id', 'error': 'network_error'}},
        }})
        self.now = 1202
        self.observer.observe(state)
        key = digest({'source': {'project': self.target_project, 'workspace': self.target_workspace},
                      'error': 'network_error'})
        self.assertEqual([row['at'] for row in state['failure_history'][key]], [900, 1100])

    def test_new_evidence_invalidates_prior_approval_and_plan(self):
        state = self.state()
        source = {'project': self.target_project, 'workspace': self.target_workspace}
        incident = self.observer._incident(state, source, 'test', {'signal': 'same-inputs'})
        incident['plan'] = {'files': [], 'evidence': digest(incident['evidence']), 'policy': digest(self.config)}
        incident['status'] = 'approval'
        prior_plan = digest(incident['plan'])
        self.save_state(state)

        incident = self.observer._incident(state, source, 'test', {'signal': 'new-inputs'})
        self.assertEqual(incident['status'], 'detected')
        self.assertNotIn('approval', incident)
        self.save_state(state)
        with self.assertRaisesRegex(ValueError, 'stale incident approval'):
            self.observer.command('approve', str(uuid.uuid4()), self.observer.scope(state),
                                  {'incident': incident['id'], 'plan': prior_plan})

    def test_service_snapshot_and_agent_sandbox_exclude_registry_and_workspace(self):
        state = self.state()
        incident = {'id': str(uuid.uuid4()), 'evidence': [{'signal': 'test'}]}
        workflow = Workflow(self.observer)
        workflow._snapshot(state, incident)
        attempt = Path(incident['attempt'])
        self.assertTrue(workflow._intact(incident))
        self.assertTrue((attempt / 'agents.json').parent == attempt)

        launcher = ObserverLauncher(self.observer.context(state), self.observer)
        launcher.stage = 'review'
        role = {'agent': 'codex', 'profile': '', 'provider': 'codex-lb',
                'model': 'gpt-6.1-sol', 'effort': 'high'}
        command = launcher.argv(role, attempt)
        encoded_roots = next(value.split('=', 1)[1] for value in command
                             if value.startswith('sandbox_workspace_write.writable_roots='))
        roots = set(json.loads(encoded_roots))
        self.assertEqual(roots, {str(attempt / 'work/review'), str(attempt / 'reports'),
                                 str(Path.home() / '.config/orca')})
        self.assertNotIn(str(attempt), roots)
        self.assertNotIn(str(self.service_repo), roots)
        self.assertNotIn(str(self.catalog.state_root), roots)
        self.assertNotIn(str(self.catalog.config_root), roots)
        self.assertIn('--cd', command)
        self.assertEqual(command[command.index('--cd') + 1], str(attempt / 'work/review'))

    def test_compact_snapshot_bounds_legacy_history_and_keeps_frozen_full_evidence(self):
        state = self.state()
        huge = 'fault details ' + '\u044f' * 100000
        rows = [{'path': '/target/agents.json', 'value': {'agents': [
            {'id': f'agent-{index}', 'status': 'failed', 'error': huge} for index in range(40)]}},
            {'path': '/target/coordinator-events.json', 'value': {'events': [
                {'id': 'event-one', 'status': 'failed', 'error': 'network error',
                 'payload': {'spec': 'NEVER_EMBED_SPEC' + huge}}]}},
            {'path': '/target/verification/check.json', 'value': {
                'status': 'executed', 'result': 'FAIL', 'inputs': {'command': ['python', 'bounded-check.py']}}}]
        incident = {'id': str(uuid.uuid4()), 'kind': 'semantic_review', 'status': 'detected',
                    'evidence': [{'historical': huge}] * 20 + [{'summary': rows}],
                    'actions': [{'spec': huge}] * 20, 'stage_history': [{'spec': huge}] * 20,
                    'diagnosis': 'prior diagnosis', 'results': []}
        original_evidence = copy.deepcopy(incident['evidence'])
        workflow = Workflow(self.observer)
        workflow._snapshot(state, incident)
        attempt = Path(incident['attempt'])
        compact = read_json(attempt / 'observer-input.json')
        self.assertLessEqual((attempt / 'observer-input.json').stat().st_size, 64 * 1024)
        self.assertNotIn('NEVER_EMBED_SPEC', (attempt / 'observer-input.json').read_text())
        self.assertNotIn('actions', compact['incident'])
        self.assertNotIn('stage_history', compact['incident'])
        self.assertEqual(compact['previous']['diagnosis'], 'prior diagnosis')
        reference = compact['full_evidence']
        self.assertEqual(reference['evidence_count'], 21)
        self.assertEqual(reference['selected_from_index'], 20)
        self.assertEqual(reference['omitted_history']['actions'], 20)
        self.assertEqual(read_json(reference['path'])['evidence'], original_evidence)
        self.assertEqual(read_json(reference['path'])['policy'], self.config)
        self.assertEqual(incident['evidence'], original_evidence)
        self.assertTrue(workflow._intact(incident))
        archive = Path(reference['path'])
        archive.write_text('{}')
        self.assertFalse(workflow._intact(incident))

    def test_snapshot_evidence_cursor_excludes_prior_review_and_marks_new_oversized_data(self):
        state = self.state()
        incident = {'id': str(uuid.uuid4()), 'kind': 'test', 'status': 'detected',
                    'input_evidence_count': 1, 'evidence': [{'old_fact': 'reviewed'},
                        {'new_fact': 'new failure', 'long_detail': 'x' * 200000}], 'actions': []}
        workflow = Workflow(self.observer)
        workflow._snapshot(state, incident)
        compact = read_json(Path(incident['attempt']) / 'observer-input.json')
        self.assertNotIn('old_fact', compact['evidence'][0])
        self.assertEqual(compact['evidence'][0]['new_fact'], 'new failure')
        self.assertIn('omitted_characters', compact['evidence'][0]['long_detail'])
        self.assertEqual(incident['input_evidence_count'], 2)

    def test_interleaved_pause_or_diagnostic_mode_prevents_new_dispatch(self):
        class Launcher:
            stage = None
            workspace = None

            def __init__(self):
                self.dispatches = []

            def dispatch_role(self, *args, **kwargs):
                self.dispatches.append((args, kwargs))
                return {'id': 'agent', 'dispatch_id': 'dispatch'}

        for command, stage in (('pause', 'diagnose'), ('mode', 'candidate')):
            with self.subTest(command=command, stage=stage):
                state = self.state()
                attempt = self.root / ('attempt-' + stage)
                attempt.mkdir()
                incident = {'id': str(uuid.uuid4()), 'source': {}, 'kind': 'test',
                            'evidence': [{'signal': 'fixture'}], 'evidence_hashes': [digest({'signal': 'fixture'})],
                            'status': 'ready', 'attempt': str(attempt), 'attempts': 1,
                            'actions': [], 'stages': {}, 'plan': {'files': []}}
                state['incidents'] = {incident['id']: copy.deepcopy(incident)}
                state['active'] = incident['id']
                self.save_state(state)
                stale_state = copy.deepcopy(state)
                stale_incident = copy.deepcopy(incident)

                current_scope = self.observer.scope()
                payload = {'mode': 'diagnostic'} if command == 'mode' else {}
                self.observer.command(command, str(uuid.uuid4()), current_scope, payload)

                launcher = Launcher()
                Workflow(self.observer)._dispatch(stale_state, stale_incident, stage, launcher)
                current = self.observer.load()
                self.assertEqual(launcher.dispatches, [])
                self.assertEqual(current['paused'], command == 'pause')
                if command == 'mode':
                    self.assertEqual(current['config']['mode'], 'diagnostic')

    def test_late_stage_merge_keeps_new_evidence_and_does_not_restore_approval(self):
        state = self.state()
        source = {'project': self.target_project, 'workspace': self.target_workspace}
        incident = self.observer._incident(state, source, 'test', {'signal': 'old'})
        incident['plan'] = {'files': [], 'evidence': digest(incident['evidence']), 'policy': digest(self.config)}
        incident['approval'] = {'plan': digest(incident['plan']), 'policy': digest(self.config),
                                'request_id': str(uuid.uuid4()), 'evidence': digest(incident['evidence'])}
        incident['status'] = 'ready'
        self.save_state(state)
        stale_report = copy.deepcopy(incident)
        stale_report['stages'] = {'diagnose': {'status': 'completed', 'output_sha256': 'late-report'}}

        latest = self.observer.load()
        self.observer._incident(latest, source, 'test', {'signal': 'new'})
        self.save_state(latest)
        Workflow(self.observer)._save_merged(stale_report)

        merged = self.observer.load()['incidents'][incident['id']]
        self.assertEqual(merged['evidence'], [{'signal': 'old'}, {'signal': 'new'}])
        self.assertTrue(merged.get('new_evidence'))
        self.assertNotIn('approval', merged)
        self.assertNotEqual(merged['plan']['evidence'], digest(merged['evidence']))
        self.assertNotEqual(merged['status'], 'ready')


if __name__ == '__main__':
    unittest.main()
