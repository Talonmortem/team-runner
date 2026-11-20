import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from team_runner import cascade_verification as cascade, runner_operations
from team_runner.runner_store import lock


class CascadeVerificationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.checkout = self.directory / 'checkout'
        self.checkout.mkdir()
        self.attempt = self.directory / 'attempt'
        self.attempt.mkdir()
        self.source = self.checkout / 'source.py'
        self.source.write_text('value = 1\n')
        (self.checkout / '.gitignore').write_text('local-settings.json\n')
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                 'commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.coverage = {'extra_inputs': [], 'checks': [
            {'argv': [sys.executable, '-c', 'print("fixture checked")'],
             'cwd': '.', 'toolchain': 'Python fixture', 'environment': {}}]}
        self.assignments = {role: {'role': role, 'run': 'manager' if role == 'coordinator' else 'child',
                                   'task': 'task-' + role, 'dispatch': 'dispatch-' + role,
                                   'checkout': str(self.checkout), 'attempt': 'attempt-id', 'execution': 'execution-id'}
                            for role in cascade.REPORT_ROLES}

    def git(self, *argv):
        return subprocess.run(['git', '-C', str(self.checkout), *argv],
                              capture_output=True, text=True, check=True).stdout

    def freeze(self):
        return cascade.freeze_candidate(self.checkout, self.base, self.attempt, coverage=self.coverage)

    def guard(self, candidate):
        if sys.platform != 'linux' or not shutil.which('bwrap', path='/usr/bin:/bin'):
            self.skipTest('local bubblewrap is unavailable')
        root = self.attempt / 'artifacts'
        root.mkdir(exist_ok=True)
        try:
            return cascade.probe_readonly(self.checkout, candidate, [root])
        except ValueError as error:
            if 'read-only verification probe failed' in str(error) and (
                    'Operation not permitted' in str(error) or 'No permissions' in str(error)):
                self.skipTest(str(error))
            raise

    def reports(self):
        return {role: {'role': role, 'outcome': 'succeeded', 'sha': self.base, 'evidence': []}
                for role in cascade.REPORT_ROLES}

    def protect_assignments(self, candidate):
        current = cascade.boundary(self.checkout, candidate)
        guard = self.guard(candidate)
        for role in cascade.CHECK_ROLES:
            self.assignments[role].update(readonly=guard, before=current, after=current)

    def accept(self, candidate, reports, receipts):
        return cascade.accept(self.checkout, self.base, candidate, self.assignments, reports, receipts)

    def test_frozen_scope_requires_explicit_input_contract(self):
        card = '# Check\n\n```cascade-verification\n' + json.dumps(self.coverage) + '\n```\n'
        self.assertEqual(cascade.coverage_from_card(card), self.coverage)
        for bad in ('# Unspecified', card + card,
                    card.replace('"extra_inputs": []', '"extra_inputs": [], "extra_inputs": []')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cascade.coverage_from_card(bad)

    def test_candidate_rejects_uncovered_ignored_and_tracks_declared_extra_inputs(self):
        local = self.checkout / 'local-settings.json'
        local.write_text('{"value": 1}')
        with self.assertRaisesRegex(ValueError, 'ignored checkout input has no frozen coverage'):
            self.freeze()
        fixture = self.directory / 'external'
        fixture.mkdir()
        (fixture / 'case.txt').write_text('case')
        self.coverage['extra_inputs'] = [local.name, str(fixture)]
        candidate = self.freeze()
        self.assertEqual(candidate['sha'], self.base)
        self.assertEqual(candidate, self.freeze())
        self.assertEqual(cascade.boundary(self.checkout, candidate)['result'], 'PASS')
        (fixture / 'case.txt').write_text('other case')
        with self.assertRaisesRegex(ValueError, 'extra inputs changed'):
            cascade.boundary(self.checkout, candidate)

    def test_dirty_check_cannot_be_accepted_after_restoring_clean_tree(self):
        candidate = self.freeze()
        self.protect_assignments(candidate)
        self.source.write_text('value = 2\n')
        with self.assertRaisesRegex(ValueError, 'clean Git index and worktree'):
            cascade.run_check(self.checkout, candidate, self.assignments['tester'], 0, self.attempt)
        self.source.write_text('value = 1\n')
        with self.assertRaisesRegex(ValueError, 'missing frozen verification commands'):
            self.accept(candidate, self.reports(), [])

    def test_acceptance_rejects_changed_runner_harness_after_pass(self):
        candidate = self.freeze()
        self.protect_assignments(candidate)
        receipt = cascade.run_check(self.checkout, candidate, self.assignments['tester'], 0, self.attempt)
        reports = self.reports()
        reports['tester']['evidence'].append(receipt['reference'])
        self.assertEqual(self.accept(candidate, reports, [receipt])['sha'], self.base)
        changed_harness = self.directory / 'verification.py'
        changed_harness.write_text('# newer installed Runner')
        with patch.object(cascade.verification, '__file__', str(changed_harness)):
            with self.assertRaisesRegex(ValueError, 'verification harness, toolchain or environment changed'):
                self.accept(candidate, reports, [receipt])

    def test_command_cannot_modify_and_restore_candidate(self):
        script = (
            'import errno, pathlib\n'
            'path = pathlib.Path("source.py")\n'
            'try:\n'
            '    path.write_bytes(path.read_bytes())\n'
            'except OSError as error:\n'
            '    assert error.errno == errno.EROFS, error\n'
            'else:\n'
            '    raise RuntimeError("candidate was writable")\n'
            'assert path.read_text() == "value = 1\\n"\n'
            'print("read-only open verified")\n'
        )
        self.coverage['checks'][0]['argv'] = [sys.executable, '-c', script]
        candidate = self.freeze()
        self.protect_assignments(candidate)
        descriptor = cascade.run_check(self.checkout, candidate, self.assignments['tester'], 0, self.attempt)
        self.assertEqual(descriptor['result'], 'PASS')
        reports = self.reports()
        reports['tester']['evidence'].append(descriptor['reference'])
        acceptance = self.accept(candidate, reports, [descriptor])
        self.assertEqual(acceptance['sha'], self.base)
        self.assertEqual(acceptance['evidence']['tester']['checks'], [descriptor])
        self.assertEqual(self.source.read_text(), 'value = 1\n')

        report = reports.pop('reviewer')
        with self.assertRaisesRegex(ValueError, 'all three worker reports'):
            self.accept(candidate, reports, [descriptor])
        reports['reviewer'] = dict(report, sha='1' * len(self.base))
        with self.assertRaisesRegex(ValueError, 'frozen revision'):
            self.accept(candidate, reports, [descriptor])
        reports['reviewer'] = report

        receipt = self.attempt / descriptor['reference']
        unmodified = receipt.read_text()
        receipt.write_text(unmodified + ' ')
        with self.assertRaisesRegex(ValueError, 'receipt digest changed'):
            self.accept(candidate, reports, [descriptor])
        receipt.write_text(unmodified)
        proof = json.loads((self.attempt / json.loads(unmodified)['verification']['reference']).read_text())
        Path(proof['logs'][0]['path']).write_text('fabricated log')
        with self.assertRaisesRegex(ValueError, 'log digest changed'):
            self.accept(candidate, reports, [descriptor])

    def test_role_cannot_wrap_checkout_or_receipt_directory_in_write_mount(self):
        candidate = self.freeze()
        self.guard(candidate)
        for forbidden in (self.checkout, self.attempt, self.directory, self.attempt / 'candidate-manifest.json'):
            with self.subTest(path=forbidden), self.assertRaisesRegex(ValueError, 'overlaps'):
                cascade.readonly_argv(['true'], self.checkout, [forbidden], candidate=candidate, role=True)

    def test_existing_hard_link_in_write_mount_is_rejected(self):
        candidate = self.freeze()
        guard = self.guard(candidate)
        root = Path(guard['writable_roots'][0])
        os.link(self.source, root / 'alias')
        with self.assertRaisesRegex(ValueError, 'hard-linked'):
            cascade.readonly_argv(['true'], self.checkout, [root], candidate=candidate, role=True)

    def test_role_probe_requires_actual_sandbox_command_execution(self):
        candidate = self.freeze()
        guard = self.guard(candidate)
        root = guard['writable_roots']
        launcher = [sys.executable, '-c',
                    'import os,sys; assert sys.argv[1:3] == ["sandbox", "--"];'
                    'assert os.environ["TEAM_RUNNER_PROBE"] == "present";'
                    'os.execv(sys.argv[3], sys.argv[3:])']
        actual = cascade.probe_readonly(self.checkout, candidate, root, codex_argv=launcher,
                                        environment=os.environ | {'TEAM_RUNNER_PROBE': 'present'})
        self.assertEqual(actual, guard)
        with self.assertRaisesRegex(ValueError, 'did not confirm execution'):
            cascade.probe_readonly(self.checkout, candidate, root,
                                   codex_argv=[sys.executable, '-c', 'pass'])
        with self.assertRaisesRegex(ValueError, 'Codex shell: namespace refused'):
            cascade.probe_readonly(
                self.checkout, candidate, root,
                codex_argv=[sys.executable, '-c',
                            'import sys; print("namespace refused", file=sys.stderr); sys.exit(1)'])

    def test_recover_completed_failed_command_with_inherited_operation_lock(self):
        self.coverage['checks'][0]['argv'] = [sys.executable, '-c', 'raise SystemExit(7)']
        candidate = self.freeze()
        self.protect_assignments(candidate)
        operation = str(uuid.uuid4())
        args = (self.checkout, candidate, self.assignments['tester'], 0, self.attempt)
        self.assertIsNone(cascade.recover_check(*args, operation))
        with lock(self.attempt / 'effect.lock') as fd:
            token = runner_operations.inherited_lock.set(fd)
            try:
                descriptor = cascade.run_check(*args, operation_id=operation)
            finally:
                runner_operations.inherited_lock.reset(token)
        self.assertEqual(descriptor['result'], 'FAIL')
        self.assertEqual(cascade.recover_check(*args, operation), descriptor)
        with self.assertRaisesRegex(ValueError, 'already started'):
            cascade.run_check(*args, operation_id=operation)
        foreign = {**self.assignments['tester'], 'dispatch': 'replacement'}
        with self.assertRaisesRegex(ValueError, 'another assignment'):
            cascade.recover_check(self.checkout, candidate, foreign, 0, self.attempt, operation)


if __name__ == '__main__':
    unittest.main()
