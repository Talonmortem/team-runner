"""Manager CLI is explicit; status stays read-only and children cannot mutate."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch
import test_platform as fixtures
from team_runner import cli


class ManagerCLI(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def test_status_does_not_start_or_create_manager_directory(self):
        output = io.StringIO()
        with patch.object(cli, 'Catalog', return_value=self.catalog), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['manager', 'status']), 0)
        self.assertEqual(json.loads(output.getvalue()), {'state': 'stopped', 'confirmed': True})
        self.assertFalse((self.catalog.state_root / 'manager').exists())

    def test_explicit_start_uses_supplied_id_and_child_is_rejected(self):
        import uuid
        rid = str(uuid.uuid4())
        with patch.object(cli, 'Catalog', return_value=self.catalog), \
                patch('team_runner.service_manager.Manager.ensure', return_value={'state': 'running'}) as ensure, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['manager', 'start', '--request-id', rid]), 0)
            ensure.assert_called_once_with(rid)
            with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': 'fixture'}):
                self.assertNotEqual(cli.main(['manager', 'start', '--request-id', rid]), 0)
            ensure.assert_called_once()
