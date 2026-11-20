import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from team_runner import cascade_container, cascade_role, cascade_verification
from team_runner.cascade_orca import OrcaBackend


class ContainerVerifierTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.checkout = root / 'checkout'
        self.checkout.mkdir()
        self.git = self.checkout / '.git'
        self.git.mkdir()
        self.authority = root / 'attempt'
        self.artifacts = self.authority / 'artifacts' / 'tester'
        self.artifacts.mkdir(parents=True)
        self.candidate = {'checkout': str(self.checkout), 'git_dirs': [str(self.git)], 'extra': {},
                          'manifest': str(self.authority / 'candidate-manifest.json'), 'digest': 'digest'}
        self.image = 'sha256:' + 'a' * 64

    def guard(self):
        with (patch.object(cascade_container.os, 'geteuid', return_value=1000),
              patch.object(cascade_container.os, 'getegid', return_value=1000)):
            return cascade_container.guard(self.candidate, self.artifacts, self.image, 'verifier-fixture')

    def test_launch_only_binds_artifacts_writable_and_uses_pinned_image(self):
        guard = self.guard()
        with patch.object(cascade_container, 'docker', side_effect=lambda *args: ['docker', *args]):
            argv = cascade_container.docker_argv(guard, guard['bridge'], [cascade_container.CODEX],
                                                 credential='PROVIDER_TOKEN')
        mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == '--mount']
        writable = [mount for mount in mounts if 'bind-recursive=readonly' not in mount]
        self.assertEqual(len(writable), 1)
        self.assertIn('source=' + str(self.artifacts), writable[0])
        self.assertTrue(any('target=' + str(self.checkout) + ',' in mount and 'readonly' in mount
                            for mount in mounts))
        self.assertIn('CARGO_TARGET_DIR=' + str(self.artifacts / 'cargo-target'), argv)
        self.assertIn('PROVIDER_TOKEN', argv)
        self.assertEqual(argv[argv.index('--network') + 1], 'none')
        self.assertEqual(argv[argv.index('--cap-drop') + 1], 'ALL')
        self.assertIn('--read-only', argv)
        self.assertEqual(argv[argv.index(self.image) + 1:], [
            '-m', 'team_runner.cascade_container', 'container', cascade_container.CODEX,
        ])

    def test_recomputing_guard_allows_internal_cargo_links_created_after_start(self):
        guard = self.guard()
        source = self.artifacts / 'compiled'
        source.touch()
        os.link(source, self.artifacts / 'compiled-link')
        with self.assertRaisesRegex(ValueError, 'hard-linked'):
            self.guard()
        with (patch.object(cascade_container.os, 'geteuid', return_value=1000),
              patch.object(cascade_container.os, 'getegid', return_value=1000)):
            self.assertEqual(cascade_verification._role_protection(self.candidate, guard), guard)

    def test_broker_checks_unix_peer_credentials_against_process_ancestor(self):
        first, second = socket.socketpair()
        with first, second:
            self.assertTrue(cascade_role._peer_is_child(first, os.getpid()))
            self.assertFalse(cascade_role._peer_is_child(first, None))
            self.assertFalse(cascade_role._peer_is_child(first, 2**30))

    def test_reap_checks_frozen_container_before_removing_exact_id(self):
        guard = self.guard()
        expected_id = 'b' * 64
        with (patch.object(cascade_container, 'absent', side_effect=[False, True]),
              patch.object(cascade_container, '_runtime', return_value={'Id': expected_id}) as inspect,
              patch.object(cascade_container, 'docker', side_effect=lambda *args: ['docker', *args]),
              patch.object(cascade_container.subprocess, 'run') as run):
            self.assertEqual(cascade_container.reap(guard), {'name': guard['name'], 'absent': True})
        inspect.assert_called_once_with(guard, running=False)
        self.assertEqual(run.call_args.args[0], ['docker', 'container', 'rm', '--force', '--', expected_id])

    def test_reap_leaves_container_with_different_settings_alone(self):
        with (patch.object(cascade_container, 'absent', return_value=False),
              patch.object(cascade_container, '_runtime', side_effect=ValueError('mismatch')),
              patch.object(cascade_container.subprocess, 'run') as run):
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                cascade_container.reap(self.guard())
        run.assert_not_called()

    def test_startup_opens_restricted_folder_with_stable_orca_request(self):
        terminal = {'handle': 'fixture', 'worktreeId': 'fixture-checkout'}
        launch = {'checkout': str(self.checkout), 'nonce': str(uuid4())}
        backend = OrcaBackend()
        composer = {'terminal': {'source': 'screen', 'tail': [
            '>_ OpenAI Codex (v0.171.0)', '› Ask Codex to do anything',
        ]}}
        screens = [
            {'terminal': {'source': 'screen', 'tail': ['Folder access', '› 1. Open restricted']}},
            {},
            composer,
            composer,
        ]
        with (patch.object(cascade_container, 'ready', return_value=True),
              patch.object(backend, '_terminal', return_value=terminal),
              patch.object(backend, 'call', side_effect=screens) as call,
              patch('team_runner.cascade_orca.time.monotonic', side_effect=[0, 1, 2, 2, 3, 4]),
              patch('team_runner.cascade_orca.time.sleep')):
            backend._open_restricted(launch, terminal, 123)
        send = call.call_args_list[1].args
        self.assertEqual(send[:-1], ('terminal', 'send', '--terminal', 'fixture', '--enter',
                                     '--text', ' ', '--retry-request'))
        self.assertEqual(len(send[-1]), 36)

    def test_only_exact_accepted_input_can_wait_for_container_report(self):
        terminal = {'incarnationId': 'pane-process'}
        data = {'state': 'outcome_unknown', 'prompt': {
            'requestId': 'request', 'provider': 'codex', 'stages': ['input_accepted'],
            'processIncarnation': 'pane-process',
        }}
        verify = OrcaBackend.container_input_accepted
        self.assertTrue(verify(data, 'request', terminal))
        self.assertFalse(verify(data, 'another-request', terminal))
        self.assertFalse(verify(data, 'request', {'incarnationId': 'replacement'}))
        data['prompt']['stages'] = []
        self.assertFalse(verify(data, 'request', terminal))

    def test_report_can_wait_past_worker_start_timeout_for_assignment_publication(self):
        session = Mock()
        def state(dispatch):
            return {'status': 'active', 'features': {'feature': {'roles': {
                'tester': {'terminal': 'term', 'dispatch': dispatch},
            }}}}
        session.read.side_effect = [state(None), state(None), state('dispatch')]
        with (patch.object(cascade_role.time, 'monotonic', side_effect=[0, 1, 65, 89]),
              patch.object(cascade_role.time, 'sleep') as sleep):
            cascade_role._wait_for_assignment(session, 'feature', 'tester', 'term')
        self.assertEqual(session.read.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
