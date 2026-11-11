#!/usr/bin/env python3
"""Run a verbose command, keep its full output on disk, hand the agent a short receipt.

Test suites, builds and migrations produce output an agent must not carry in its context:
every kilobyte pasted into a session is replayed on every later request of that session.
This wrapper streams stdout and stderr straight into per-run log files outside the
repository and prints a few lines instead: the command, its exit code, duration, the log
paths with sizes and hashes, and — only when the command failed — a bounded diagnostic
excerpt. Every excerpt states how much was left out and where the full text lives.

The child's exit code is never rewritten: `run` exits with it. The wrapper's own failures
use codes the child cannot produce here: 124 timeout (child killed), 125 wrapper error,
127 command not found.

Logs default to $TEAM_RUNNER_ATTEMPT/logs/, or, outside an attempt,
$XDG_STATE_HOME/team-runner/runs/<run-or-unbound>/logs/ (XDG_STATE_HOME defaults to
~/.local/state). --logs-dir and TEAM_RUNNER_LOGS override this, in that order.
A log directory inside a Git work tree is refused so that nothing lands in the
repository by accident.

Commands
  run   --label L [--run R] [--cwd DIR] [--timeout S] [--tail N] [--fail-tail N] -- CMD [ARG...]
  show  --log PATH [--head N | --tail N | --grep RE [--context N]] [--max-lines N]

`show` is how the next agent (tester, reviewer, coordinator) reads the original selectively:
it reads the same untouched file and caps what it prints, so the evidence stays checkable
without being copied into a session. Commands run through subprocess with shell=False, so
repository content and worker text stay inert data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time

DEFAULT_TAIL = 5
DEFAULT_FAIL_TAIL = 20
DEFAULT_SHOW_LINES = 200
MAX_LINE_CHARS = 500
DEFAULT_MARKERS = ("FAIL", "PASS", "SKIP", "panic:", "Error", "error:", "warning:")


def logs_dir(run: str | None, override: str | None) -> str:
    override = override or os.environ.get("TEAM_RUNNER_LOGS")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    attempt = os.environ.get("TEAM_RUNNER_ATTEMPT")
    if attempt:
        base = os.path.join(attempt, "logs")
    else:
        state = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
        base = os.path.join(state, "team-runner", "runs", run or "unbound", "logs")
    return os.path.abspath(os.path.expanduser(base))


def nearest_existing(path: str) -> str:
    """The deepest existing ancestor of path; the log directory may not exist yet."""
    current = os.path.abspath(path)
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            return current
        current = parent
    return current


def git_dir_ancestor(path: str) -> str | None:
    """Fallback used only when git cannot be run: nearest ancestor holding a .git entry."""
    current = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def inside_git_worktree(path: str) -> str | None:
    """Return the work tree root containing path, or None. Keeps logs out of the repository.

    Git itself answers this: a stray or empty `.git` directory (for example in /tmp) is not a
    work tree and must not block logging there.
    """
    probe = nearest_existing(path)
    try:
        done = subprocess.run(["git", "-C", probe, "rev-parse", "--show-toplevel"],
                              shell=False, capture_output=True, text=True)
    except OSError:
        return git_dir_ancestor(probe)
    if done.returncode == 0 and done.stdout.strip():
        return done.stdout.strip()
    return None


def unique_paths(directory: str, label: str) -> tuple[str, str]:
    """Never overwrite evidence: a repeated label gets .2, .3 … suffixes."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", label) or "command"
    attempt = 1
    while True:
        suffix = "" if attempt == 1 else f".{attempt}"
        out = os.path.join(directory, f"{safe}{suffix}.stdout.log")
        err = os.path.join(directory, f"{safe}{suffix}.stderr.log")
        if not os.path.exists(out) and not os.path.exists(err):
            return out, err
        attempt += 1


def file_facts(path: str) -> dict:
    """Size, line count and hash without ever holding the log in memory."""
    lines = 0
    last = b""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if last and last != b"\n":
        lines += 1  # trailing fragment without a newline is still a line
    return {"path": path, "bytes": os.path.getsize(path), "lines": lines,
            "sha256": digest.hexdigest()}


def clip(line: str) -> str:
    line = line.rstrip("\n")
    if len(line) <= MAX_LINE_CHARS:
        return line
    return f"{line[:MAX_LINE_CHARS]}… [truncated line: +{len(line) - MAX_LINE_CHARS} chars]"


def tail_lines(path: str, count: int) -> list[str]:
    if count <= 0:
        return []
    kept: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                kept.append(line)
                if len(kept) > count:
                    kept.pop(0)
    return kept


def count_markers(path: str, markers: tuple[str, ...]) -> dict[str, int]:
    found = {marker: 0 for marker in markers}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            for marker in markers:
                if marker in line:
                    found[marker] += 1
    return {marker: n for marker, n in found.items() if n}


def excerpt(stream: str, facts: dict, count: int) -> list[str]:
    """Bounded excerpt that always states what it left out and where the original is."""
    if facts["lines"] == 0:
        return [f"--- {stream}: empty ---"]
    lines = tail_lines(facts["path"], count)
    shown = len(lines)
    if shown < facts["lines"]:
        header = (f"--- {stream}: last {shown} non-empty of {facts['lines']} lines "
                  f"(TRUNCATED; full log: {facts['path']}) ---")
    else:
        header = f"--- {stream}: all {shown} lines ---"
    return [header] + [clip(line) for line in lines]


def index_append(directory: str, record: dict) -> str:
    """One JSON line per invocation, so a later agent can find evidence without a listing."""
    path = os.path.join(directory, "run-index.jsonl")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def do_run(args: argparse.Namespace) -> int:
    if not args.command:
        sys.stderr.write("run: no command given (use -- CMD [ARG...])\n")
        return 125
    directory = logs_dir(args.run, args.logs_dir)
    repo = inside_git_worktree(directory)
    if repo and not args.allow_repo_logs:
        sys.stderr.write(f"run: refusing to write logs inside the Git work tree {repo}\n"
                         f"     log directory: {directory}\n"
                         f"     use --logs-dir outside the repository (or --allow-repo-logs)\n")
        return 125
    os.makedirs(directory, exist_ok=True)
    out_path, err_path = unique_paths(directory, args.label)

    started = time.time()
    timed_out = False
    try:
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            process = subprocess.Popen(args.command, shell=False, cwd=args.cwd,
                                       stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                       start_new_session=True)
            try:
                code = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                code = 124
                # only this child's own process group, never a name pattern
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        break
                    try:
                        process.wait(timeout=10)
                        break
                    except subprocess.TimeoutExpired:
                        continue
    except FileNotFoundError as exc:
        sys.stderr.write(f"run: command not found: {exc.filename}\n")
        return 127
    except OSError as exc:
        sys.stderr.write(f"run: cannot start command: {exc}\n")
        return 125
    duration = time.time() - started

    out_facts, err_facts = file_facts(out_path), file_facts(err_path)
    markers = count_markers(out_path, tuple(args.marker or DEFAULT_MARKERS)) if out_facts["lines"] else {}

    print(f"CMD     {' '.join(args.command)}")
    print(f"EXIT    {code}" + ("  (TIMEOUT: child killed after "
                               f"{args.timeout}s; exit code is the wrapper's 124)" if timed_out else ""))
    print(f"TIME    {duration:.1f}s")
    if args.cwd:
        print(f"CWD     {args.cwd}")
    print(f"STDOUT  {out_facts['path']}  ({out_facts['lines']} lines, {out_facts['bytes']} B, "
          f"sha256 {out_facts['sha256'][:16]})")
    print(f"STDERR  {err_facts['path']}  ({err_facts['lines']} lines, {err_facts['bytes']} B, "
          f"sha256 {err_facts['sha256'][:16]})")
    if markers:
        print("MARKERS " + ", ".join(f"{k}={v}" for k, v in sorted(markers.items())))

    if code == 0 and not timed_out:
        for line in excerpt("stdout", out_facts, args.tail):
            print(line)
    else:
        for line in excerpt("stderr", err_facts, args.fail_tail):
            print(line)
        for line in excerpt("stdout", out_facts, args.fail_tail):
            print(line)
    print(f"READ    python3 -m team_runner.run_logged show --log {out_facts['path']} "
          f"[--grep RE | --head N | --tail N]")

    index = index_append(directory, {
        "label": args.label,
        "command": args.command,
        "cwd": args.cwd or os.getcwd(),
        "exit": code,
        "timed_out": timed_out,
        "duration_s": round(duration, 1),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started)),
        "stdout": out_facts,
        "stderr": err_facts,
    })
    print(f"INDEX   {index}")
    return code


def do_show(args: argparse.Namespace) -> int:
    path = os.path.abspath(os.path.expanduser(args.log))
    if not os.path.exists(path):
        sys.stderr.write(f"show: no such log: {path}\n")
        return 125
    facts = file_facts(path)
    limit = args.max_lines
    selected: list[tuple[int, str]] = []
    cap_hit = False

    if args.grep:
        pattern = re.compile(args.grep)
        window: list[tuple[int, str]] = []
        pending = 0
        with open(path, encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if pattern.search(line):
                    selected.extend(window[-args.context:] if args.context else [])
                    window.clear()
                    selected.append((number, line))
                    pending = args.context
                elif pending:
                    selected.append((number, line))
                    pending -= 1
                else:
                    window.append((number, line))
                    if len(window) > args.context:
                        window.pop(0)
                if len(selected) >= limit:
                    cap_hit = True
                    break
    elif args.head:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                selected.append((number, line))
                if len(selected) >= min(args.head, limit):
                    cap_hit = len(selected) >= limit
                    break
    else:
        count = min(args.tail or DEFAULT_SHOW_LINES, limit)
        kept: list[tuple[int, str]] = []
        with open(path, encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                kept.append((number, line))
                if len(kept) > count:
                    kept.pop(0)
        selected = kept

    print(f"LOG     {path}  ({facts['lines']} lines, {facts['bytes']} B, "
          f"sha256 {facts['sha256'][:16]})")
    # A filter is not a truncation: only the --max-lines cap hides lines the caller asked for.
    if args.grep:
        note = f"CAP REACHED (--max-lines {limit}): more matches exist" if cap_hit \
            else "all matches shown"
        print(f"SHOWING {len(selected)} line(s) of {facts['lines']} "
              f"(grep {args.grep!r}; {note})")
    elif len(selected) < facts["lines"]:
        mode = "head" if args.head else "tail"
        print(f"SHOWING {len(selected)} of {facts['lines']} lines ({mode}; TRUNCATED — "
              f"the full original is unchanged at the path above)")
    else:
        print(f"SHOWING all {facts['lines']} lines "
              f"({'head' if args.head else 'tail'})")
    for number, line in selected:
        print(f"{number}: {clip(line)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    run_parser = sub.add_parser("run", help="run a command, log it in full, print a receipt")
    run_parser.add_argument("--label", required=True, help="log file name stem, e.g. go-test-db")
    run_parser.add_argument("--run", default=os.environ.get("TEAM_RUNNER_RUN"), help="Orca run id")
    run_parser.add_argument("--logs-dir", help="override the log directory (must be outside Git)")
    run_parser.add_argument("--cwd", help="working directory for the command")
    run_parser.add_argument("--timeout", type=float, help="seconds before the child is killed")
    run_parser.add_argument("--tail", type=int, default=DEFAULT_TAIL,
                            help=f"stdout lines shown on success (default {DEFAULT_TAIL})")
    run_parser.add_argument("--fail-tail", type=int, default=DEFAULT_FAIL_TAIL,
                            help=f"lines per stream shown on failure (default {DEFAULT_FAIL_TAIL})")
    run_parser.add_argument("--marker", action="append",
                            help="substring counted in stdout and reported (repeatable); "
                                 "given at least once, it replaces the defaults")
    run_parser.add_argument("--allow-repo-logs", action="store_true",
                            help="permit a log directory inside a Git work tree")
    run_parser.add_argument("command", nargs=argparse.REMAINDER,
                            help="-- followed by the command and its arguments")

    show_parser = sub.add_parser("show", help="read part of an existing log")
    show_parser.add_argument("--log", required=True)
    show_parser.add_argument("--head", type=int)
    show_parser.add_argument("--tail", type=int)
    show_parser.add_argument("--grep")
    show_parser.add_argument("--context", type=int, default=0, help="lines around each match")
    show_parser.add_argument("--max-lines", type=int, default=DEFAULT_SHOW_LINES,
                             help=f"hard cap on printed lines (default {DEFAULT_SHOW_LINES})")

    args = parser.parse_args(argv)
    if args.mode == "run":
        if args.command and args.command[0] == "--":
            args.command = args.command[1:]
        return do_run(args)
    return do_show(args)


if __name__ == "__main__":
    sys.exit(main())
