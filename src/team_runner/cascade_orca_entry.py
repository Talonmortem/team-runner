"""Verify the environment of a cascade terminal before starting its Codex process."""

import os
from pathlib import Path
import sys

from .cascade_orca import _connection, _digest, _validate_launch, require
from .runner_store import atomic_json, read_json


def main():
    path, digest = sys.argv[1:]
    plan = read_json(path)
    require(_digest(plan) == digest, 'cascade launch description changed')
    require(Path.cwd() == Path(plan['checkout']) and
            all(os.environ.get(k) == v for k, v in plan['environment'].items()),
            'cascade terminal inherited different launch parameters')
    marker = read_json(plan['entry'], {})
    require(marker.get('nonce') == plan['nonce'] and marker.get('pid') == os.getpid()
            and marker.get('terminal', {}).get('worktreeId') == plan['worktree']
            and marker.get('terminal', {}).get('handle') == os.environ.get('ORCA_TERMINAL_HANDLE'),
            'cascade terminal wrapper is running in a different Orca pane')
    provider, connection = _connection(Path.cwd(), plan['runtime']['settings'])
    _validate_launch(plan['runtime'], provider, connection)
    atomic_json(plan['ready'], {'nonce': plan['nonce'], 'pid': os.getpid(), 'launch_digest': digest})
    argv = plan['argv']
    if 'image' in (plan.get('guard') or {}):
        # The foreground launcher is outside Docker. The guarded binary and
        # its commands are attested separately inside Docker before dispatch.
        os.execv(argv[0], ['codex-container', *argv[1:]])
    os.execvpe(argv[0], argv, os.environ)


if __name__ == '__main__':
    main()
