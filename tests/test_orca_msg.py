#!/usr/bin/env python3
"""Offline regression tests for team/orca_msg.py argv construction.

They prove hostile payloads (backticks, $(), quotes, newlines, shell metacharacters) survive as
one inert argv element and that no shell is involved. Run: python3 team/tests/test_orca_msg.py
Exit code 0 = all passed. No Orca runtime, network or repository writes are required.
"""
from __future__ import annotations

import importlib.util
import json
import inspect
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_ROOT = os.path.join(os.path.dirname(HERE), 'src')
sys.path.insert(0, SOURCE_ROOT)
MODULE_PATH = os.path.join(os.path.dirname(HERE), "src", "team_runner", "orca_msg.py")

spec = importlib.util.spec_from_file_location("orca_msg", MODULE_PATH)
orca_msg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orca_msg)

PAYLOADS = {
    "backtick": "`touch /tmp/should_not_exist`",
    "dollar_paren": "$(touch /tmp/should_not_exist)",
    "brace_var": "${HOME}/secret",
    "quotes": "\"double\" 'single' `mixed`",
    "newlines": "line one\nline two\n\nline four",
    "metachars": "a | b ; c & d > e < f * g ? h",
    "arith": "$((1+1))",
    "backslash": "c:\\path\\to\\file and \\`escaped",
    "combined": orca_msg.HOSTILE,
}

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"FAIL {name} {detail}")


class Args:
    def __init__(self, **kwargs):
        defaults = dict(command="send", task_title="T", worktree="W", agent="codex", model=None,
                        effort=None, run=None, task=None, deps=None, terminal="term_x", id="msg_1",
                        to="run:r", subject="S", wait_submit=None)
        defaults.update(kwargs)
        for key, value in defaults.items():
            setattr(self, key, value)


def test_payload_is_single_argv_element() -> None:
    for name, payload in PAYLOADS.items():
        for command, args in (
            ("send", Args(command="send")),
            ("reply", Args(command="reply")),
            ("prompt", Args(command="prompt")),
            ("start", Args(command="start")),
            ("task", Args(command="task")),
            ("adopt", Args(command="adopt")),
        ):
            argv = orca_msg.build(args, payload)
            occurrences = [element for element in argv if element == payload]
            check(f"{command}/{name}: payload appears verbatim exactly once",
                  len(occurrences) == 1, f"argv={argv!r}")
            check(f"{command}/{name}: no element merges payload with a flag",
                  not any(payload in element and element != payload for element in argv))


def test_no_shell_usage() -> None:
    source = inspect.getsource(orca_msg)
    check("module never uses shell=True", "shell=True" not in source)
    check("module never uses os.system", "os.system" not in source)
    check("module never uses eval/exec of commands", "eval(" not in source)
    check("every subprocess call passes shell=False",
          source.count("subprocess.run(") == source.count("shell=False"))


def test_body_file_roundtrip() -> None:
    for name, payload in PAYLOADS.items():
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write(payload)
            path = handle.name
        try:
            check(f"body-file/{name}: file content read unchanged",
                  orca_msg.read_body(path) == payload)
        finally:
            os.unlink(path)


def test_dry_run_executes_nothing() -> None:
    """--dry-run must print argv as JSON and create no side effects."""
    marker = os.path.join(tempfile.gettempdir(), "orca_msg_test_marker")
    if os.path.exists(marker):
        os.remove(marker)
    payload = f"`touch {marker}`\n$(touch {marker})\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
        handle.write(payload)
        path = handle.name
    try:
        completed = subprocess.run(
            [sys.executable, MODULE_PATH, "send", "--to", "run:test", "--subject", "s",
             "--body-file", path, "--dry-run"],
            shell=False, capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': SOURCE_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')})
        check("dry-run exits 0", completed.returncode == 0, completed.stderr)
        check("dry-run created no file", not os.path.exists(marker))
        printed_argv = json.loads(completed.stdout)
        check("dry-run printed the payload as one intact argv element",
              printed_argv.count(payload) == 1, f"argv={printed_argv!r}")
    finally:
        os.unlink(path)
        if os.path.exists(marker):
            os.remove(marker)


if __name__ == "__main__":
    test_payload_is_single_argv_element()
    test_no_shell_usage()
    test_body_file_roundtrip()
    test_dry_run_executes_nothing()
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
