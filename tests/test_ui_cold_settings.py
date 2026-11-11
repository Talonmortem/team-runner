"""Cold UI can inspect/edit local settings without starting or forwarding a service."""
import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import Mock

import test_platform as fixtures
from team_runner import runner_ui


class ColdSettings(unittest.TestCase):
    setUp = fixtures.Platform.setUp

    def test_configuration_and_profile_use_local_contract_when_service_is_offline(self):
        services = Mock()
        services.forward.side_effect = AssertionError('cold settings must not forward')
        server, token = runner_ui.make_server(self.catalog, services=services)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        prefix = f'/api/projects/{self.a.context.project_id}/workspaces/{self.a.context.workspace_id}'
        def request(path, value=None, authenticated=True):
            headers = {'Content-Type': 'application/json', 'Origin': origin}
            if authenticated:
                headers['Authorization'] = 'Bearer ' + token
            data = json.dumps(value).encode() if value is not None else None
            with urllib.request.urlopen(urllib.request.Request(origin + prefix + path,
                                      data=data, headers=headers)) as response:
                return json.load(response)
        try:
            config = request('/config')['config']
            profile = request('/profile')['profile']
            self.assertEqual(request('/config', config), config)
            request('/profile', profile)
            self.assertEqual(request('/profile')['profile'], profile)
            self.assertIn('goals', request('/goals'))
            summary = request('/summary')
            self.assertTrue(summary['confirmed'])
            command = {'schema_version': 1, 'request_id': str(uuid.uuid4()),
                       'action': 'pause', 'payload': {},
                       'scope': {'identity': summary['identity'], 'generation': summary['generation'],
                                 'attempt': summary['current']['attempt'], 'revision': summary['revision']}}
            receipt = request('/commands', command)
            self.assertEqual(receipt['status'], 'succeeded')
            self.assertEqual(request('/commands', command), receipt)
            for path in ('/config', '/profile'):
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    request(path, authenticated=False)
                self.assertEqual(exc.exception.code, 403)
            self.assertEqual(services.mock_calls, [])
        finally:
            server.shutdown(); server.server_close(); thread.join()
