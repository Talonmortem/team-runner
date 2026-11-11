"""Browser navigation must receive HTTP responses before tab creation returns."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

from team_runner.catalog import Catalog
from team_runner import orca, runner_ui


class UIStartup(unittest.TestCase):
    def test_http_ready_during_browser_open_and_cleanup(self):
        for failure in (None, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                catalog = Catalog(Path(tmp) / 'config', Path(tmp) / 'state')
                server, token = runner_ui.make_server(catalog)
                output = io.StringIO()

                def open_tab(*args):
                    self.assertEqual(args[:3], ('tab', 'create', '--url'))
                    base = args[3].split('#')[0]
                    for path in ('', 'app.js', 'style.css'):
                        with urllib.request.urlopen(base + path, timeout=2) as response:
                            self.assertEqual(response.status, 200)
                            self.assertTrue(response.read())
                    request = urllib.request.Request(base + 'api/projects',
                        headers={'Authorization': 'Bearer ' + token})
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(json.load(response), catalog.read())
                    server.shutdown()
                    if failure is subprocess.TimeoutExpired:
                        raise failure(['orca', *args], 60)
                    if failure:
                        raise failure('fixture')
                    return {'browserPageId': 'fixture'}

                try:
                    with patch.object(runner_ui, 'make_server', return_value=(server, token)), \
                            patch.object(orca, 'call', side_effect=open_tab) as call, redirect_stdout(output):
                        runner_ui.serve(catalog)
                    call.assert_called_once()
                    self.assertEqual(server.fileno(), -1)
                    # Only the deliberately printed launch URL contains the token.
                    self.assertEqual(output.getvalue().count(token), 1)
                    if failure in (RuntimeError, subprocess.TimeoutExpired):
                        self.assertIn('Open the local URL manually.', output.getvalue())
                finally:
                    server.server_close()


if __name__ == '__main__':
    unittest.main()
