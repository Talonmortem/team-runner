"""Startup identity survives reconciliation and transient supervisor lock ownership."""
from contextlib import contextmanager
import threading
import unittest
import uuid
from unittest.mock import patch

import test_platform as fixtures
from team_runner import application, goal_intent, service
from team_runner.runner_store import atomic_json, read_json, lock


class StartupIdentity(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def test_reconciliation_preserves_launch_identity_for_live_and_idle_owner(self):
        startup = {'status': 'starting', 'nonce': str(uuid.uuid4()), 'terminal': 'retained-terminal'}
        atomic_json(self.a.state_dir / 'startup.json', startup)
        state = {'history': [], 'runner_status': 'running'}
        self.a.save_state(state)
        with lock(self.a.state_dir / 'runner.lock'):
            self.a.recover(state, technical_only=True)
        self.assertEqual(read_json(self.a.state_dir / 'startup.json'), dict(startup, status='running'))
        self.a.recover(state, technical_only=True)
        self.assertEqual(read_json(self.a.state_dir / 'startup.json'), dict(startup, status='idle'))

    def test_entry_waits_for_supervisor_lock_and_still_consumes_nonce_once(self):
        auth = goal_intent.authorize(self.a, {'goal': application.goals(self.a)[0]}, str(uuid.uuid4()))
        nonce = str(uuid.uuid4())
        atomic_json(self.a.state_dir / 'launch.json', {'nonce': nonce, 'identity': self.a.context.identity(),
                    'authorization': {k: auth[k] for k in ('generation', 'epoch')},
                    'options': goal_intent.read(self.a)['options']})
        attempted = threading.Event()
        results, errors = [], []
        runner_lock = self.a.state_dir / 'runner.lock'

        @contextmanager
        def observed_lock(path, *args, **kwargs):
            if path == runner_lock:
                attempted.set()
            with lock(path, *args, **kwargs) as fd:
                yield fd

        def enter():
            try:
                results.append(service.run_process(self.a, nonce))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=enter, daemon=True)
        with patch.object(service, 'lock', observed_lock), patch.object(self.a, 'cmd_run', return_value=0) as run:
            with lock(runner_lock):
                thread.start()
                self.assertTrue(attempted.wait(2))
                thread.join(.05)
                self.assertTrue(thread.is_alive(), errors)
                self.assertFalse((self.a.state_dir / ('launch-' + nonce + '.claim')).exists())
                run.assert_not_called()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [0])
            run.assert_called_once()
            with self.assertRaises(FileExistsError):
                service.run_process(self.a, nonce)
            run.assert_called_once()
        self.assertEqual(read_json(self.a.state_dir / 'startup.json'), {'status': 'idle', 'nonce': nonce})


if __name__ == '__main__':
    unittest.main()
