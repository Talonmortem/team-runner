"""Isolated contracts for the native cascade Orca adapter."""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from team_runner import cascade_orca_entry, runner_config
from team_runner.cascade_orca import OrcaBackend, ROLE_SETTINGS, _digest
from team_runner.catalog import Context, profile
from team_runner.runner_operations import UncertainOperation
from team_runner.runner_store import atomic_json


class CascadeOrcaTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.checkout = Path(temp.name) / 'source'
        self.checkout.mkdir()
        self.git(self.checkout, 'init')
        self.git(self.checkout, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '--allow-empty', '-m', 'Fixture base')
        self.head = self.git(self.checkout, 'rev-parse', 'HEAD')
        self.child = self.checkout.parent / 'child'
        self.git(self.checkout, 'worktree', 'add', '-b', 'team-runner/cascade-attempt', str(self.child), self.head)
        self.parent = {'id': 'repo::source', 'repoId': 'repo', 'projectId': 'project',
                       'instanceId': 'parent-incarnation'}
        self.context = SimpleNamespace(repo=self.checkout, orca_id=self.parent['id'], verify=lambda _: self.parent)
        self.session = SimpleNamespace(context=self.context, execution='execution')
        self.worktree = {
            'id': 'repo::child', 'instanceId': 'child-incarnation', 'repoId': 'repo', 'projectId': 'project',
            'path': str(self.child), 'head': self.head, 'hostId': 'local', 'isArchived': False,
            'isMainWorktree': False, 'isBare': False, 'branch': 'refs/heads/team-runner/cascade-attempt',
            'parentWorktreeId': self.parent['id'], 'comment': 'execution/feat/attempt',
        }
        self.terminal = {'handle': 'term_worker', 'incarnationId': 'incarnation', 'ptyId': 'pty',
                         'worktreeId': self.worktree['id'], 'tabId': 'tab', 'leafId': 'leaf'}
        self.assignment = {'terminal': 'term_worker', 'terminal_identity': self.terminal,
                           'dispatch': 'ctx_worker', 'task': 'task_worker', 'run': 'run_child',
                           'worktree': self.worktree['id']}
        self.backend = OrcaBackend()
        self.backend.call = Mock(side_effect=self.call)

    @staticmethod
    def git(repo, *args):
        return subprocess.run(['git', '-C', str(repo), *args], text=True,
                              capture_output=True, check=True).stdout.strip()

    @staticmethod
    def inventory(worktrees):
        return {'worktrees': worktrees, 'hostScope': {'hostIds': ['local'], 'omittedHostIds': []},
                'truncated': False, 'totalCount': len(worktrees)}

    def call(self, *args, **kwargs):
        if args[:2] == ('worktree', 'show'):
            return {'worktree': self.worktree}
        if args[:2] == ('worktree', 'list'):
            return self.inventory([self.worktree])
        if args[:2] == ('orchestration', 'worker-show'):
            return {
                'dispatch': {'id': 'ctx_worker', 'runId': 'run_child', 'taskId': 'task_worker',
                             'assigneeHandle': 'term_worker', 'assigneePaneKey': 'tab:leaf',
                             'processIncarnation': 'pty:incarnation', 'status': 'completed'},
                'worker': {'dispatchId': 'ctx_worker', 'agentTerminalHandle': 'term_worker',
                           'state': 'succeeded', 'worktreeId': self.worktree['id']},
                'terminalResource': {'ownerDispatchId': 'ctx_worker', 'ownershipState': 'external'},
            }
        raise AssertionError((args, kwargs))

    def evidence(self):
        return {'repo_id': 'repo', 'project': 'project', 'parent': self.parent['id'],
                'branch': self.worktree['branch'], 'sha': self.head, 'marker': self.worktree['comment']}

    def test_lost_worktree_receipt_recovers_exact_lineage_branch_and_git_base(self):
        self.assertEqual(self.backend._recover_worktree(self.session, self.evidence()),
                         {'worktree': self.worktree})
        self.git(self.child, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '--allow-empty', '-m', 'Moved branch')
        with self.assertRaisesRegex(ValueError, 'base or HEAD'):
            self.backend._recover_worktree(self.session, self.evidence())

    def test_worktree_recovery_blocks_missing_or_ambiguous_inventory(self):
        for rows in ([], [self.worktree, dict(self.worktree, id='other')]):
            self.backend.call.return_value = self.inventory(rows)
            self.backend.call.side_effect = None
            with self.subTest(rows=len(rows)), self.assertRaisesRegex(ValueError, 'absent or ambiguous'):
                self.backend._recover_worktree(self.session, self.evidence())

    def test_plain_repo_import_uses_orcas_implicit_project_for_the_child(self):
        self.parent.pop('projectId')
        self.worktree['projectId'] = 'repo:' + self.parent['repoId']
        state = {'features': {'feat': {'attempt': 'attempt', 'base_sha': self.head}}, 'operations': {}}
        self.session.read = lambda: state

        @contextmanager
        def transaction():
            yield state

        def effect(key, *, validate):
            result = {'worktree': self.worktree}
            validate(result)
            return result

        self.session.transaction = transaction
        self.session.effect = effect
        self.assertEqual(self.backend.ensure_attempt(self.session, 'feat'), str(self.child))
        self.assertEqual(state['operations']['worktree:feat']['evidence']['project'], 'repo:repo')

    def test_worker_start_uses_worker_show_for_terminal_and_process_identity(self):
        result = {'runId': 'run_child', 'taskId': 'task_worker', 'dispatchId': 'ctx_worker', 'state': 'ready'}
        self.backend._dispatch_receipt(result, 'run_child', 'task_worker', self.terminal)
        with self.assertRaisesRegex(ValueError, 'exact assignment'):
            self.backend._dispatch_receipt(result, 'run_child', 'task_worker',
                                           dict(self.terminal, incarnationId='different'))
        with self.assertRaisesRegex(ValueError, 'exact Task'):
            self.backend._dispatch_receipt(result, 'run_child', 'task_other', self.terminal)

    def test_terminal_create_resolves_missing_leaf_without_rebinding_process(self):
        assignment = self.assignment | {'checkout': str(self.child)}
        receipt = {'terminal': self.terminal | {'leafId': None}}
        with patch.object(self.backend, '_terminal', return_value=self.terminal):
            self.backend._terminal_receipt(receipt, assignment)
            self.assertEqual(receipt['terminal'], self.terminal)
            changed = {'terminal': self.terminal | {'leafId': None, 'incarnationId': 'different'}}
            with self.assertRaisesRegex(ValueError, 'different panel or incarnation'):
                self.backend._terminal_receipt(changed, assignment)

    def test_lost_worker_start_receipt_checks_request_identity_and_never_creates_again(self):
        self.backend.call.side_effect = lambda *a, **kw: (
            {'requestId': 'request-worker', 'state': 'completed', 'method': 'orchestration.workerStart',
             'receipt': {'dispatchId': 'ctx_worker', 'runId': 'run_child', 'taskId': 'task_worker', 'state': 'ready'}}
            if a[1] == 'request-show' else self.call(*a, **kw)
        )
        self.session.call = self.backend.call
        op = {
            'request_id': 'request-worker', 'mailbox': 'term_coordinator',
            'args': ['orchestration', 'worker-start', '--run', 'run_child', '--task', 'task_worker'],
            'evidence': {'terminal': self.terminal},
        }
        self.assertEqual(self.backend.reconcile(self.session, op)['dispatchId'], 'ctx_worker')
        self.assertEqual([c.args[1] for c in self.backend.call.call_args_list], ['request-show', 'worker-show'])

    def test_request_absent_cannot_authorize_repeat(self):
        self.session.call = Mock(return_value={'requestId': 'lost', 'state': 'absent'})
        op = {'request_id': 'lost', 'mailbox': 'term_worker', 'args': ['orchestration', 'ask']}
        with self.assertRaisesRegex(UncertainOperation, 'absent'):
            self.backend.reconcile(self.session, op)

    def test_run_receipt_for_native_chat_requires_its_authenticated_read_back(self):
        self.backend.call.side_effect = None
        row = {'id': 'run_native', 'objective': 'fixture', 'coordinator_handle': None, 'legacy': 0}
        self.backend.call.return_value = {'run': row}
        self.backend._validate_run_create({'run': row}, 'orca_session_id:chat', 'fixture')
        self.backend.call.assert_called_once_with('orchestration', 'run-current', mailbox='orca_session_id:chat')
        self.backend.call.return_value = {'run': dict(row, id='foreign')}
        with self.assertRaisesRegex(ValueError, 'not currently bound'):
            self.backend._validate_run_create({'run': row}, 'orca_session_id:chat', 'fixture')

    def test_worker_done_rejects_another_attempt_dispatch(self):
        message = {
            'id': 'message', 'from_handle': 'term_worker', 'to_handle': 'run:run_child', 'run_id': 'run_child',
            'type': 'worker_done', 'payload': '{"taskId":"task_worker","dispatchId":"ctx_worker","outcome":"succeeded"}',
        }
        self.assertEqual(self.backend.validate_report(self.session, self.assignment, {'message': message}), message)
        with self.assertRaisesRegex(ValueError, 'worker_done receipt identity'):
            self.backend.validate_report(self.session, self.assignment, {'message': dict(message, run_id='other')})

    def test_external_worker_stop_receipt_requires_exact_fence_reason(self):
        receipt = {'dispatchId': 'ctx_worker', 'state': 'stop_unknown', 'processAction': 'none',
                   'lastError': 'The worker terminal is external; no terminal was closed.'}
        self.assertEqual(self.backend._stopped(receipt, 'ctx_worker', external=True), receipt)
        for result, external in ((receipt, False), (dict(receipt, processAction='unknown'), True),
                                 (dict(receipt, lastError='Terminal did not exit'), True)):
            with self.assertRaisesRegex(ValueError, 'unresolved'):
                self.backend._stopped(result, 'ctx_worker', external=external)

    def test_real_transport_never_impersonates_saved_mailbox(self):
        transport = Mock()
        backend = OrcaBackend(transport)
        with patch.dict(os.environ, {'ORCA_TERMINAL_HANDLE': 'term_other'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'caller differs'):
                backend.call('orchestration', 'worker-stop', mailbox='term_worker')
        transport.assert_not_called()

    def test_frozen_codex_arguments_and_environment_per_role(self):
        root = self.checkout.parent
        cache = root / 'gocache'
        home = root / 'codex'
        for name in ('tmp', 'sessions', 'log'):
            (home / name).mkdir(parents=True)
        codex_settings = {'agent': 'codex', 'profile': '', 'provider': 'openai', 'model': 'gpt-fixture',
                          'effort': 'high'}
        context = Context('project', 'workspace', self.checkout, root / 'state', root / 'config',
                          root / 'state-root', self.parent['id'], self.parent['instanceId'],
                          profile({'runtime': {'go_cache': str(cache)}}), ())
        attempt = root / 'attempt'
        feature = {'directory': str(attempt), 'base_sha': self.head,
                   'worktree': {'id': self.worktree['id'], 'incarnation': self.worktree['instanceId']},
                   'candidate': {'sha': self.head}}
        frozen = {
            'runtime': context.profile['runtime'], 'settings': codex_settings, 'binary': sys.executable,
            'binary_sha256': sha256(Path(sys.executable).read_bytes()).hexdigest(),
            'connection': 'frozen', 'version': 'codex-fixture', 'tier': 'default',
        }
        session = SimpleNamespace(context=context, execution='execution')
        state = {'role_settings': {name: frozen for name in ROLE_SETTINGS}}
        with (patch('team_runner.cascade_orca._codex_home', return_value=home),
              patch('team_runner.cascade_orca._connection', return_value=('openai', 'frozen')),
              patch('team_runner.cascade_orca._version', return_value='codex-fixture'),
              patch('team_runner.cascade_verification.probe_readonly', return_value={'guard': 'ok'}) as probe,
              patch('team_runner.cascade_verification.boundary', return_value={'clean': True}),
              patch('team_runner.cascade_verification.readonly_argv', side_effect=lambda argv, *a, **k: ['bwrap', *argv])):
            for role in ROLE_SETTINGS:
                with self.subTest(role=role):
                    plan = self.backend._launch_plan(session, state, 'feature', feature, role, self.child)
                    self.assertEqual(plan['codex_argv'][0], sys.executable)
                    self.assertIn('workspace-write', plan['codex_argv'])
                    self.assertIn('on-request', plan['codex_argv'])
                    self.assertIn('service_tier="default"', plan['codex_argv'])
                    self.assertIn('model_provider="openai"', plan['codex_argv'])
                    self.assertIn('model_reasoning_effort="high"', plan['codex_argv'])
                    self.assertIn('features.multi_agent=false', plan['codex_argv'])
                    self.assertIn('--no-daemon', plan['codex_argv'])
                    self.assertEqual(plan['environment']['TEAM_RUNNER_WORKSPACE'], 'workspace')
                    self.assertEqual(plan['environment']['TEAM_RUNNER_CHECKOUT'], str(self.child))
                    self.assertEqual(plan['environment']['TEAM_RUNNER_CASCADE_ROLE'], role)
                    self.assertEqual(plan['environment']['TEAM_RUNNER_ATTEMPT'], str(attempt))
                    self.assertEqual(plan['environment']['GOCACHE'], str(cache))
                    if role in ('tester', 'reviewer'):
                        self.assertEqual(plan['argv'][2:7], ['team_runner.cascade_role', 'execution',
                                                            'feature', '--', 'bwrap'])
                        self.assertNotIn(attempt, probe.call_args.args[2])
                    else:
                        self.assertEqual(plan['argv'], plan['codex_argv'])

    def test_wrapper_fences_changed_environment_and_uses_exact_argv(self):
        path = self.checkout.parent / 'launch.json'
        entry = path.with_suffix('.entry.json')
        ready = path.with_suffix('.ready.json')
        environment = {'TEAM_RUNNER_PROJECT': 'project', 'ORCA_TERMINAL_HANDLE': 'term_worker'}
        plan = {'checkout': str(self.child), 'worktree': self.worktree['id'],
                'nonce': 'nonce', 'entry': str(entry), 'ready': str(ready), 'environment': environment,
                'argv': ['codex', '-s', 'workspace-write'], 'runtime': {'settings': {}}}
        atomic_json(path, plan)
        atomic_json(entry, {'nonce': 'nonce', 'pid': os.getpid(), 'terminal': self.terminal})
        digest = _digest(plan)
        with (patch.dict(os.environ, environment, clear=True),
              patch('sys.argv', ['cascade_orca_entry', str(path), digest]),
              patch('team_runner.cascade_orca_entry.Path.cwd', return_value=self.child),
              patch('team_runner.cascade_orca_entry._connection', return_value=('openai', 'frozen')),
              patch('team_runner.cascade_orca_entry._validate_launch'),
              patch('os.execvpe') as launch):
            cascade_orca_entry.main()
            launch.assert_called_once_with('codex', plan['argv'], os.environ)
            self.assertIn(digest, ready.read_text())
            os.environ['TEAM_RUNNER_PROJECT'] = 'other'
            with self.assertRaisesRegex(ValueError, 'different launch parameters'):
                cascade_orca_entry.main()
            self.assertEqual(launch.call_count, 1)

    def test_unsupported_profile_is_rejected_before_orca(self):
        context = SimpleNamespace(repo=self.checkout, profile=profile({}), config_root=self.checkout,
                                  settings_dir=self.checkout, role_layers=())
        config = runner_config.defaults()
        config['roles']['coordinator'].update(agent='codex', profile='not-supported')
        with (patch.object(runner_config, 'load_config', return_value=config),
              patch('team_runner.cascade_orca._binary', return_value=sys.executable),
              patch('team_runner.cascade_orca._version', return_value='codex-fixture')):
            with self.assertRaisesRegex(ValueError, 'empty profile'):
                self.backend.role_snapshots(context)
        self.backend.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
