#!/usr/bin/env python3
"""Shell-free transport for coordinator -> worker text (Orca orchestration).

Every payload (task spec, reply, follow-up, terminal prompt) is read from a file or stdin and
handed to the `orca` CLI as a single argv element through subprocess.run(list, shell=False).
Nothing is ever interpolated into a shell command line, so backticks, $(), quotes and newlines
in worker output or repository content stay inert data.

Subcommands (payload always via --body-file PATH or "-" for stdin):
  start    --task-title T --worktree SEL --agent A [--model M] [--effort E] [--run R]
  task     --task-title T [--deps JSON] [--run R]   (task-create; spec is the payload)
  adopt    --task-title T --terminal HANDLE --worktree SEL [--run R]
           (worker-start --terminal: supervised ownership of a custom-argv terminal)
  reply    --id MESSAGE_ID
  send     --to ADDRESS --subject S
  prompt   --terminal HANDLE [--wait-submit N]      (terminal send --text ... --enter)
  selftest [--run R]                                (round-trips hostile strings through Orca)

Options: --dry-run prints the argv list as JSON and executes nothing; --json-out prints the
CLI receipt. Exit code mirrors the orca CLI.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HOSTILE = "`id`\n$(id)\n${HOME}\n\"double\" 'single' \\backslash\n| ; & > < * ?\n$((1+1))\nlast"


def orca_bin() -> str:
    from team_runner.orca import executable
    candidate = executable()
    if not shutil.which(candidate):
        sys.exit('selected Orca CLI unavailable: ' + candidate)
    return candidate


def read_body(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run(argv: list[str], dry_run: bool, json_out: bool) -> int:
    if dry_run:
        print(json.dumps(argv, ensure_ascii=False))
        return 0
    completed = subprocess.run(argv, shell=False, capture_output=True, text=True)
    if json_out or completed.returncode != 0:
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
    else:
        try:
            receipt = json.loads(completed.stdout)
            result = receipt.get("result", receipt)
            summary = {key: result[key] for key in
                       ("runId", "taskId", "dispatchId", "state", "stage", "message", "send")
                       if isinstance(result, dict) and key in result}
            print(json.dumps(summary or result, ensure_ascii=False))
        except json.JSONDecodeError:
            sys.stdout.write(completed.stdout)
    return completed.returncode


def build(args: argparse.Namespace, body: str) -> list[str]:
    orca = orca_bin()
    if args.command == "start":
        argv = [orca, "orchestration", "worker-start", "--spec", body,
                "--task-title", args.task_title, "--worktree", args.worktree, "--agent", args.agent]
        if args.model:
            argv += ["--model", args.model]
        if args.effort:
            argv += ["--effort", args.effort]
        if args.run:
            argv += ["--run", args.run]
        return argv + ["--json"]
    if args.command == "task":
        argv = [orca, "orchestration", "task-create", "--spec", body]
        if args.task_title:
            argv += ["--task-title", args.task_title]
        if args.deps:
            argv += ["--deps", args.deps]
        if args.run:
            argv += ["--run", args.run]
        return argv + ["--json"]
    if args.command == "adopt":
        argv = [orca, "orchestration", "worker-start", "--spec", body,
                "--task-title", args.task_title, "--terminal", args.terminal,
                "--worktree", args.worktree]
        if args.run:
            argv += ["--run", args.run]
        return argv + ["--json"]
    if args.command == "reply":
        return [orca, "orchestration", "reply", "--id", args.id, "--body", body, "--json"]
    if args.command == "send":
        argv = [orca, "orchestration", "send", "--subject", args.subject, "--body", body]
        for name in ('to', 'sender', 'type', 'task_id', 'dispatch_id', 'outcome',
                     'report_path', 'files_modified', 'dispatch_capability'):
            value = getattr(args, name, None)
            if value is not None:
                argv += ['--from' if name == 'sender' else '--' + name.replace('_', '-'), value]
        return argv + ['--json']
    if args.command == "prompt":
        argv = [orca, "terminal", "send", "--terminal", args.terminal, "--text", body, "--enter"]
        if args.wait_submit:
            argv += ["--wait-submit", str(args.wait_submit)]
        return argv + ["--json"]
    raise SystemExit(f"unknown command {args.command}")


def selftest(args: argparse.Namespace) -> int:
    """Round-trip hostile text through Orca mail to this coordinator's own Run inbox.

    Proves the transport delivers bytes unchanged and that nothing executes on the way.
    """
    orca = orca_bin()
    marker_file = os.path.join(os.environ.get("TMPDIR", "/tmp"), "orca_msg_selftest_marker")
    if os.path.exists(marker_file):
        os.remove(marker_file)
    payload = HOSTILE + f"\n`touch {marker_file}`\n$(touch {marker_file})\n"
    send = [orca, "orchestration", "send", "--to", f"run:{args.run}" if args.run else "run:current",
            "--subject", "orca_msg selftest", "--body", payload, "--json"]
    sent = subprocess.run(send, shell=False, capture_output=True, text=True)
    if sent.returncode != 0:
        sys.stderr.write(sent.stdout + sent.stderr)
        return sent.returncode
    message_id = json.loads(sent.stdout)["result"]["message"]["id"]
    peek = subprocess.run([orca, "orchestration", "check", "--peek", "--json"],
                          shell=False, capture_output=True, text=True)
    delivered = None
    for message in json.loads(peek.stdout)["result"].get("messages", []):
        if message["id"] == message_id:
            delivered = message["body"]
    results = {
        "message_id": message_id,
        "delivered_equals_sent": delivered == payload,
        "side_effect_file_created": os.path.exists(marker_file),
        "delivered_found": delivered is not None,
    }
    print(json.dumps(results, ensure_ascii=False))
    ok = results["delivered_found"] and results["delivered_equals_sent"] and not results["side_effect_file_created"]
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["start", "task", "adopt", "reply", "send", "prompt", "selftest"])
    parser.add_argument("--body-file", help='file with the payload, or "-" for stdin')
    parser.add_argument("--task-title")
    parser.add_argument("--worktree")
    parser.add_argument("--agent")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--run")
    parser.add_argument("--task")
    parser.add_argument("--deps")
    parser.add_argument("--terminal")
    parser.add_argument("--id")
    parser.add_argument("--to")
    parser.add_argument("--subject")
    parser.add_argument("--from", dest="sender")
    for name in ('type', 'task-id', 'dispatch-id', 'outcome', 'report-path',
                 'files-modified', 'dispatch-capability'):
        parser.add_argument('--' + name)
    parser.add_argument("--wait-submit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-out", action="store_true")
    args = parser.parse_args()

    if args.command == "selftest":
        return selftest(args)
    if not args.body_file:
        parser.error("--body-file is required")
    return run(build(args, read_body(args.body_file)), args.dry_run, args.json_out)


if __name__ == "__main__":
    sys.exit(main())
