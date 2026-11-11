"""Project recovery keeps a blocked workspace off another workspace's path."""
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from team_runner.recovery import Supervisor


class RecoveryIsolation(unittest.TestCase):
    def test_blocked_workspace_does_not_block_other_workspace(self):
        catalog = Mock()
        catalog.read.return_value = {'projects': {'p': {'workspaces': {'slow': {}, 'fast': {}}}}}
        catalog.context.side_effect = lambda project, workspace: SimpleNamespace(workspace=workspace)
        supervisor = Supervisor(catalog, 'p', runner_factory=lambda context: context)
        entered, progressed, release = threading.Event(), threading.Event(), threading.Event()
        def tick(runner):
            if runner.workspace == 'slow':
                entered.set()
                release.wait(3)
            else:
                progressed.set()
        supervisor.tick = tick
        supervisor.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(progressed.wait(2))
        finally:
            release.set()
            supervisor.stop()
        self.assertFalse(supervisor.thread.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in supervisor.threads.values()))
