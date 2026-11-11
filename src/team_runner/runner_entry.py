#!/usr/bin/env python3
"""Terminal-side receipt before exec. Survives a lost terminal-create response."""
import os
from pathlib import Path
import sys

from .runner_store import atomic_json


def process_stamp(pid):
    try:
        # comm may contain spaces or parentheses; fields after its final ')' start at field 3.
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except OSError:
        return None


def main():
    from .orca import call
    path, nonce, *command = sys.argv[1:]
    if command and command[0] == '--':
        command.pop(0)
    handle = os.environ.get('ORCA_TERMINAL_HANDLE')
    checkout = os.environ.get('TEAM_RUNNER_CHECKOUT')
    if not checkout or Path.cwd().resolve() != Path(checkout).resolve():
        raise RuntimeError('terminal working directory differs from registered checkout')
    if not handle or not command:
        raise RuntimeError('runner entry requires an Orca terminal and command')
    # Never exec twice even if someone manually reuses this wrapper command.
    claim = os.open(str(path) + '.claim', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(claim)
    terminal = call('terminal', 'show', '--terminal', handle)['terminal']
    if terminal.get('handle') != handle or not terminal.get('incarnationId') or terminal.get('worktreeId') != os.environ.get('ORCA_WORKTREE_ID'):
        raise RuntimeError('terminal identity unavailable')
    atomic_json(path, {'nonce': nonce, 'terminal': {k: terminal.get(k) for k in
                ('handle', 'incarnationId', 'ptyId', 'worktreeId')},
                'pid': os.getpid(), 'process_stamp': process_stamp(os.getpid()), 'boot_id': boot_id()})
    os.execvpe(command[0], command, os.environ)


if __name__ == '__main__':
    main()
