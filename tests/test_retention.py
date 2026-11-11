"""Destructive cleanup is exercised only in temporary fixture directories."""
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from team_runner.retention import cleanup
from team_runner.runner_store import atomic_json


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = {'current': None}
        self.runner = SimpleNamespace(state_dir=self.root / 'workspace', load_state=lambda: self.state)
        self.runner.state_dir.mkdir()
        self.refs = patch('team_runner.retention.process_references', return_value=([], []))
        self.refs.start(); self.addCleanup(self.refs.stop)

    def attempt(self, name='old'):
        p = self.runner.state_dir / 'attempts' / name
        atomic_json(p / 'agents.json', {'fenced': True, 'agents': [{'status': 'released', 'terminal_closed': True}],
                                      'runs': [{'status': 'closed'}], 'launch_requests': [{'status': 'done'}]})
        atomic_json(p / 'operations.json', {'close': {'status': 'completed'}})
        (p / 'reports').mkdir()
        (p / 'reports' / 'acceptance.md').write_text('retain evidence')
        return p

    def modules(self, root):
        p = root / 'copy' / 'frontend' / 'node_modules'
        p.mkdir(parents=True)
        (p.parent / 'package.json').write_text('{}')
        (p / 'dependency.js').write_text('generated' * 1000)
        return p

    def pg(self, root):
        p = root / 'database'
        (p / 'global').mkdir(parents=True)
        (p / 'base').mkdir()
        (p / 'PG_VERSION').write_text('16')
        (p / 'global' / 'pg_control').write_text('control')
        (p / 'base' / 'data').write_text('data' * 1000)
        return p

    def test_preview_apply_and_repeat_preserve_history(self):
        a = self.attempt(); modules = self.modules(a); pg = self.pg(a)
        preview = cleanup(self.runner, days=0)
        self.assertEqual(len(preview['candidates']), 2)
        self.assertTrue(modules.exists()); self.assertTrue(pg.exists())
        result = cleanup(self.runner, days=0, apply=True)
        self.assertGreater(result['removed_bytes'], 0)
        self.assertFalse(modules.exists()); self.assertFalse(pg.exists())
        self.assertTrue((a / 'reports/acceptance.md').exists())
        self.assertTrue((a / 'agents.json').exists())
        self.assertTrue((a / 'copy/frontend/package.json').exists())
        self.assertTrue(Path(result['journal']).exists())
        self.assertEqual(cleanup(self.runner, days=0, apply=True)['removed_bytes'], 0)

    def test_current_failed_attempt_is_always_preserved(self):
        a = self.attempt(); p = self.modules(a)
        self.state['current'] = {'attempt_dir': str(a), 'failed': True}
        self.assertFalse(cleanup(self.runner, days=0, apply=True)['candidates'])
        self.assertTrue(p.exists())

    def test_retry_context_and_go_cache(self):
        a = self.attempt(); p = a / 'private-cache'
        p.mkdir(); (p / 'README').write_text('This directory holds cached build artifacts from the Go build system.')
        (p / 'artifact').write_text('cache')
        self.state['current'] = {'retry_context_from': str(a)}
        cleanup(self.runner, days=0, apply=True); self.assertTrue(p.exists())
        self.state['current'] = None
        cleanup(self.runner, days=0, apply=True); self.assertFalse(p.exists())

    def test_bind_mount_is_not_traversed_for_deletion(self):
        a = self.attempt(); p = self.modules(a)
        original = Path.read_text
        def contents(path, *args, **kwargs):
            if str(path) == '/proc/self/mountinfo':
                return '1 2 0:1 / ' + str(p) + ' rw - ext4 /dev/test rw\n'
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', contents):
            cleanup(self.runner, days=0, apply=True)
        self.assertTrue(p.exists())

    def test_unsettled_or_unknown_operations_are_preserved(self):
        for flag in ('active', 'unknown', 'missing', 'fenced', 'run', 'request'):
            a = self.attempt(flag); p = self.modules(a)
            reg = json.loads((a / 'agents.json').read_text())
            if flag == 'active': reg['agents'][0]['status'] = 'active'
            if flag == 'fenced': reg['fenced'] = False
            if flag == 'run': reg['runs'][0]['status'] = 'ready'
            if flag == 'request': reg['launch_requests'][0]['status'] = 'pending'
            atomic_json(a / 'agents.json', reg)
            if flag == 'missing': (a / 'agents.json').unlink()
            if flag == 'unknown': atomic_json(a / 'operations.json', {'send': {'status': 'unknown'}})
            cleanup(self.runner, days=0, apply=True)
            self.assertTrue(p.exists(), flag)

    def test_age_pin_process_and_symlink(self):
        a = self.attempt(); p = self.modules(a)
        self.assertFalse(cleanup(self.runner, apply=True)['candidates'])
        (a / '.keep').touch()
        self.assertFalse(cleanup(self.runner, days=0, apply=True)['candidates'])
        (a / '.keep').unlink()
        with patch('team_runner.retention.process_references', return_value=([str(a / 'script.py')], [])):
            self.assertFalse(cleanup(self.runner, days=0, apply=True)['candidates'])
        p.rename(p.with_name('saved'))
        p.symlink_to(p.with_name('saved'), target_is_directory=True)
        cleanup(self.runner, days=0, apply=True)
        self.assertTrue(p.is_symlink()); self.assertTrue(p.with_name('saved').exists())

    def test_live_postgres_and_embedded_logs_are_preserved(self):
        a = self.attempt(); p = self.pg(a)
        (p / 'postmaster.pid').write_text(str(os.getpid()))
        cleanup(self.runner, days=0, apply=True); self.assertTrue(p.exists())
        (p / 'postmaster.pid').unlink(); (p / 'server.log').write_text('evidence')
        cleanup(self.runner, days=0, apply=True); self.assertTrue(p.exists())

    def test_legacy_only_explicit_retired_root(self):
        legacy = self.root / '.local/state/shm-team/runs'
        p = self.pg(legacy / 'old-run')
        with patch('team_runner.retention.Path.home', return_value=self.root):
            self.assertFalse(cleanup(self.runner, days=0)['candidates'])
            result = cleanup(self.runner, days=0, apply=True, legacy_root=legacy)
            self.assertGreater(result['removed_bytes'], 0); self.assertFalse(p.exists())
            with self.assertRaises(ValueError):
                cleanup(self.runner, days=0, legacy_root=self.root)

    def test_preserved_directories_and_future_mtime(self):
        a = self.attempt(); p = self.modules(a / 'reports')
        other = self.modules(a)
        os.utime(other / 'dependency.js', (time.time() + 100, time.time() + 100))
        cleanup(self.runner, days=0, apply=True)
        self.assertTrue(p.exists()); self.assertTrue(other.exists())

    def test_process_starts_after_preview_is_skipped(self):
        a = self.attempt(); p = self.modules(a)
        with patch('team_runner.retention.process_references', side_effect=[([], []), ([str(p)], [])]):
            result = cleanup(self.runner, days=0, apply=True)
        self.assertEqual(result['candidates'][0]['status'], 'skipped_process')
        self.assertTrue(p.exists())

    def test_child_cannot_cleanup(self):
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': '/any/attempt'}):
            with self.assertRaises(ValueError): cleanup(self.runner)


if __name__ == '__main__':
    unittest.main()
