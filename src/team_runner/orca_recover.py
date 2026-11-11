#!/usr/bin/env python3
"""Bounded, auditable recovery helpers for an Orca coordinator.

Design rules taken from $TEAM_RUNNER_MATERIALS/GOAL_MASTER.md §6:
  * dispatch/process state and the content verdict are separate facts and are never merged;
  * `working`, a heartbeat or a wait timeout is absence of evidence, not proof of progress;
  * nudges are budgeted (MAX_INFRA_RECOVERY_ATTEMPTS) and recorded, never repeated blindly;
  * a completed report whose delivery failed may be accepted MANUALLY and is recorded as
    delivery_failed + manual acceptance — worker_done is never fabricated;
  * repeated delivery of the same result must not re-trigger fixes or integration (ledger).

All Orca calls go through subprocess.run(list, shell=False); no payload ever reaches a shell.

Commands
  state    --dispatch ID [--run R]     classify one dispatch (JSON)
  bind     --run R                     rebind this coordinator terminal if fenced (bounded)
  nudge    --dispatch ID --terminal H --reason R --body-file F   one budgeted reminder
  accept   --dispatch ID --report PATH --sha SHA --reason TEXT   manual acceptance record
  ledger   --list | --seen KEY | --mark KEY --value JSON         idempotency ledger
State file: $TEAM_RUNNER_RECOVERY_STATE or $TEAM_RUNNER_ATTEMPT/recovery-state.json
Outside an attempt: $XDG_STATE_HOME/team-runner/runs/<run-or-unbound>/recovery-state.json
(XDG_STATE_HOME defaults to ~/.local/state).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

MAX_INFRA_RECOVERY_ATTEMPTS = 2
PROVIDER_ERROR_MARKERS = (
    "502 Bad Gateway",
    "Previous response owner account is unavailable",
    "429",
    "503",
    "stream disconnected",
    "unexpected status",
)


def orca_bin() -> str:
    from team_runner.orca import executable
    candidate = executable()
    if not shutil.which(candidate):
        sys.exit('selected Orca CLI unavailable: ' + candidate)
    return candidate


def orca(*args: str) -> tuple[int, dict | None, str]:
    completed = subprocess.run([orca_bin(), *args], shell=False, capture_output=True, text=True)
    try:
        return completed.returncode, json.loads(completed.stdout), completed.stderr
    except json.JSONDecodeError:
        return completed.returncode, None, completed.stdout + completed.stderr


def state_path(run: str | None) -> str:
    override = os.environ.get("TEAM_RUNNER_RECOVERY_STATE")
    if override:
        return override
    base = os.environ.get("TEAM_RUNNER_ATTEMPT")
    if not base:
        state = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
        base = os.path.join(state, "team-runner", "runs", run or "unbound")
    base = os.path.abspath(os.path.expanduser(base))
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "recovery-state.json")


def load_state(run: str | None) -> dict:
    path = state_path(run)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return {"nudges": {}, "ledger": {}, "acceptances": [], "binds": 0}


def save_state(run: str | None, state: dict) -> None:
    path = state_path(run)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def find_rollout(dispatch: str, sessions_root: str | None = None) -> str | None:
    """Locate the Codex rollout file whose injected preamble carries this dispatch id."""
    root = sessions_root or os.path.expanduser("~/.codex/sessions")
    candidates = sorted(glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True),
                        key=os.path.getmtime, reverse=True)
    for path in candidates[:40]:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                if dispatch in handle.read():
                    return path
        except OSError:
            continue
    return None


def rollout_facts(path: str) -> dict:
    """Last turn outcome, provider error and effective model/effort from a rollout file."""
    facts: dict = {"rollout": path, "provider_error": None, "last_event_ts": None,
                   "last_turn_complete": False, "model": None, "effort": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") or {}
                if event.get("timestamp"):
                    facts["last_event_ts"] = event["timestamp"]
                if event.get("type") == "turn_context":
                    facts["model"] = payload.get("model", facts["model"])
                    facts["effort"] = (payload.get("effort") or payload.get("reasoning_effort")
                                       or facts["effort"])
                if payload.get("type") == "task_complete":
                    facts["last_turn_complete"] = True
                    error = payload.get("error") or {}
                    message = error.get("message") if isinstance(error, dict) else None
                    if message and any(marker in message for marker in PROVIDER_ERROR_MARKERS):
                        facts["provider_error"] = message[:400]
                    elif message:
                        facts["provider_error"] = message[:400]
                    else:
                        facts["provider_error"] = None
    except OSError as exc:
        facts["error"] = str(exc)
    return facts


def classify(dispatch_row: dict, facts: dict) -> str:
    """Dispatch/process classification only. It never encodes a content verdict."""
    status = (dispatch_row.get("dispatchStatus") or dispatch_row.get("status") or "").lower()
    liveness = ((dispatch_row.get("projection") or {}).get("liveness") or {}).get("verdict")
    if status in ("completed", "settled"):
        return "settled"
    if status in ("failed", "stopped"):
        return "stopped"
    if facts.get("provider_error") and facts.get("last_turn_complete"):
        return "provider_error_turn_ended"
    if facts.get("last_turn_complete"):
        return "turn_ended_without_worker_done"
    if liveness == "live":
        return "working_unproven"
    if liveness == "exited":
        return "exited"
    return "unverifiable"


def cmd_state(args: argparse.Namespace) -> int:
    code, payload, err = orca("orchestration", "worker-list", "--run", args.run, "--json")
    if payload is None:
        print(json.dumps({"error": "worker-list failed", "detail": err[:300]}))
        return code or 1
    row = None
    for worker in payload.get("result", {}).get("workers", []):
        if worker.get("dispatchId") == args.dispatch:
            row = worker
    if row is None:
        print(json.dumps({"error": "dispatch not found in run", "dispatch": args.dispatch}))
        return 1
    rollout = find_rollout(args.dispatch)
    facts = rollout_facts(rollout) if rollout else {"rollout": None}
    result = {
        "dispatch": args.dispatch,
        "dispatch_status": row.get("dispatchStatus"),
        "worker_state": row.get("workerState"),
        "terminal_state": row.get("terminalState"),
        "liveness": ((row.get("projection") or {}).get("liveness") or {}).get("verdict"),
        "activity": ((row.get("projection") or {}).get("stage") or {}).get("activity"),
        "classification": classify(row, facts),
        "content_verdict": "not_derivable_from_dispatch_state",
        "provider_error": facts.get("provider_error"),
        "effective_model": facts.get("model"),
        "effective_effort": facts.get("effort"),
        "rollout": facts.get("rollout"),
        "last_event_ts": facts.get("last_event_ts"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    code, payload, err = orca("orchestration", "run-current", "--json")
    bound = (payload or {}).get("result", {}).get("run", {}).get("id") if payload else None
    if bound == args.run:
        print(json.dumps({"bound": True, "run": args.run, "action": "none"}))
        return 0
    state = load_state(args.run)
    if state.get("binds", 0) >= MAX_INFRA_RECOVERY_ATTEMPTS:
        print(json.dumps({"bound": False, "run": args.run, "action": "blocked",
                          "reason": f"bind attempts exhausted (>{MAX_INFRA_RECOVERY_ATTEMPTS})"}))
        return 2
    code, payload, err = orca("orchestration", "run-use", "--id", args.run, "--json")
    state["binds"] = state.get("binds", 0) + 1
    save_state(args.run, state)
    ok = bool(payload and payload.get("ok"))
    print(json.dumps({"bound": ok, "run": args.run, "action": "run-use",
                      "attempt": state["binds"], "detail": None if ok else err[:200]}))
    return 0 if ok else 1


def cmd_nudge(args: argparse.Namespace) -> int:
    state = load_state(args.run)
    key = f"{args.dispatch}:{args.reason}"
    used = state["nudges"].get(key, 0)
    if used >= MAX_INFRA_RECOVERY_ATTEMPTS:
        print(json.dumps({"sent": False, "reason": "nudge budget exhausted",
                          "key": key, "used": used, "max": MAX_INFRA_RECOVERY_ATTEMPTS}))
        return 2
    with open(args.body_file, encoding="utf-8") as handle:
        body = handle.read()
    code, payload, err = orca("terminal", "send", "--terminal", args.terminal,
                              "--text", body, "--enter", "--wait-submit", "15", "--json")
    state["nudges"][key] = used + 1
    state.setdefault("nudge_log", []).append(
        {"key": key, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": bool(payload and payload.get("ok"))})
    save_state(args.run, state)
    print(json.dumps({"sent": bool(payload and payload.get("ok")), "key": key,
                      "used": state["nudges"][key], "max": MAX_INFRA_RECOVERY_ATTEMPTS,
                      "detail": None if payload else err[:200]}))
    return 0 if payload and payload.get("ok") else 1


def cmd_accept(args: argparse.Namespace) -> int:
    """Record a coordinator's manual acceptance of a complete report whose delivery failed."""
    if not os.path.exists(args.report):
        print(json.dumps({"accepted": False, "reason": "report file missing", "path": args.report}))
        return 1
    with open(args.report, "rb") as handle:
        import hashlib
        digest = hashlib.sha256(handle.read()).hexdigest()
    state = load_state(args.run)
    record = {
        "dispatch": args.dispatch,
        "report": args.report,
        "report_sha256": digest,
        "reviewed_sha": args.sha,
        "delivery": "failed",
        "worker_done": "absent (not fabricated)",
        "accepted_by": "coordinator",
        "reason": args.reason,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if any(existing["dispatch"] == args.dispatch and existing["report_sha256"] == digest
           for existing in state["acceptances"]):
        print(json.dumps({"accepted": True, "duplicate": True, **record}, ensure_ascii=False))
        return 0
    state["acceptances"].append(record)
    save_state(args.run, state)
    print(json.dumps({"accepted": True, "duplicate": False, **record}, ensure_ascii=False))
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    state = load_state(args.run)
    if args.list:
        print(json.dumps(state["ledger"], ensure_ascii=False, indent=1))
        return 0
    if args.seen:
        entry = state["ledger"].get(args.seen)
        print(json.dumps({"key": args.seen, "seen": entry is not None, "entry": entry},
                         ensure_ascii=False))
        return 0 if entry is None else 3
    if args.mark:
        if args.mark in state["ledger"]:
            print(json.dumps({"key": args.mark, "marked": False, "reason": "already processed",
                              "entry": state["ledger"][args.mark]}, ensure_ascii=False))
            return 3
        state["ledger"][args.mark] = {
            "value": json.loads(args.value) if args.value else None,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        save_state(args.run, state)
        print(json.dumps({"key": args.mark, "marked": True}, ensure_ascii=False))
        return 0
    print(json.dumps(state, ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["state", "bind", "nudge", "accept", "ledger"])
    parser.add_argument("--run", default=os.environ.get("TEAM_RUNNER_RUN"))
    parser.add_argument("--dispatch")
    parser.add_argument("--terminal")
    parser.add_argument("--reason")
    parser.add_argument("--body-file")
    parser.add_argument("--report")
    parser.add_argument("--sha")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--seen")
    parser.add_argument("--mark")
    parser.add_argument("--value")
    args = parser.parse_args()
    return {"state": cmd_state, "bind": cmd_bind, "nudge": cmd_nudge,
            "accept": cmd_accept, "ledger": cmd_ledger}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
