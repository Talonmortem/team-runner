"""Installed OpenCode integration against a loopback fake API; no provider calls."""
import json
import os
import pty
import signal
import sys
import time
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from team_runner.runner_cloud import overlay


@unittest.skipUnless(shutil.which('opencode2'), 'opencode2 is not installed')
class OpenCodeWire(unittest.TestCase):
    def test_five_models_effort_thinking_and_tool_continuation(self):
        self.check_wire(medium=False)

    def test_five_models_medium_effort_thinking_and_tool_continuation(self):
        self.check_wire(medium=True)

    def test_interactive_tui_ignores_shared_high_variant(self):
        self.check_wire(medium=True, tui=True)

    def check_wire(self, medium, tui=False):
        models = {'claude-opus-4.8': 'anthropic/claude-opus-4.8',
                  'gpt-5.5': 'openai/gpt-5.5', 'claude-sonnet-4.6': 'anthropic/claude-sonnet-4.6',
                  'deepseek-v4.1-flash': 'deepseek-ai/DeepSeek-V4.1-Flash', 'glm-5.2': 'zai-org/GLM-5.2'}
        if tui:
            models = {'claude-opus-4.8': models['claude-opus-4.8']}
        for model, api_id in models.items():
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp:
                received = []
                tool_sent = []
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass
                    def do_POST(self):
                        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        received.append(data)
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream')
                        self.end_headers()
                        tool = bool(data.get('tools')) and not tool_sent
                        delta = {'role': 'assistant', 'content': 'DONE'}
                        if tool:
                            tool_sent.append(True)
                            delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'call_fixture',
                                     'type': 'function', 'function': {'name': 'execute',
                                     'arguments': '{"code":"return 1"}'}}]}
                        chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1,
                                 'model': data['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
                        self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                        chunk['choices'][0] = {'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if tool else 'stop'}
                        self.wfile.write(('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode())
                server = HTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                effort = 'medium' if medium else ('xhigh' if model == 'gpt-5.5' else 'high')
                content = overlay({'model': model, 'effort': effort})
                content['update'] = 'disable'
                cloud = content['providers']['cloudru']
                cloud['package'] = '@opencode/ai/providers/openai-compatible'
                cloud['settings']['baseURL'] = f'http://127.0.0.1:{server.server_port}/v1'
                cloud['models'][model]['modelID'] = api_id
                cloud['models'][model]['limit']['context'] = 1000000
                environment = {**os.environ, 'HOME': tmp, 'CLOUD_API': 'fake-api-key',
                               'XDG_CONFIG_HOME': tmp + '/config', 'XDG_DATA_HOME': tmp + '/data',
                               'XDG_STATE_HOME': tmp + '/state', 'XDG_CACHE_HOME': tmp + '/cache',
                               'OPENCODE_DISABLE_MODELS_FETCH': '1', 'OPENCODE_CONFIG_CONTENT': json.dumps(content)}
                environment.pop('OPENCODE_CONFIG', None)
                environment.pop('OPENCODE_CONFIG_DIR', None)
                try:
                    if tui:
                        repo = Path(tmp) / 'repo/sber/app'
                        repo.mkdir(parents=True)
                        (repo.parent / 'AI').mkdir()
                        (repo.parent / 'AI/.env').write_text('CLOUD_API=fake-api-key')
                        shared = Path(tmp) / 'state/opencode/model.json'
                        shared.parent.mkdir(parents=True)
                        shared.write_text(json.dumps({'recent': [{'providerID': 'cloudru', 'modelID': model}],
                                                      'favorite': [], 'variant': {'cloudru/' + model: 'high'}}))
                        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
                        master, slave = pty.openpty()
                        process = subprocess.Popen([sys.executable, '-m', 'team_runner.runner_cloud',
                                                    '--standalone', '--prompt', 'Reply DONE'],
                                                   cwd=repo, env=environment, stdin=slave, stdout=slave,
                                                   stderr=slave, start_new_session=True)
                        os.close(slave)
                        def drain():
                            try:
                                while os.read(master, 65536):
                                    pass
                            except OSError:
                                pass
                        reader = threading.Thread(target=drain, daemon=True)
                        reader.start()
                        try:
                            deadline = time.monotonic() + 35
                            while len(received) < 2 and process.poll() is None and time.monotonic() < deadline:
                                time.sleep(.1)
                            self.assertGreaterEqual(len(received), 2, 'TUI did not complete tool continuation')
                            self.assertEqual(json.loads(shared.read_text())['variant']['cloudru/' + model], 'high')
                        finally:
                            if process.poll() is None:
                                os.killpg(process.pid, signal.SIGTERM)
                            process.wait(timeout=5)
                            os.close(master)
                            reader.join(timeout=2)
                    else:
                        process = subprocess.run([shutil.which('opencode2'), 'run', '--standalone', '--format', 'json',
                                                  'Reply DONE'], cwd=tmp, env=environment, text=True,
                                                 capture_output=True, timeout=40)
                        self.assertEqual(process.returncode, 0, process.stderr[-1000:])
                    self.assertGreaterEqual(len(received), 2)
                    for body in received:
                        self.assertEqual(body['model'], api_id)
                        self.assertEqual(body['reasoning_effort'], effort)
                        self.assertNotIn('max_tokens', body)
                        self.assertEqual(body.get('max_completion_tokens'), 24576)
                        if model.startswith(('deepseek', 'glm')):
                            self.assertEqual(body['thinking']['type'], 'enabled')
                    self.assertTrue(any(any(m['role'] == 'tool' for m in b['messages']) for b in received))
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()


if __name__ == '__main__':
    unittest.main()
