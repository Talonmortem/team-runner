"""Scripted proc, clock and Orca checks for shared agent launch admission."""
import os
from pathlib import Path
import select
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import test_platform as platform
import test_team_graph as graph_tests
from memory_fixtures import FakeProc
from team_runner import application, graph_dispatch, memory_admission, network_recovery, protocol
from team_runner.runner_store import atomic_json, enqueue, lock, read_json


class QueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.proc = FakeProc(self.root / 'proc')
        self.now = 100
        self.queue = self.client()

    def client(self):
        return memory_admission.MemoryQueue(self.root / 'shared', self.root / 'proc', lambda: self.now)

    def wait(self, checkout, operation, queue=None):
        with self.assertRaises(memory_admission.WaitingMemory) as waiting:
            with (queue or self.queue).hold(checkout, checkout + '/attempt', operation):
                self.fail('admitted while blocked')
        return waiting.exception.status

    def test_thresholds_and_two_recovery_samples(self):
        self.proc.memory(3_199_999)
        self.proc.psi(5, 1)
        initial = self.wait('first', 'coordinator')
        self.assertEqual(initial['reason'], 'low_available_memory, high_memory_psi')
        self.assertEqual(initial['metrics']['floor_kib'], 3_200_000)
        self.assertEqual(initial['metrics']['resume_kib'], 3_724_288)
        self.proc.memory(3_724_288)
        self.proc.psi(2, .49)
        self.now += 2
        self.wait('first', 'coordinator')
        self.proc.psi(1.99, .5)
        self.now += 2
        self.wait('first', 'coordinator')
        self.proc.psi(1.99, .49)
        self.now += 2
        self.wait('first', 'coordinator')
        self.now += 1
        self.wait('first', 'coordinator')
        self.now += 1
        with self.queue.hold('first', 'first/attempt', 'coordinator'):
            self.assertIsNone(self.queue.view('first', 'first/attempt'))

    def test_threshold_cap_and_independent_signal_fallback(self):
        self.proc.memory(4 * 1024**2, total=64 * 1024**2)
        (self.proc.root / 'pressure/memory').unlink()
        with self.queue.hold('first', 'first/attempt', 'coordinator'):
            pass
        self.assertEqual(self.queue.metrics()['floor_kib'], 4 * 1024**2)
        self.proc.memory(999_999, total=5_000_000)
        self.assertEqual(self.wait('second', 'delegate')['reason'], 'low_available_memory')
        self.assertEqual(self.queue.metrics()['resume_kib'], 1_250_000)
        (self.proc.root / 'meminfo').unlink()
        self.proc.psi(some=0, full=1)
        self.now += 2
        self.assertEqual(self.wait('second', 'delegate')['reason'], 'high_memory_psi')
        (self.proc.root / 'pressure/memory').unlink()
        with self.assertLogs(memory_admission.__name__, level='WARNING') as messages:
            self.assertEqual(self.wait('second', 'delegate')['diagnostic'], memory_admission.NO_METRICS)
            self.now += 3
            with self.queue.hold('second', 'second/attempt', 'delegate'):
                pass
        self.assertEqual(len(messages.output), 1)

    def test_fifo_across_checkouts_and_cooldown_after_failure(self):
        second_client = self.client()
        with self.assertRaisesRegex(ValueError, 'startup failed'):
            with self.queue.hold('first', 'first/attempt', 'one'):
                self.assertEqual(self.wait('second', 'two', second_client)['reason'], 'launch_in_progress')
                self.wait('third', 'three')
                raise ValueError('startup failed')
        self.now += 4
        self.assertEqual(self.wait('third', 'three')['reason'], 'launch_cooldown')
        self.now += 1
        self.assertEqual(self.wait('third', 'three')['reason'], 'fifo_queue')
        with second_client.hold('second', 'second/attempt', 'two'):
            self.assertEqual(self.wait('third', 'three')['reason'], 'launch_in_progress')
        self.now += 5
        with self.queue.hold('third', 'third/attempt', 'three'):
            self.assertEqual(self.queue.directory.stat().st_mode & 0o777, 0o700)

    def test_dead_owner_pid_reuse_expiry_and_boot_change(self):
        other_pid = os.getpid() + 10000
        self.proc.process(other_pid, 'previous')
        with patch.object(memory_admission.os, 'getpid', return_value=other_pid):
            holder = self.queue.hold('other', 'other/attempt', 'one')
            holder.__enter__()
            self.wait('other', 'stale')
        self.proc.process(other_pid, 'reused')
        self.assertEqual(self.wait('first', 'two')['reason'], 'launch_cooldown')
        self.now += 5
        with self.queue.hold('first', 'first/attempt', 'two'):
            self.wait('idle', 'three')
        self.now += 31
        with self.queue.hold('first', 'first/attempt', 'four'):
            self.assertIsNone(self.queue.view('idle', 'idle/attempt'))
        holder.__exit__(None, None, None)
        self.proc.boot('new-boot')
        with self.queue.hold('last', 'last/attempt', 'five'):
            pass


class LaunchTests(unittest.TestCase):
    setUp = platform.Platform.setUp
    attempt = platform.Platform.attempt

    def _transport(self):
        return read_json(self.a.launcher.orca.root / 'transport.json', {})

    def test_state_view_reads_profile_from_captured_attempt(self):
        old = self.attempt(self.a)
        snapshot = self.a.load_state()
        snapshot['goal'] = application.goals(self.a)[0]
        new = self.attempt(self.a)
        context_path = new / 'context.json'
        context = read_json(context_path)
        context['profile']['tracking_root'] = 'other-tracking-root'
        atomic_json(context_path, context)
        load = self.a.load_state
        states = iter([snapshot])
        with patch.object(self.a, 'load_state', side_effect=lambda: next(states, None) or load()):
            view = application.state_view(self.a)
        self.assertEqual(view['state']['current']['attempt_dir'], str(old))
        self.assertTrue(view['queue'])
        self.assertEqual(self.a.load_state()['current']['attempt_dir'], str(new))

    def test_coordinator_processes_pause_and_stop_while_waiting(self):
        for action in ('pause', 'stop'):
            with self.subTest(action=action):
                attempt = self.attempt(self.a)
                self.a.orca = self.a.launcher.orca
                state = self.a.load_state()
                atomic_json(self.a.state_dir / 'recovery-settings.json', {'enabled': False, 'intent': 'running'})
                before = self._transport()
                self.proc.memory(0)
                command = None

                def control(delay):
                    nonlocal command
                    wait = self.admission.view(self.a.state_dir, attempt)
                    self.assertEqual(wait['state'], 'waiting_memory')
                    self.assertEqual(wait['reason'], 'low_available_memory')
                    self.assertEqual(application.state_view(self.a)['health']['state'], 'waiting_memory')
                    summary = protocol.summary(self.a)
                    self.assertEqual(summary['observed_state'], 'waiting_memory')
                    self.assertEqual(summary['available_actions'], [{'action': 'pause'}, {'action': 'stop'}])
                    self.assertEqual(self._transport(), before)
                    with lock(attempt / 'agents.lock', blocking=False), lock(self.a.state_dir / 'goal-intent.lock', blocking=False):
                        command = enqueue(self.a.state_dir, action)

                with patch.object(self.admission, 'sleep', side_effect=control) as sleep:
                    result = self.a.start_coordinator(SimpleNamespace(mode='auto'), state, 'T1', 'spec', 'title')
                sleep.assert_called_once_with(2)
                self.assertEqual(result, 'paused_gate' if action == 'pause' else 'stopped_attempt')
                self.assertIsNone(self.admission.view(self.a.state_dir, attempt))
                self.assertEqual(self._transport(), before)
                self.assertEqual(read_json(self.a.state_dir / 'commands' / (command['id'] + '.json'))['status'], 'done')

    def test_graph_does_not_consume_edge_or_create_terminal_before_admission(self):
        attempt, parent = graph_tests.GraphExecution.prepare(self)
        before = self._transport()
        identifier = str(uuid.uuid4())
        self.proc.psi(some=5)

        def recover(delay):
            with lock(attempt / 'agents.lock', blocking=False):
                reg = self.a.launcher.registry(attempt)
                self.assertEqual(reg.get('edge_passes', {}), {})
                self.assertEqual(reg.get('graph_calls', []), [])
                self.assertEqual(reg['agents'][0]['status'], 'active')
                self.assertEqual(self.admission.view(self.a.state_dir, attempt)['operation'], 'delegate:' + identifier)
            self.assertEqual(self._transport(), before)
            self.proc.psi()

        with patch.object(self.admission, 'sleep', side_effect=recover) as sleep:
            call, child = graph_tests.GraphExecution.delegate(self, attempt, parent, request=identifier)
        self.assertGreaterEqual(sleep.call_count, 2)
        self.assertEqual(call['id'], identifier)
        self.assertTrue(child['terminal'])
        self.assertEqual(self.a.launcher.registry(attempt)['edge_passes'], {'implement': 1})

    def test_interrupted_delegate_can_retry_same_request(self):
        attempt, parent = graph_tests.GraphExecution.prepare(self)
        identifier = str(uuid.uuid4())
        self.proc.memory(0)
        with patch.object(self.admission, 'sleep', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            graph_tests.GraphExecution.delegate(self, attempt, parent, request=identifier)
        self.assertIsNone(self.admission.view(self.a.state_dir, attempt))
        self.assertEqual(self.a.launcher.registry(attempt)['agents'][0]['status'], 'active')
        self.proc.memory()
        with patch.object(self.admission, 'sleep'):
            call, _ = graph_tests.GraphExecution.delegate(self, attempt, parent, request=identifier)
        self.assertEqual(call['id'], identifier)
        self.assertEqual(call['status'], 'running')

    def test_killed_granted_delegate_cannot_strand_parent(self):
        cfg = graph_tests.small_graph()
        cfg['edges'][0]['max_passes'] = 1
        attempt, parent = graph_tests.GraphExecution.prepare(self, cfg)
        delegate = graph_tests.GraphExecution.delegate
        _, child = delegate(self, attempt, parent)
        graph_tests.GraphExecution.finish(self, attempt, child)
        limited, _ = delegate(self, attempt, parent)
        graph_dispatch.grant(self.a.launcher, attempt, limited['id'], str(uuid.uuid4()))
        self.proc.memory(0)
        reader, writer = os.pipe()
        pid = os.fork()
        if not pid:
            os.close(reader)
            try:
                owner = self.proc.root / str(os.getpid())
                owner.symlink_to(Path('/proc') / str(os.getpid()))
                def waiting(delay):
                    os.write(writer, b'1')
                    signal.pause()
                self.admission.sleep = waiting
                delegate(self, attempt, parent, request=limited['id'])
            finally:
                os._exit(1)
        os.close(writer)
        try:
            self.assertTrue(select.select([reader], [], [], 5)[0], 'child did not queue')
            self.assertEqual(os.read(reader, 1), b'1')
            self.assertIsNotNone(self.admission.view(self.a.state_dir, attempt))
        finally:
            os.close(reader)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        self.assertIsNone(self.admission.view(self.a.state_dir, attempt))
        reg = self.a.launcher.registry(attempt)
        self.assertEqual(reg['agents'][0]['status'], 'active')
        self.assertEqual(reg['edge_passes'], {'implement': 1})
        self.assertEqual(graph_dispatch.delegation_request(reg, limited['id'])['status'], 'pending')
        self.proc.memory()
        fresh, _ = delegate(self, attempt, parent)
        self.assertEqual(fresh['status'], 'limit')
        graph_dispatch.grant(self.a.launcher, attempt, fresh['id'], str(uuid.uuid4()))
        with patch.object(self.admission, 'sleep'):
            continued, _ = delegate(self, attempt, parent, request=limited['id'])
        self.assertEqual(continued['status'], 'running')
        self.assertEqual(self.a.launcher.registry(attempt)['edge_passes'], {'implement': 2})

    def test_network_command_retains_id_until_replacement_can_begin(self):
        attempt = self.attempt(self.a)
        self.proc.memory(0)
        command = enqueue(self.a.state_dir, 'network-recover',
                          {'_target': str(attempt), 'scope': {}, 'action': 'replace'})
        path = self.a.state_dir / 'commands' / (command['id'] + '.json')
        with patch.object(network_recovery, 'permitted', return_value=True), patch.object(network_recovery, '_apply') as apply:
            self.a.process_controls(self.a.load_state())
            pending = read_json(path)
            self.assertEqual(pending['status'], 'pending')
            self.assertEqual(pending['result']['state'], 'waiting_memory')
            apply.assert_not_called()
            self.proc.memory()
            self.a.process_controls(self.a.load_state())
            self.a.process_controls(self.a.load_state())
        self.assertEqual(read_json(path)['status'], 'done')
        self.assertEqual(self.a.load_state()['current']['attempt_dir'], str(attempt))
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[-1], command['id'])


if __name__ == '__main__':
    unittest.main()
