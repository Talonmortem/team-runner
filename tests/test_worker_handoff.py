import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from team_runner import agent_runtime, worker_report
from team_runner.runner_launch import Launcher
from team_runner.runner_store import atomic_json, read_json


class Runtime(unittest.TestCase):
    def test_cache_creation_and_explicit_sandbox_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / 'go-cache')
            self.assertEqual(agent_runtime.prepare({'go_cache': cache})['go_cache'], cache)
            self.assertTrue(Path(cache).is_dir())
            cfg = dict(agent='codex', profile='', provider='', model='gpt-6.1-sol', effort='high')
            launcher = Launcher(SimpleNamespace(repo=Path(tmp)))
            argv = launcher.argv(cfg, tmp, {'go_cache': cache})
            roots = next(v.split('=', 1)[1] for v in argv if v.startswith('sandbox_workspace_write.writable_roots='))
            self.assertIn(cache, json.loads(roots))
            self.assertNotIn(cache, launcher.argv(cfg, tmp))

    def test_invalid_and_unwritable_cache(self):
        for value in ['relative', '/', str(Path.home()), '/tmp/../b']:
            with self.assertRaises(ValueError): agent_runtime.validate({'go_cache': value})
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'file'; p.write_text('x')
            with self.assertRaises(OSError): agent_runtime.prepare({'go_cache': str(p)})

    def test_frozen_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / 'cache')
            atomic_json(Path(tmp) / 'context.json', {'profile': {'runtime': {'go_cache': cache}}})
            launcher = Launcher(SimpleNamespace(profile={'runtime': {'go_cache': '/wrong'}}))
            self.assertEqual(launcher.prepare_runtime(tmp)['go_cache'], cache)


class Completion(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.attempt = Path(tmp.name)
        atomic_json(self.attempt / 'agents.json', {'agents': [dict(id='worker', role='tester',
                    terminal='term', task_id='task', dispatch_id='dispatch', requested={})]})
        self.context = Mock()
        self.env = patch.dict(os.environ, TEAM_RUNNER_PROJECT='project', TEAM_RUNNER_WORKSPACE='workspace',
                              TEAM_RUNNER_ATTEMPT=str(self.attempt), ORCA_TERMINAL_HANDLE='term')
        self.env.start(); self.addCleanup(self.env.stop)
        self.catalog = patch.object(worker_report, 'Catalog'); self.catalog_mock = self.catalog.start()
        self.addCleanup(self.catalog.stop); self.catalog_mock.return_value.context.return_value = self.context
        self.payload = dict(outcome='succeeded', summary='done', checks=[], files_modified=['code.py'])

    def complete(self):
        return worker_report.complete(self.payload, 'term', 'task', 'dispatch')

    def test_report_and_retry_send_once(self):
        with patch.object(worker_report, 'call', return_value={'accepted': True}) as send:
            self.assertEqual(self.complete(), self.complete())
            self.assertEqual(send.call_count, 1)
            args = send.call_args.args
            self.assertEqual(args[args.index('--dispatch-id') + 1], 'dispatch')
            self.assertEqual(args[args.index('--files-modified') + 1], 'code.py')
        self.assertIn('Outcome: succeeded', (self.attempt / 'reports/worker-summary.md').read_text())
        self.payload['summary'] = 'different'
        with self.assertRaisesRegex(ValueError, 'changed'): self.complete()

    def test_unknown_is_not_replayed(self):
        with patch.object(worker_report, 'call', side_effect=RuntimeError('lost reply')) as send:
            with self.assertRaisesRegex(RuntimeError, 'lost reply'): self.complete()
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'): self.complete()
            self.assertEqual(send.call_count, 1)

    def test_identity_and_evidence_fail_before_send(self):
        with patch.object(worker_report, 'call') as send:
            with self.assertRaises(ValueError): worker_report.complete(self.payload, 'other', 'task', 'dispatch')
            with self.assertRaises(ValueError): worker_report.complete(self.payload, 'term', 'task', 'old')
            self.payload['checks'] = ['../escape']
            with self.assertRaises(ValueError): self.complete()
            self.payload['checks'] = ['missing']
            with self.assertRaises(ValueError): self.complete()
            self.context.check_attempt.side_effect = ValueError('stale attempt')
            with self.assertRaisesRegex(ValueError, 'stale'): self.complete()
            send.assert_not_called()

    def test_hostile_text_through_real_subprocess(self):
        cli = self.attempt / 'orca-fixture'
        cli.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        cli.chmod(0o700)
        marker = self.attempt / 'executed'
        body = f'`touch {marker}`\n$(touch {marker})\n"quoted"\n'
        payload = self.attempt / 'body'; payload.write_text(body)
        env = dict(os.environ, ORCA_CLI_COMMAND=str(cli))
        result = subprocess.run([sys.executable, '-m', 'team_runner.orca_msg', 'send',
                                 '--to', 'run:test', '--subject', 'test', '--body-file', str(payload),
                                 '--from', 'term', '--type', 'status', '--json-out'],
                                env=env, capture_output=True, text=True, check=True)
        argv = json.loads(result.stdout)
        self.assertEqual(argv[argv.index('--body') + 1], body)
        self.assertFalse(marker.exists())
