"""Orca transport; every workspace call uses explicit execution context."""
import json
import os
import subprocess
import sys
import shutil
from pathlib import Path
from . import runner_operations as operations


def executable():
    if os.environ.get('ORCA_CLI_COMMAND'):
        return os.environ['ORCA_CLI_COMMAND']
    if os.environ.get('ORCA_DEV_REPO_ROOT'):
        return 'orca-dev'
    if sys.platform == 'linux':
        binary = shutil.which('orca')
        if not os.environ.get('ORCA_TERMINAL_HANDLE') or not binary or Path(binary).resolve() == Path('/usr/bin/orca'):
            return 'orca-ide'
    return 'orca'


def call(*args, timeout=60, mailbox=None, context=None):
    env = dict(os.environ)
    if context:
        env.update(context.environment())
    if mailbox:
        env['ORCA_TERMINAL_HANDLE'] = mailbox
        # Явный адрес исключает привязку Run к унаследованному терминалу процесса.
        if len(args) >= 2 and args[0] == 'orchestration':
            flag = ('--from' if args[1] in ('run-create', 'run-current', 'run-use', 'worker-start')
                    else '--terminal' if args[1] == 'check' else None)
            if flag and flag not in args:
                args = (*args, flag, mailbox)
    # Resolve before Context changes PATH (system Python otherwise selects GNOME Orca).
    command = executable()
    command = shutil.which(command) or command
    # Orchestration's concise view omits records and ownership fields. Other
    # command groups retain their JSON contract and do not accept --full.
    output_flags = ['--json', '--full'] if args[:1] == ('orchestration',) else ['--json']
    p = subprocess.run([command, *map(str, args), *output_flags], env=env,
                       cwd=context.repo if context else None, capture_output=True, text=True, timeout=timeout,
                       pass_fds=((operations.inherited_lock.get(),) if operations.inherited_lock.get() is not None else ()))
    try: value = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f'Orca {args[:2]}: invalid response (exit {p.returncode})') from None
    if p.returncode or not value.get('ok'):
        error = value.get('error') or {}
        exc = RuntimeError(f"Orca {args[:2]}: {error.get('code', 'failed')}: {error.get('message', '')[:500]}")
        exc.receipt = value
        raise exc
    return value.get('result', {})
