#!/usr/bin/env python3
"""Read and acknowledge one batch of a Run mailbox.

Heartbeats are printed as one line each; every other message is printed in full so the coordinator
can act on it. The whole batch is acknowledged afterwards (Orca acks batches, not messages), which is
only available for explicitly selected unmanaged mailboxes. Managed attempts use the persisted projection.

usage: python3 $TEAM_RUNNER_MATERIALS/orca_chk.py [--run RUN] [--terminal HANDLE] [--no-ack]
Defaults: --run $TEAM_RUNNER_RUN, --terminal $TEAM_RUNNER_MAILBOX (else $ORCA_TERMINAL_HANDLE).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys



def payload_of(message: dict) -> dict:
    raw = message.get("payload")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": str(raw)}
    return value if isinstance(value, dict) else {"raw": str(raw)}


def format_messages(result: dict) -> list[str]:
    """Render one check result: heartbeats as one line, other messages with their body."""
    lines = []
    for message in result.get("messages") or []:
        kind = message.get("type") or "?"
        sender = (message.get("from_handle") or "?")[:13]
        payload = payload_of(message)
        if kind == "heartbeat":
            lines.append(f"heartbeat {sender} {payload.get('phase') or message.get('subject') or ''}".rstrip())
            continue
        head = [message.get("id") or "?", kind, sender, message.get("subject") or ""]
        for key in ("taskId", "dispatchId", "outcome"):
            if payload.get(key):
                head.append(f"{key}={payload[key]}")
        lines.append(" ".join(head))
        body = (message.get("body") or "").strip()
        if body:
            lines.append(body)
    return lines


def orca(args: list[str], env: dict) -> dict:
    from team_runner.orca import executable
    proc = subprocess.run([executable(), *args], capture_output=True, text=True,
                          cwd=env.get("TEAM_RUNNER_CHECKOUT") or os.getcwd(), env=env)
    try:
        return json.loads(proc.stdout or "{}")
    except ValueError:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("TEAM_RUNNER_ATTEMPT"):
        raise SystemExit("Managed attempts use team-runner messages; only the runner consumes mailboxes")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", default=os.environ.get("TEAM_RUNNER_RUN"))
    parser.add_argument("--terminal", default=os.environ.get("TEAM_RUNNER_MAILBOX") or os.environ.get("ORCA_TERMINAL_HANDLE"))
    parser.add_argument("--no-ack", action="store_true")
    opts = parser.parse_args(argv)
    if not opts.run or not opts.terminal:
        parser.error("set --run/--terminal or TEAM_RUNNER_RUN and TEAM_RUNNER_MAILBOX")
    env = dict(os.environ, ORCA_TERMINAL_HANDLE=opts.terminal)
    data = orca(["orchestration", "check", "--run", opts.run, "--terminal", opts.terminal, "--json"], env)
    result = data.get("result") or {}
    for line in format_messages(result):
        print(line)
    delivery = result.get("deliveryId")
    if delivery and not opts.no_ack:
        orca(["orchestration", "check", "--run", opts.run, "--terminal", opts.terminal, "--ack", delivery, "--json"], env)
        print("acked", delivery)
    elif not delivery:
        print("no messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
