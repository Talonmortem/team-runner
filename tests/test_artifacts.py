import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from team_runner.artifacts import Store, compact, restore, sweep, prepare
from team_runner.runner_store import atomic_json, read_json


class ArtifactsTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.runner = SimpleNamespace(state_dir=self.root)
        self.run = self.root / 'attempts' / 'attempt' / 't1-runs' / 'finished'
        self.run.mkdir(parents=True)
        atomic_json(self.run / 'verification.json', {'status': 'executed', 'result': 'FAIL', 'exit_code': 1,
            'resource': {'status': 'clean', 'cleanup_inventory_complete': True, 'processes': [], 'ports': []}})
        refs = patch('team_runner.artifacts.process_references', return_value=([], []))
        refs.start(); self.addCleanup(refs.stop)

    def tree(self, name='copy'):
        p = self.run / name; p.mkdir()
        (p / 'tool').write_bytes(b'preserved executable' * 100)
        (p / 'tool').chmod(0o751)
        (p / 'empty').mkdir(); (p / 'empty').chmod(0o750)
        return p

    def test_deduplicates_and_restores_exact_bytes_and_modes(self):
        copy = self.tree(); self.tree('bundle')
        raw = (copy / 'tool').read_bytes(); stamp = (copy / 'tool').stat().st_mtime_ns
        self.assertEqual(len(compact(self.runner, self.run)['trees']), 2)
        self.assertTrue(copy.exists())
        result = compact(self.runner, self.run, apply=True)
        self.assertGreater(result['released_bytes'], 0)
        self.assertEqual(len(list((self.root / 'artifact-store/objects').glob('*/*.gz'))), 1)
        self.assertFalse(copy.exists()); self.assertTrue((self.run / 'verification.json').exists())
        restore(self.runner, self.run, 'copy')
        self.assertEqual((copy / 'tool').read_bytes(), raw)
        self.assertEqual((copy / 'tool').stat().st_mode & 0o777, 0o751)
        self.assertEqual((copy / 'tool').stat().st_mtime_ns, stamp)
        self.assertTrue((copy / 'empty').is_dir())
        with self.assertRaises(ValueError): restore(self.runner, self.run, 'copy')
        self.assertGreater(compact(self.runner, self.run, apply=True)['released_bytes'], 0)
        self.assertFalse(copy.exists())

    def test_corrupt_store_never_deletes_original_or_restores(self):
        p = self.tree(); raw = (p / 'tool').read_bytes()
        store = Store(self.root); obj = store.blob(hashlib.sha256(raw).hexdigest())
        obj.parent.mkdir(parents=True); obj.write_bytes(b'corrupted')
        with self.assertRaises(OSError): compact(self.runner, self.run, apply=True)
        self.assertEqual((p / 'tool').read_bytes(), raw)
        obj.unlink(); compact(self.runner, self.run, apply=True); obj.write_bytes(b'corrupted')
        with self.assertRaises(OSError): restore(self.runner, self.run, 'copy')
        self.assertFalse(p.exists())

    def test_unsettled_and_live_runs_are_not_deleted(self):
        p = self.tree()
        with patch('team_runner.artifacts.process_references', return_value=([str(p / 'tool')], [])):
            with self.assertRaises(ValueError): compact(self.runner, self.run, apply=True)
        r = read_json(self.run / 'verification.json'); r['resource']['processes'] = [{'pid': 1}]
        atomic_json(self.run / 'verification.json', r)
        with self.assertRaises(ValueError): compact(self.runner, self.run, apply=True)
        self.assertTrue(p.exists())

    def test_symlink_pin_and_mount_refused(self):
        p = self.tree(); (p / 'link').symlink_to(self.root)
        with self.assertRaises(ValueError): compact(self.runner, self.run, apply=True)
        (p / 'link').unlink(); (p / '.keep').touch()
        with self.assertRaises(ValueError): compact(self.runner, self.run, apply=True)
        (p / '.keep').unlink()
        with patch('team_runner.artifacts.mountpoints', return_value={p}):
            with self.assertRaises(ValueError): compact(self.runner, self.run, apply=True)
        self.assertTrue(p.exists())

    def test_scope_and_manifest_tampering_rejected(self):
        self.tree()
        with self.assertRaises(ValueError): compact(self.runner, self.root, apply=True)
        compact(self.runner, self.run, apply=True)
        m = self.run / '.artifact-archives/copy.manifest.json'; m.write_text('{}')
        with self.assertRaises(ValueError): restore(self.runner, self.run, 'copy')

    def test_repeat_and_explicit_sweep(self):
        self.tree()
        self.assertFalse(sweep(self.runner, apply=True)['runs'])  # no settlement registry
        result = sweep(self.runner, attempt=self.root / 'attempts/attempt', apply=True)
        self.assertGreater(result['released_bytes'], 0)
        self.assertEqual(compact(self.runner, self.run, apply=True)['released_bytes'], 0)

    def test_child_cannot_access_another_attempt(self):
        self.tree()
        with patch.dict(os.environ, {'TEAM_RUNNER_ATTEMPT': str(self.root / 'attempts/other')}):
            with self.assertRaises(ValueError): compact(self.runner, self.run, apply=True)
            with self.assertRaises(ValueError): restore(self.runner, self.run, 'copy')
            with self.assertRaises(ValueError): sweep(self.runner, apply=True)

    def test_prepare_refuses_low_disk_before_starting_command(self):
        attempt = self.root / 'attempts/attempt'
        self.runner.load_state = lambda: {'current': {'attempt_dir': str(attempt)}}
        self.runner.repo = self.root
        with patch('team_runner.artifacts.shutil.disk_usage', return_value=SimpleNamespace(free=10)), \
             patch('team_runner.artifacts.subprocess.run') as call:
            with self.assertRaises(ValueError): prepare(self.runner, attempt, ['never-run'])
        call.assert_not_called()

    def test_prepare_compacts_then_executes_bounded_command(self):
        self.tree(); attempt = self.root / 'attempts/attempt'
        self.runner.load_state = lambda: {'current': {'attempt_dir': str(attempt)}}
        self.runner.repo = self.root
        with patch('team_runner.artifacts.shutil.disk_usage', return_value=SimpleNamespace(free=100 * 2**30)), \
             patch('team_runner.artifacts.subprocess.run', return_value=SimpleNamespace(returncode=7)) as call:
            result = prepare(self.runner, attempt, ['test-preparer'])
        self.assertEqual(result['exit_code'], 7)
        self.assertGreater(result['released_bytes'], 0)
        call.assert_called_once_with(['test-preparer'], cwd=self.root, check=False)

    def test_interruption_after_complete_removal_is_recoverable(self):
        self.tree()
        import shutil
        def interrupted(tree):
            shutil.rmtree(tree)
            raise OSError('interrupted receipt update')
        with patch('team_runner.artifacts.remove_archived_tree', side_effect=interrupted):
            with self.assertRaises(OSError): compact(self.runner, self.run, apply=True)
        compact(self.runner, self.run, apply=True)
        restore(self.runner, self.run, 'copy')
        self.assertEqual((self.run / 'copy/tool').read_bytes(), b'preserved executable' * 100)

    def test_readonly_tree_and_interrupted_removal_are_recoverable(self):
        p = self.tree(); (p / 'empty/data').write_text('preserve this too')
        (p / 'empty').chmod(0o555)
        original = __import__('team_runner.artifacts', fromlist=['remove_archived_tree']).remove_archived_tree
        def interrupted(tree):
            (tree / 'tool').unlink()
            raise OSError('interrupted removal')
        with patch('team_runner.artifacts.remove_archived_tree', side_effect=interrupted):
            with self.assertRaises(OSError): compact(self.runner, self.run, apply=True)
        compact(self.runner, self.run, apply=True)
        self.assertFalse(p.exists())
        restore(self.runner, self.run, 'copy')
        self.assertTrue((p / 'tool').exists())
        self.assertEqual((p / 'empty/data').read_text(), 'preserve this too')
        self.assertEqual((p / 'empty').stat().st_mode & 0o777, 0o555)
        original(p)


if __name__ == '__main__': unittest.main()
