#!/usr/bin/env python3
"""Token usage counter for the Orca pilot.

Codex workers: ~/.codex/sessions/**/rollout-*.jsonl whose session_meta.cwd is under the pilot
worktree; the last token_count event holds cumulative totals. The session is mapped to its
Orca Dispatch/Task by the IDs injected in the worker preamble.
Coordinator: Claude Code transcript; usage is deduplicated by message id (streamed chunks repeat it).

Usage: token-usage.py [--worktree PATH] [--claude-transcript FILE] [--since ISO_DATE]
Prints a Markdown table. Reads local logs only; prints no prompts or secrets.
"""
import argparse
import glob
import json
import os
import re

DISPATCH_RE = re.compile(r"ctx_[0-9a-f]{12}")
TASK_RE = re.compile(r"task_[0-9a-f]{12}")


def load_categories(path):
    """Map dispatch id / session id prefix -> category label (setup, work, fix, infra, review-service)."""
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def categorize(row, categories):
    """Approval-review service sessions stay visible as such even when their dispatch is mapped."""
    if row.get("model", "").startswith("codex-auto-review"):
        return "approval-review-service"
    for key in (row.get("dispatch"), row.get("session")):
        if key and key in categories:
            return categories[key]
    return "uncategorized"


def codex_sessions(worktree, since, sessions_root=None, categories=None):
    rows = []
    categories = categories or {}
    root = sessions_root or os.path.expanduser("~/.codex/sessions")
    for path in glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True):
        if since and os.path.basename(path)[8:18] < since:
            continue
        with open(path, encoding="utf-8") as f:
            first = f.readline()
            try:
                meta = json.loads(first).get("payload", {})
            except json.JSONDecodeError:
                continue
            if not str(meta.get("cwd", "")).startswith(worktree):
                continue
            model = effort = None
            usage = None
            requests = 0
            last_ts = None
            dispatches, tasks = {}, set()
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = ev.get("payload") or {}
                if ev.get("type") == "turn_context":
                    model = p.get("model", model)
                    effort = p.get("effort") or p.get("reasoning_effort") or effort
                if ev.get("timestamp"):
                    last_ts = ev["timestamp"]
                if p.get("type") == "token_count" and p.get("info"):
                    # cumulative snapshot: keep the last one only, never sum snapshots
                    usage = p["info"].get("total_token_usage")
                    requests += 1
                for d in DISPATCH_RE.findall(line):
                    dispatches[d] = dispatches.get(d, 0) + 1
                tasks.update(TASK_RE.findall(line))
        started = meta.get("timestamp") or ""
        row = {
            "session": meta.get("id", "unknown")[:13],
            "session_full": meta.get("id", "unknown"),
            "started": started[:19] or "unknown",
            "model": model or "unknown",
            "effort": effort or "unknown",
            "dispatch": max(dispatches, key=dispatches.get) if dispatches else "unknown",
            "tasks": ",".join(sorted(tasks)) or "unknown",
            "usage": usage if usage else None,  # None = unknown, never zero
            "requests": requests,
            "duration_s": duration_seconds(started, last_ts),
        }
        row["category"] = categorize(row, categories)
        rows.append(row)
    return sorted(rows, key=lambda r: r["started"])


def duration_seconds(start, end):
    from datetime import datetime
    if not start or not end:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        a = datetime.strptime(start[:19], fmt)
        b = datetime.strptime(end[:19], fmt)
        return int((b - a).total_seconds())
    except ValueError:
        return None


def claude_usage(transcript, start=None, end=None):
    """Deduplicate by assistant message id; optional ISO time window [start, end)."""
    seen = {}
    with open(transcript, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "assistant":
                continue
            stamp = d.get("timestamp") or ""
            if start and stamp and stamp < start:
                continue
            if end and stamp and stamp >= end:
                continue
            msg = d.get("message") or {}
            if msg.get("id") and msg.get("usage"):
                seen[msg["id"]] = (msg.get("model"), msg["usage"])
    tot = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    model = None
    for model, u in seen.values():
        tot["input"] += u.get("input_tokens", 0)
        tot["cache_write"] += u.get("cache_creation_input_tokens", 0)
        tot["cache_read"] += u.get("cache_read_input_tokens", 0)
        tot["output"] += u.get("output_tokens", 0)
    return model, len(seen), tot


def fmt(n):
    return f"{n:,}".replace(",", " ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worktree", default="/home/talonmortem/orca/workspaces/SHM/shm-pilot-e6-01")
    ap.add_argument("--claude-transcript", action="append", default=[],
                    help="repeatable; Claude Code transcript .jsonl")
    ap.add_argument("--since", default="2026-09-19")
    ap.add_argument("--sessions-root", help="override ~/.codex/sessions (tests/fixtures)")
    ap.add_argument("--category-map", help="JSON {dispatch_or_session: category}")
    ap.add_argument("--claude-split", help="ISO timestamp: Claude usage before/after it is shown separately")
    a = ap.parse_args()

    categories = load_categories(a.category_map)
    rows = codex_sessions(a.worktree, a.since, a.sessions_root, categories)
    print("| Session | Start (UTC) | Model/effort | Dispatch | Category | Req | Sec | Input | of which cached | Uncached input | Output (reasoning) | Total |")
    print("|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
    s = {"in": 0, "cached": 0, "out": 0, "reason": 0, "total": 0}
    per_category = {}
    unknown_rows = 0
    for r in rows:
        u = r["usage"]
        if u is None:
            unknown_rows += 1
            print(f"| {r['session']} | {r['started']} | {r['model']}/{r['effort']} | {r['dispatch']} | {r['category']} | {r['requests']} | {r['duration_s'] if r['duration_s'] is not None else 'unknown'} | unknown | unknown | unknown | unknown | unknown |")
            continue
        i, c = u.get("input_tokens", 0), u.get("cached_input_tokens", 0)
        o, rs, t = u.get("output_tokens", 0), u.get("reasoning_output_tokens", 0), u.get("total_tokens", 0)
        s["in"] += i; s["cached"] += c; s["out"] += o; s["reason"] += rs; s["total"] += t
        agg = per_category.setdefault(r["category"], {"in": 0, "cached": 0, "out": 0, "total": 0, "n": 0})
        agg["in"] += i; agg["cached"] += c; agg["out"] += o; agg["total"] += t; agg["n"] += 1
        dur = r["duration_s"] if r["duration_s"] is not None else "unknown"
        print(f"| {r['session']} | {r['started']} | {r['model']}/{r['effort']} | {r['dispatch']} | {r['category']} | {r['requests']} | {dur} | {fmt(i)} | {fmt(c)} | {fmt(i - c)} | {fmt(o)} ({fmt(rs)}) | {fmt(t)} |")
    print(f"| **Codex total** | | | | | | | {fmt(s['in'])} | {fmt(s['cached'])} | {fmt(s['in'] - s['cached'])} | {fmt(s['out'])} ({fmt(s['reason'])}) | {fmt(s['total'])} |")
    if unknown_rows:
        print(f"\n*{unknown_rows} session(s) had no token_count record: reported as unknown, not zero.*")

    if per_category:
        print()
        print("| Category | Sessions | Uncached input | Cached input | Output | Total |")
        print("|---|---:|---:|---:|---:|---:|")
        for name in sorted(per_category):
            agg = per_category[name]
            print(f"| {name} | {agg['n']} | {fmt(agg['in'] - agg['cached'])} | {fmt(agg['cached'])} | {fmt(agg['out'])} | {fmt(agg['total'])} |")

    if a.claude_transcript:
        print()
        print("| Coordinator transcript | Model | Window | Messages | Input (uncached) | Cache write | Cache read | Output | Total |")
        print("|---|---|---|---:|---:|---:|---:|---:|---:|")
        for path in a.claude_transcript:
            windows = [("all", None, None)]
            if a.claude_split:
                windows = [("before split", None, a.claude_split), ("after split", a.claude_split, None)]
            for label, start, end in windows:
                model, n, t = claude_usage(path, start, end)
                allin = t["input"] + t["cache_write"] + t["cache_read"]
                print(f"| {os.path.basename(path)[:13]} | {model or 'unknown'} | {label} | {n} | {fmt(t['input'])} | {fmt(t['cache_write'])} | {fmt(t['cache_read'])} | {fmt(t['output'])} | {fmt(allin + t['output'])} |")


if __name__ == "__main__":
    main()
