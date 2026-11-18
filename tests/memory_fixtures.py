"""Isolated Linux memory metrics and agent-admission clock for Orca fixtures."""
from itertools import count
import os
from pathlib import Path
from unittest.mock import patch

from team_runner import memory_admission


class FakeProc:
    def __init__(self, root):
        self.root = Path(root)
        self.boot('test-boot')
        self.process(os.getpid(), 'test-owner')
        self.memory()
        self.psi()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def boot(self, value):
        self.write('sys/kernel/random/boot_id', value)

    def process(self, pid, stamp, state='S'):
        self.write(f'{pid}/stat', f'{pid} (fake ) agent) ' + ' '.join([state] + ['0'] * 18 + [stamp]))

    def memory(self, available=8_000_000, total=16_000_000):
        self.write('meminfo', f'MemTotal: {total} kB\nMemAvailable: {available} kB\n')

    def psi(self, some=0, full=0):
        self.write('pressure/memory', f'some avg10=0 avg60={some} avg300=0 total=1\n'
                                      f'full avg10=0 avg60={full} avg300=0 total=1\n')


def isolated_admission(test, root):
    proc = FakeProc(Path(root) / 'fake-proc')
    clock = count(step=10)
    queue = memory_admission.MemoryQueue(Path(root) / 'launch-queue', proc.root, lambda: next(clock))
    patcher = patch.object(memory_admission, 'queue', queue)
    patcher.start()
    test.addCleanup(patcher.stop)
    return proc, queue
