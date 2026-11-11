from contextlib import contextmanager
import argparse

import datetime as dt

import json

import os

import shutil

import subprocess

import sys

import time

import uuid

from . import runner_config as config


from . import runner_operations as operations

from .runner_store import atomic_json, enqueue, lock, read_json, running

from pathlib import Path


from .tracking import Tracking
from .runner_launch import Launcher
WAIT_TYPES = "worker_done,escalation,question"
FINAL_ID = "GOAL-FINAL-REVIEW"
MAX_ACCEPTANCE_REWORKS = 2
class Runner:
    def __init__(self, context):
        self.context = context
        self.repo = context.repo
        self.state_dir = context.state_dir
        self.state_file = self.state_dir / 'state.json'
        self.log_file = self.state_dir / 'runner.log'
        self.launcher = Launcher(context)

    def orca(self, *args, **kwargs):
        if not kwargs.get('mailbox'):
            mailbox = self.load_state().get('mailbox')
            if mailbox:
                kwargs['mailbox'] = mailbox
        return self.launcher.orca(*args, **kwargs)


    def log(self, msg):
        line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a") as f:
            f.write(line + "\n")


    def notify(self, title, body=""):
        self.log(f"!! {title} {body}".rstrip())
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", f"goal-runner: {title}", body[:300]], check=False)
        print("\a", end="", flush=True)


    def effect(self, st, key, *args, mailbox=None):
        return operations.mutation(self.state_dir / 'operations.json', key, self.orca, *args, mailbox=mailbox or st.get('mailbox'))


    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout


    def load_state(self):
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {"history": []}


    def save_state(self, st):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(self.state_file, st)


    def dirty_paths(self):
        out = self.git("status", "--porcelain", "--untracked-files=all")
        return sorted(line[3:] for line in out.splitlines() if line)


    def load_config(self, path=None, overrides=None, *, allow_unavailable_fast=False):
        return config.load_config(self.context.settings_dir, path, overrides, layers=self.context.role_layers,
                                  repo=self.context.repo, config_root=self.context.config_root.parent,
                                  allow_unavailable_fast=allow_unavailable_fast)

    def attempt_profile(self):
        current = self.load_state().get('current') or {}
        if current.get('attempt_dir'):
            return self.context.check_attempt(current['attempt_dir'])['profile']
        return self.context.profile

    def coordinator_spec(self, args, feat, item):
        evidence = Path(args.goal) / feat['path'] / self.attempt_profile()['evidence_dir'] / item['id'].lower() / 'acceptance.md'
        return f"""Coordinate ONE item: {item['id']} — {item.get('task', '')}.
Goal: {args.goal}; feature: {feat['path']}. Exact checkout: {self.repo}.
Read $TEAM_RUNNER_ATTEMPT/context.json and materials/GOAL_MASTER.md first.
The snapshot includes project instructions, requirements, acceptance, checks and workflow permissions.
This item's check scope is {item.get('check_scope', 'item')}. Run checks with matching scope;
legacy checks without scope retain their prior semantics. Stage checks run ONLY on a stage gate item.
Finish independent review and blocker/high fixes BEFORE expensive verification. After FAIL diagnose,
repair and run targeted checks before a new full gate; never repeat an unchanged failing full run.
Use team-runner manifest create/verify for frozen inputs and pre/postflight JSON, never manual hashes.
Use team-runner verification run/reuse to record exact inputs, toolchain, non-secret environment and logs.
Stage gate evidence must be attached to the item as gate_evidence (path, manifest, sha256, accepted), with
accepted an absolute unused path under the current attempt, written only by runner acceptance, and
manifest roots exactly gate_roots. A missing/stale/failed gate blocks the following stage.
Project instructions take priority over shared rules. Role settings: $TEAM_RUNNER_ATTEMPT/config.json.
Read auto_continue_after_replan from that immutable config (missing means false).
If true, a failed expert repair cycle requires a saved revised plan followed by continued repair
and independent verification in this attempt, not worker_done failed merely for reaching the escalation threshold.
Stop for a concrete external blocker or a required user decision; never bypass acceptance, permissions,
or replay unknown effects. If false, stop and report the revised plan after a failed expert cycle.
Launch child roles ONLY with team-runner launch ROLE --spec FILE --title TITLE.
Read persisted child messages using team-runner messages; only the runner consumes mailboxes.
Use team-runner events list; after handling each event use events ack --dispatch YOUR_DISPATCH --ids IDS.
Reading messages and transport ACK do not acknowledge processing.
Child sessions close after accepted worker_done unless launched with --keep-session for planned reuse.
Reuse requires prior --keep-session plus explicit --function ID --reuse DISPATCH --context-percent MEASURED_PERCENT <=50,
same attempt/role/function/settings/connection. Reviewer always fresh. Never guess context usage.
Store specs/reports under $TEAM_RUNNER_ATTEMPT. Do not launch other goals or alter branches.
Update Tasks.json and evidence following snapshot acceptance. Commit only if workflow permits it.
Runner acceptance requires queue status exactly completed and evidence at {evidence}.
After worker_done, keep this session available: runner may dispatch acceptance corrections here.
Questions and worker_done use the exact injected preamble. Send succeeded only after acceptance.
Optional handoff: {args.handoff or 'none'}.
"""

    def final_spec(self, args):
        return f"""Final review of {args.goal} in {self.repo}.
Read $TEAM_RUNNER_ATTEMPT/context.json, config.json and materials/GOAL_MASTER.md.
Use completion_checker for formal tracking/report/version checks only, without rerunning the full test suite.
Use a fresh final_reviewer or final_reviewer_critical according to approved risk in materials/MODELS.md
through team-runner launch; preserve the existing review cadence and tester's frozen manifest.
Check stopping conditions, all tracking and evidence. Validate accepted stage gates and evidence applicability;
do not rerun stage checks at final goal review. Legacy checks without scope retain previous semantics.
Write goal-final-review.md in the goal's configured evidence directory.
Respect workflow permissions; no production edits. Report gaps and checks, then worker_done.
"""

    def verify_item(self, args, item_id, start_head, dirty_before):
        problems = []
        tr = Tracking(self.repo / args.goal)
        found = tr.find(item_id)
        if found is None:
            return [f"{item_id} не найден в трекинге"]
        feat, _, t = found
        if t["status"] != "completed":
            problems.append(f"Tasks.json: статус {item_id} = {t['status']}, ожидался completed")
        current = self.load_state().get('current') or {}
        frozen = read_json(Path(current['attempt_dir']) / 'item-contract.json', {}) if current.get('attempt_dir') else {}
        if frozen.get('check_scope') == 'stage' and any(t.get(k) != value for k, value in frozen.items()):
            problems.append('stage gate contract changed after dispatch')
        if t.get('check_scope') == 'stage':
            stage_checks = [c for c in self.attempt_profile()['checks'] if c.get('scope') == 'stage']
            if not frozen or stage_checks != [{'cwd': '.', 'argv': t.get('gate_command'), 'scope': 'stage'}]:
                problems.append('stage gate command must match the frozen profile stage harness')
            from .verification import gate_valid
            if not gate_valid(self.repo, t, current=True):
                problems.append('stage gate evidence missing, failed or stale')
        profile = self.attempt_profile()
        acc = Path(args.goal) / feat["path"] / profile["evidence_dir"] / t["id"].lower() / "acceptance.md"
        if not (self.repo / acc).is_file() or (profile["acceptance"]["require_commit"] and not self.git("ls-files", str(acc)).strip()):
            problems.append(f"нет закоммиченного {acc}" if profile['acceptance']['require_commit'] else f"нет файла {acc}")
        head = self.git("rev-parse", "HEAD").strip()
        if profile["acceptance"]["require_commit"] and head == start_head:
            problems.append("нет новых коммитов с начала пункта")
        new_dirty = [p for p in self.dirty_paths() if p not in dirty_before]
        if profile["acceptance"]["require_clean"] and new_dirty:
            problems.append("незакоммиченные изменения: " + ", ".join(new_dirty[:10]))
        return problems


    def verify_final(self, args, start_head):
        profile = self.attempt_profile()
        report = Path(args.goal) / profile["evidence_dir"] / "goal-final-review.md"
        def accepted_artifact(path):
            path = (self.repo / path).resolve()
            if not path.is_relative_to((self.repo / args.goal).resolve()) or not path.is_file():
                return False
            if not profile['acceptance']['require_commit']:
                return True
            relative = str(path.relative_to(self.repo))
            try:
                return (bool(self.git('ls-tree', '--name-only', 'HEAD', '--', relative).strip())
                        and not self.git('diff', 'HEAD', '--', relative).strip())
            except subprocess.CalledProcessError:
                return False

        valid = accepted_artifact(report)
        problems = [] if valid else [f"missing accepted report: {report}"]
        try:
            tr = Tracking(self.repo / args.goal)
            if not isinstance(tr.goal['queue'], list):
                raise ValueError('goal queue must be a list')
            features = list(tr.features())
            present = {path for _, path, _ in features}
            for feat in tr.goal['queue']:
                path = (tr.goal_dir / feat['path'] / 'Tasks.json').resolve()
                if path not in present:
                    problems.append(f"missing feature tracking: {path}")
                if feat['status'] != 'completed' or not tr.feature_done(feat['id']):
                    problems.append(f"feature {feat['id']}: expected completed")
            for feat, _, data in features:
                queue = data['queue']
                if not isinstance(queue, list):
                    raise ValueError(f"feature {feat['id']}: queue must be a list")
                for item in queue:
                    if not tr.item_done(item):
                        problems.append(f"{tr.key(feat, item)}: expected completed")
                    artifact = (tr.goal_dir / feat['path'] / profile['evidence_dir'] /
                                item['id'].lower() / 'acceptance.md').resolve()
                    if not artifact.is_relative_to(tr.goal_dir):
                        raise ValueError('acceptance artifact escapes goal directory')
                    if not accepted_artifact(artifact):
                        problems.append(f"missing accepted artifact: {artifact}")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            problems.append(f"unreadable or invalid tracking: {exc}")
        if profile['acceptance']['require_commit']:
            try:
                if self.git('rev-parse', 'HEAD').strip() == start_head:
                    problems.append('no new commits since final review started')
            except subprocess.CalledProcessError:
                problems.append('missing final commit')
        if profile['acceptance']['require_clean'] and self.dirty_paths():
            problems.append('uncommitted changes at final acceptance')
        return problems


    def recover_mailbox(self, st):
        """Replace only a positively stale runner mailbox; preserve the Run and deliveries."""
        old = st['mailbox']
        directory = Path(st['mailbox_dir'])
        key = 'replacement-' + old
        self.launcher.check_operations(directory)
        if any(o['status'] not in ('completed', 'rejected') for o in
               read_json(self.state_dir / 'operations.json', {}).values()):
            raise RuntimeError('mailbox recovery requires reconciliation of unknown operations')
        prior = read_json(self.launcher.journal(directory), {}).get(key, {})
        replacement = (prior.get('result') or {}).get('terminal', {})
        owners = {old}
        if replacement.get('handle'):
            owners.add(replacement['handle'])
        run = self.launcher.orca('orchestration', 'run-show', '--id', st['run_id'])['run']
        if (run.get('legacy') or run.get('id') != st['run_id']
                or run.get('coordinator_handle') not in owners):
            raise RuntimeError('Run ownership changed; refusing mailbox takeover')
        terminal = self.launcher.create_terminal(directory, key, ['cat'], 'team-runner mailbox')
        fresh = self.launcher.orca('terminal', 'show', '--terminal', terminal['handle'])['terminal']
        if (fresh.get('incarnationId') != terminal.get('incarnationId')
                or not fresh.get('incarnationId') or fresh.get('worktreeId') != self.context.orca_id
                or not fresh.get('tabId') or not fresh.get('leafId')):
            raise RuntimeError('replacement mailbox identity unconfirmed')
        self.effect(st, 'mailbox-rebind:' + old, 'orchestration', 'run-use', '--id', st['run_id'],
                    mailbox=fresh['handle'])
        current = self.launcher.orca('orchestration', 'run-show', '--id', st['run_id'])['run']
        if current.get('coordinator_handle') != fresh['handle']:
            raise RuntimeError('replacement mailbox binding unconfirmed')
        attempt = (st.get('current') or {}).get('attempt_dir')
        if attempt:
            with lock(Path(attempt) / 'agents.lock'):
                reg = self.launcher.registry(attempt)
                for agent in reg['agents']:
                    if agent.get('mailbox') == old and agent.get('run_id') == st['run_id']:
                        agent['mailbox'] = fresh['handle']
                self.launcher.save_registry(attempt, reg)
        st.setdefault('mailbox_recoveries', []).append({'previous': old, 'replacement': fresh['handle'],
                                                       'run_id': st['run_id']})
        st.update(mailbox=fresh['handle'], mailbox_incarnation=fresh['incarnationId'])
        self.save_state(st)
        self.log('восстановлен mailbox прежнего Run: ' + old + ' -> ' + fresh['handle'])
        return fresh


    @contextmanager
    def launch_permission(self):
        """Fence new resources against cancellation, without blocking cleanup."""
        from .goal_intent import read, permitted
        with lock(self.state_dir / 'goal-intent.lock'):
            intent = read(self)
            expected = getattr(self, 'authorization', None)
            if intent:
                if not expected or not permitted(self, expected['generation'], expected['epoch']):
                    raise ValueError('goal authorization changed before dispatch')
            yield

    def ensure_run(self, st):
        mailbox = st.get('mailbox')
        if mailbox:
            try:
                terminal = self.launcher.orca('terminal', 'show', '--terminal', mailbox)['terminal']
            except RuntimeError as exc:
                code = (getattr(exc, 'receipt', {}).get('error') or {}).get('code')
                if code != 'terminal_handle_stale' or not st.get('run_id'):
                    raise
                terminal = self.recover_mailbox(st)
            if terminal.get('incarnationId') != st.get('mailbox_incarnation') or terminal.get('worktreeId') != self.context.orca_id:
                raise ValueError('runner mailbox identity changed')
        else:
            st.setdefault('session_id', str(uuid.uuid4()))
            st['mailbox_dir'] = str(self.state_dir / 'mailboxes' / st['session_id'])
            self.save_state(st)
            with self.launch_permission():
                terminal = self.launcher.create_terminal(Path(st['mailbox_dir']), 'mailbox', ['cat'], 'team-runner mailbox')
            st.update(mailbox=terminal['handle'], mailbox_incarnation=terminal['incarnationId'])
            self.save_state(st)
        current = self.orca("orchestration", "run-current").get("run")
        if st.get("run_id"):
            if not current or current.get("id") != st["run_id"]:
                self.effect(st, "run-use:" + st["run_id"] + ":" + os.environ.get("ORCA_TERMINAL_HANDLE", "local"), "orchestration", "run-use", "--id", st["run_id"])
                self.log(f"привязан к Run {st['run_id']}")
            return st["run_id"]
        with self.launch_permission():
            run = self.effect(st, "run-create:" + st["session_id"], "orchestration", "run-create", "--objective", "goal-runner: " + st["goal"])["run"]
        st["run_id"] = run["id"]
        self.save_state(st)
        self.log(f"создан Run {run['id']}")
        return run["id"]


    def wait_gate(self, st, gate_id, task_id):
        self.notify("ждёт решения", f"{st['current']['item']}: team-runner resolve start|skip|stop")
        while True:
            self.process_controls(st)
            if st.get("stop_requested"):
                return "stopped_attempt"
            if st.get("paused"):
                return "paused_gate"
            gates = self.orca("orchestration", "gate-list", "--task", task_id, "--run", st["run_id"]).get("gates", [])
            g = next((x for x in gates if x.get("id") == gate_id), None)
            if g and g.get("status") not in (None, "pending", "open"):
                return (g.get("resolution") or "").strip().lower()
            time.sleep(1)


    def start_coordinator(self, args, st, item_id, spec, title):
        cur = st.get("current")
        if not cur or cur.get("item") != item_id:
            if st.get('recovery_issues') or st.get('last_error'):
                previous = cur or (st.get('history') or [{}])[-1]
                st.setdefault('recovery_history', []).append({
                    'attempt_dir': previous.get('attempt_dir'), 'issues': st.get('recovery_issues', []),
                    'last_error': st.get('last_error'), 'at': dt.datetime.now().isoformat(timespec='seconds')})
                st.pop('recovery_issues', None)
                st.pop('last_error', None)
            cur = {"item": item_id, "start_head": self.git("rev-parse", "HEAD").strip(), "attempt": 1,
                   "dirty_before": self.dirty_paths(), "started_at": dt.datetime.now().isoformat(timespec="seconds")}
            st["current"] = cur
        if not cur.get("dispatch_id") and not cur.get("attempt_dir"):
            attempt = self.state_dir / "attempts" / str(uuid.uuid4())
            from .catalog import Catalog
            self.context = Catalog(self.context.config_root, self.context.state_root).context(self.context.project_id, self.context.workspace_id)
            self.launcher.context = self.context
            snapshot = cur.pop("retry_config", None) or self.load_config(args.config,
                        {k: getattr(args, k, None) for k in config.FIELDS | {'fast'}})
            previous = cur.pop('retry_context_from', None)
            if previous:
                shutil.copytree(Path(previous) / 'materials', attempt / 'materials')
                atomic_json(attempt / 'context.json', read_json(Path(previous) / 'context.json'))
            else:
                self.context.snapshot(attempt)
            if getattr(args, 'goal', None) and (self.repo / args.goal / 'Tasks.json').is_file():
                found = Tracking(self.repo / args.goal).find(item_id)
                if found and found[2].get('check_scope') == 'stage':
                    atomic_json(attempt / 'item-contract.json', {k: found[2].get(k) for k in
                                ('check_scope', 'gate_roots', 'gate_command', 'depends_on')})
            atomic_json(attempt / "config.json", snapshot)
            cur["attempt_dir"] = str(attempt)
            cur["requested"] = snapshot["roles"]["coordinator"]
            cur["snapshot_id"] = attempt.name
            cur["observed"] = None
            self.save_state(st)
        if cur.get("attempt_dir"):
            spec += "\nRead $TEAM_RUNNER_ATTEMPT/config.json before work. It is the authoritative role snapshot, including on retries."
        if not cur.get("task_id"):
            if cur.get("attempt", 1) > 1:
                spec += (f"\nЭто ПОПЫТКА {cur['attempt']}: прошлый координатор ({cur.get('previous_dispatch')}) прервался. "
                         "Обязательно выполни шаг 2 (продолжение прошлой попытки).\n")
                title += f" (attempt {cur['attempt']})"
            with self.launch_permission():
                task = self.effect(st, "task:" + cur["attempt_dir"], "orchestration", "task-create", "--run", st["run_id"], "--spec", spec, "--task-title", title)["task"]
            cur["task_id"] = task["id"]
            self.save_state(st)
            self.log(f"{item_id}: task {task['id']} (попытка {cur.get('attempt', 1)})")
        if args.mode == "confirm" and st.get("history") and not cur.get("gate_resolved"):
            if not cur.get("gate_id"):
                gate = self.effect(st, "gate:" + cur["task_id"], "orchestration", "gate-create", "--task", cur["task_id"],
                            "--question", f"Запустить координатора для {item_id}?",
                            "--options", json.dumps(["start", "skip", "stop"]))
                cur["gate_id"] = (gate.get("gate") or gate)["id"]
                self.save_state(st)
            choice = self.wait_gate(st, cur["gate_id"], cur["task_id"])
            self.log(f"{item_id}: решение gate = {choice}")
            if choice != "start":
                return choice
            cur["gate_resolved"] = True
            self.save_state(st)
        if not cur.get("dispatch_id"):
            self.process_controls(st)
            if st.get("stop_requested"):
                return "stopped_attempt"
            with self.launch_permission():
                record = self.launcher.launch(cur["attempt_dir"], "coordinator", cur["task_id"], st["run_id"], title,
                                         self.state_dir, mailbox=st.get("mailbox"))
            cur["dispatch_id"] = record["dispatch_id"]
            cur["terminal"] = record.get("terminal")
            self.save_state(st)
            self.notify("координатор запущен", f"{item_id}: dispatch {cur['dispatch_id']}")
        return "start"


    def wait_coordinator(self, st):
        cur = st["current"]
        for m in cur.get("messages", []):
            payload = m.get("payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            if m.get("type") == "worker_done" and payload.get("dispatchId") == cur["dispatch_id"]:
                return {"outcome": payload.get("outcome", "unknown"), "body": m.get("body", "")}
        last_liveness_check = 0.0
        while True:
            self.process_controls(st)
            if st.get("stop_requested") or st.get('control_uncertain'):
                return None
            try:
                self.poll_children(st)
                self.poll_outputs(st)
                from .coordinator_events import tick
                tick(self, st)
                res = self.orca("orchestration", "check", "--run", st["run_id"], "--wait", "--types", WAIT_TYPES,
                           "--timeout-ms", "1000", timeout=10)
            except subprocess.TimeoutExpired:
                # Неподтверждённая выдача повторится; мутации и ack здесь не повторяем.
                self.log("Orca check timeout; retrying mailbox read")
                time.sleep(2)
                continue
            except RuntimeError as exc:
                code = (getattr(exc, 'receipt', {}).get('error') or {}).get('code')
                if code not in ('terminal_handle_stale', 'stable_pane_required'):
                    raise
                previous = st.get('mailbox')
                self.ensure_run(st)
                if st.get('mailbox') == previous:
                    raise  # A child mailbox/foreign failure must not become a retry loop.
                continue
            self.persist_messages(st, res, st["run_id"])

            msgs = res.get("messages") or []
            if not msgs:
                if time.monotonic() - last_liveness_check >= 15:
                    last_liveness_check = time.monotonic()
                    lv = self.launcher.liveness(cur["dispatch_id"], st.get("mailbox"))
                    self.log(f"{cur['item']}: ожидание, координатор {lv}")
                    if lv == "exited":
                        self.notify("координатор завершился без worker_done", f"{cur['item']}: team-runner retry")
                        return None
                continue
            done = None
            for m in msgs:
                body = (m.get("body") or "").strip()
                if m["type"] == "question":
                    cur.setdefault("questions", []).append(m["id"])
                    self.save_state(st)
                    self.notify(f"вопрос от координатора {cur['item']}", body[:200])
                    print(f"\n=== ВОПРОС {m['id']} ===\n{body}\nОтвет: team-runner reply {m['id']} \"...\"\n", flush=True)
                elif m["type"] == "escalation":
                    self.notify(f"эскалация {cur['item']}", body[:200])
                    print(f"\n=== ЭСКАЛАЦИЯ ===\n{body}\n", flush=True)
                elif m["type"] == "worker_done":
                    payload = m.get("payload") or {}
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    if payload.get("dispatchId") != cur["dispatch_id"]:
                        self.log(f"игнорирую worker_done чужого dispatch {payload.get('dispatchId')}")
                        continue
                    done = {"outcome": payload.get("outcome", "unknown"), "body": body}
            if res.get("deliveryId"):
                self.ack_delivery(st, res["deliveryId"], st["run_id"])
            if done:
                return done


    def finish_item(self, args, st, done, verify):
        cur = st["current"]
        final_fingerprint = None
        final_source = None
        if cur['item'] == FINAL_ID:
            from .goal_completion import tracking_fingerprint, source_proof
            try:
                final_fingerprint = tracking_fingerprint(self, args.goal)
                final_source = source_proof(self, args.goal)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        problems = verify() if done["outcome"] == "succeeded" else [f"outcome={done['outcome']}"]
        self.poll_children(st)
        child_problems, resource_problems = self.child_acceptance_problems(st)
        problems.extend(child_problems)
        acceptance_problems = list(problems)
        problems.extend(resource_problems)
        if not problems and cur.get("attempt_dir") and not self.settle_children(cur["attempt_dir"]):
            reg = self.launcher.registry(cur['attempt_dir'])
            details = [f"{a.get('role')} Dispatch {a.get('dispatch_id')}: {a.get('cleanup_error') or a.get('error')}"
                       for a in reg['agents'] if a.get('cleanup_error') or a.get('error')]
            details.extend(f"mailbox {r.get('run_id')}: {r['error']}" for r in reg['runs'] if r.get('error'))
            problems.extend(details or ["child resources not settled or release unconfirmed"])
        if (not problems or done["outcome"] == "failed") and cur.get("attempt_dir"):
            with lock(Path(cur["attempt_dir"]) / "agents.lock"):
                reg = self.launcher.registry(cur["attempt_dir"])
                coordinator = next((a for a in reg['agents'] if a.get('dispatch_id') == cur['dispatch_id']), None)
                if coordinator:
                    try:
                        self.launcher.release_agent(coordinator, cur["attempt_dir"],
                                                    {"dispatchId": cur["dispatch_id"], **done})
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                        problems.append(str(exc))
                    self.launcher.save_registry(cur["attempt_dir"], reg)
        if not problems and getattr(args, 'goal', None) and (self.repo / args.goal / 'Tasks.json').is_file():
            try:
                found = Tracking(self.repo / args.goal).find(cur['item'])
                item = found[2] if found else {}
                contract = read_json(Path(cur['attempt_dir']) / 'item-contract.json', {}) if cur.get('attempt_dir') else {}
                if contract.get('check_scope') == 'stage' and any(item.get(k) != value for k, value in contract.items()):
                    raise ValueError('stage gate contract changed before acceptance')
                if item.get('check_scope') == 'stage':
                    from .verification import accept_gate
                    receipt = Path(item['gate_evidence']['accepted'])
                    attempt = Path(cur['attempt_dir']).resolve()
                    if not receipt.is_absolute() or not receipt.resolve().is_relative_to(attempt):
                        raise ValueError('stage acceptance receipt must stay in current attempt')
                    accept_gate(self.repo, item)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                problems.append('stage acceptance: ' + str(exc))
        entry = {**cur,
                 "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
                 "outcome": done["outcome"], "summary": done["body"], "problems": problems,
                 "head": self.git("rev-parse", "HEAD").strip()}
        if cur['item'] == FINAL_ID and not problems:
            from .goal_completion import tracking_fingerprint, source_proof
            from .goal_intent import read as read_intent
            intent = read_intent(self)
            if intent:
                try:
                    entry['tracking_fingerprint'] = tracking_fingerprint(self, args.goal)
                    if entry['tracking_fingerprint'] != final_fingerprint:
                        raise ValueError('tracking changed during final acceptance')
                    entry['source_proof'] = source_proof(self, args.goal)
                    if entry['source_proof'] != final_source:
                        raise ValueError('source or acceptance artifacts changed during final acceptance')
                    entry['completion_generation'] = intent['generation']
                except (OSError, ValueError, KeyError, TypeError):
                    problems.append('goal tracking, source or acceptance artifacts changed before final acceptance was saved')
        st["history"].append(entry)
        st["current"] = cur if problems else None
        if problems:
            cur["failed"] = True
            cur["problems"] = list(problems)
            if (acceptance_problems and not resource_problems and done['outcome'] == 'succeeded' and cur.get('attempt_dir')
                    and cur.get('acceptance_reworks', 0) < MAX_ACCEPTANCE_REWORKS):
                cur['acceptance_rework'] = {
                    'number': cur.get('acceptance_reworks', 0) + 1,
                    'dispatch_id': cur['dispatch_id'], 'problems': list(acceptance_problems),
                }
                cur.pop('failed', None)
        self.save_state(st)
        print(f"\n=== {entry['item']}: {done['outcome']} ===\n{done['body']}\n", flush=True)
        if problems:
            self.notify(f"{entry['item']} НЕ принят runner'ом", "; ".join(problems))
            return False
        try:
            if not cur.get("attempt_dir"):
                self.effect(st, "legacy-release:" + entry["dispatch_id"], "orchestration", "worker-release", "--dispatch", entry["dispatch_id"])
        except RuntimeError as e:
            self.log(f"release: {e}")
        self.notify(f"{entry['item']} принят", entry["head"][:7])
        return True


    def rework_acceptance(self, st):
        cur = st['current']
        repair = cur['acceptance_rework']
        self.process_controls(st)
        if st.get('stop_requested') or st.get('control_uncertain'):
            return False
        try:
            self.launcher.check_operations(cur['attempt_dir'])
            reg = self.launcher.registry(cur['attempt_dir'])
            if reg.get('fenced') or any(a.get('status') in ('launching', 'unknown') for a in reg['agents']):
                raise RuntimeError('attempt fenced or launch unconfirmed; reconcile first')
            spec = (f"Correct runner acceptance for {cur['item']} in this SAME attempt. "
                    "Preserve completed work; do not rerun implementation without cause. "
                    "Read $TEAM_RUNNER_ATTEMPT/context.json and config.json. "
                    "Use the NEW injected Dispatch preamble for exactly one worker_done. "
                    "Do not reuse the previous dispatch ID. Resolve these checks before succeeded:\n- "
                    + '\n- '.join(repair['problems']))
            key = 'acceptance-rework:' + repair['dispatch_id']
            with self.launch_permission():
                task = self.effect(st, key, 'orchestration', 'task-create', '--run', st['run_id'],
                                   '--spec', spec, '--task-title', f"{cur['item']} acceptance correction {repair['number']}")['task']
                record = self.launcher.rework_coordinator(cur['attempt_dir'], repair['dispatch_id'],
                            task['id'], st['run_id'], self.state_dir)
            cur.update(dispatch_id=record['dispatch_id'], task_id=task['id'],
                       terminal=record.get('terminal'), acceptance_reworks=repair['number'])
            cur.pop('acceptance_rework')
            cur.pop('acceptance_rework_error', None)
            cur.pop('failed', None)
            self.save_state(st)
            self.notify('замечания приёмки возвращены координатору', f"{cur['item']}: {repair['number']}/{MAX_ACCEPTANCE_REWORKS}")
            return True
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            cur['failed'] = True
            cur['acceptance_rework_error'] = str(exc)
            self.save_state(st)
            self.notify('возврат приёмки заблокирован; сессия сохранена', str(exc))
            return False


    def cmd_run(self, args):
        from .goal_intent import read as read_intent
        intent = read_intent(self)
        if intent:
            if not getattr(self, 'authorization', None):
                self.authorization = {key: intent[key] for key in ('generation', 'epoch')}
            with self.launch_permission():
                if args.goal != intent['goal']:
                    raise ValueError('runner goal differs from authorization')
        st = self.load_state()
        if st.get("goal") and st["goal"] != args.goal and (st.get("current") or st.get("run_id")):
            raise ValueError("state belongs to another goal; finish the previous goal first")
        if st.get("control_uncertain"):
            raise ValueError("control command outcome unknown; reconcile command before run")
        if st.get("stop_requested") or (st.get("current") or {}).get("failed"):
            raise ValueError("attempt stopped/failed; retry explicitly before run")
        if not (st.get('current') or {}).get('attempt_dir'):
            self.load_config(args.config, {k: getattr(args, k, None) for k in config.FIELDS | {'fast'}})
        goal_path = (self.repo / args.goal).resolve()
        if not goal_path.is_relative_to((self.repo / self.attempt_profile()["tracking_root"]).resolve()) or not (goal_path / "Tasks.json").is_file():
            raise ValueError("goal must reference existing tracking under profile.tracking_root")
        st["goal"] = args.goal
        if args.config:
            args.config = str(Path(args.config).expanduser().resolve())
        st["options"] = {k: v for k, v in vars(args).items() if k not in ("func", "cmd") and not (k == "fast" and v is None)}
        st["paused"] = False
        st["runner_status"] = "running"
        st.pop("last_error", None)
        self.save_state(st)
        run_id = self.ensure_run(st)
        names = Tracking(self.repo / args.goal)
        only = {names.canonical(i) for i in args.items.split(",") if i} if args.items else None
        self.log(f"runner: goal={args.goal} run={run_id} mode={args.mode} items={args.items or 'auto'} model={args.model}")
        skipped = {names.canonical(i, strict=False) for i in st.get("skipped", [])}
        while True:
            self.process_controls(st)
            if st.get("control_uncertain") or st.get("stop_requested") or (st.get("paused") and not st.get("current")):
                return 0
            tr = Tracking(self.repo / args.goal)
            cur = st.get("current")
            if cur and cur.get('acceptance_rework'):
                if not self.rework_acceptance(st):
                    return 1
            if cur:
                item_id = cur["item"]
            else:
                nxt = tr.next_item(only, skipped)
                if nxt is None:
                    last = st['history'][-1] if st['history'] else {}
                    final_accepted = last.get('item') == FINAL_ID and not last.get('problems')
                    if final_accepted and intent:
                        from .goal_completion import tracking_fingerprint, source_proof
                        try:
                            final_accepted = (last.get('outcome') == 'succeeded'
                                              and last.get('completion_generation') == intent['generation']
                                              and last.get('tracking_fingerprint') == tracking_fingerprint(self, args.goal)
                                              and last.get('source_proof') == source_proof(self, args.goal))
                        except (OSError, ValueError, KeyError, TypeError):
                            final_accepted = False
                    if args.final and not final_accepted:
                        item_id = FINAL_ID
                    else:
                        from .goal_completion import record
                        record(self, st)
                        blocked = tr.blocked_report()
                        self.notify("очередь пуста", "ждут решения пользователя: " + ", ".join(blocked) if blocked else "")
                        return 0
                else:
                    item_id = tr.key(nxt[0], nxt[2])
            if item_id == FINAL_ID:
                spec, title = self.final_spec(args), "goal final review"
                verify = lambda: self.verify_final(args, st["current"]["start_head"])
            else:
                feat, _, item = tr.find(item_id)
                spec, title = self.coordinator_spec(args, feat, item), f"{item_id} coordinator"
                verify = lambda i=item_id: self.verify_item(args, i, st["current"]["start_head"], st["current"]["dirty_before"])
            choice = self.start_coordinator(args, st, item_id, spec, title)
            if choice in ("stopped_attempt", "paused_gate"):
                return 0
            if choice == "skip":
                skipped.add(item_id)
                st["skipped"] = sorted(skipped)
                st["current"] = None
                self.save_state(st)
                continue
            if choice == "stop":
                self.log("остановлено решением gate")
                st["current"] = None
                self.save_state(st)
                return 0
            done = self.wait_coordinator(st)
            if done is None or not self.finish_item(args, st, done, verify):
                if done is not None and st['current'].get('acceptance_rework'):
                    continue
                self.log("runner остановлен: нужна проверка пользователя (state.json, runner.log)")
                return 1
            if only is not None:
                only.discard(names.canonical(item_id, strict=False))
                if not only and not args.final:
                    from .goal_completion import record
                    record(self, st)
                    self.notify("заданные пункты выполнены")
                    return 0


    def cmd_control(self, args):
        data = {}
        if args.cmd == "reply":
            data = {"message_id": args.message_id, "text": Path(args.file).read_text() if args.file else args.text}
            if getattr(args, 'run_id', None) is not None:
                data['run_id'] = args.run_id
        elif args.cmd == "resolve":
            data = {"choice": args.choice}
        elif args.cmd == 'continue-operation':
            data = {'operation_id': args.operation_id}
        elif args.cmd == "retry":
            data = {"force": args.force, "refresh": args.refresh,
                    "allow_extra_attempt": getattr(args, "allow_extra_attempt", False)}
        from .application import submit_command
        rec = submit_command(self, args.cmd, data, getattr(args, 'request_id', None) or str(uuid.uuid4()))
        print(json.dumps(rec, ensure_ascii=False))
        return 1 if rec["status"] in ("error", "unknown") else 0


    def submit_control(self, action, data=None, request_id=None):
        if request_id is not None:
            try:
                uuid.UUID(request_id)
            except (ValueError, TypeError, AttributeError):
                raise ValueError('request_id must be a UUID') from None
        data = dict(data or {})
        if action in ('pause', 'stop', 'retry', 'reply', 'resolve', 'reconcile', 'continue-operation'):
            cur = self.load_state().get('current') or {}
            data['_target'] = cur.get('attempt_dir') or cur.get('dispatch_id') or cur.get('item')
            if request_id:
                old = read_json(self.state_dir / 'commands' / (str(request_id) + '.json')) if len(str(request_id)) == 36 else None
                if old and 'schema_version' in old:
                    raise ValueError('request_id already used for a versioned command')
                if old and old['action'] == action and {k:v for k,v in old['data'].items() if k != '_target'} == {k:v for k,v in data.items() if k != '_target'}:
                    return old
                if old:
                    raise ValueError('request_id already used for another command')
        if action in ('pause', 'stop', 'retry'):
            from .recovery import set_intent
            set_intent(self, {'pause': 'paused', 'stop': 'stopped', 'retry': 'paused'}[action])
        rec = enqueue(self.state_dir, action, data, request_id)
        if not running(self.state_dir):
            try:
                with lock(self.state_dir / "runner.lock", blocking=False):
                    st = self.load_state()
                    st["runner_status"] = "idle"
                    self.process_controls(st)
            except RuntimeError:
                pass  # A runner acquired ownership; it will consume the queue.
        return read_json(self.state_dir / "commands" / (rec["id"] + ".json"))


    def cmd_launch(self, args):
        attempt = os.environ.get("TEAM_RUNNER_ATTEMPT")
        if not attempt:
            raise ValueError("launch requires TEAM_RUNNER_ATTEMPT inherited from coordinator")
        self.context.check_attempt(attempt)
        print(json.dumps(self.launcher.dispatch_role(attempt, args.role, Path(args.spec).read_text(),
                                               args.title, self.state_dir, function=args.function, reuse=args.reuse,
                                               context_percent=args.context_percent, keep_session=args.keep_session,
                                               variant=args.variant, reason=args.reason), ensure_ascii=False))


    def cmd_messages(self, args):
        attempt = os.environ.get("TEAM_RUNNER_ATTEMPT")
        st = self.load_state()
        cur = st.get("current") or {}
        if not attempt or attempt != cur.get("attempt_dir"):
            raise ValueError("no current attempt for this coordinator")
        print(json.dumps(cur.get("messages", []), ensure_ascii=False))


    def cmd_status(self, args):
        from .application import legacy_status_view
        print(json.dumps(legacy_status_view(self), ensure_ascii=False, indent=1))


    def persist_messages(self, st, receipt, run_id):
        cur = st.get('current')
        if not cur:
            return
        messages = cur.setdefault('messages', [])
        known = {(m['run_id'], m['id']) for m in messages}
        for message in receipt.get('messages') or []:
            if message.get('id') and (run_id, message['id']) not in known:
                messages.append({**message, 'run_id': run_id})
                known.add((run_id, message['id']))
        operations.checkpoint('message:before_save')
        self.save_state(st)  # Always persist before ack; replay deduplicates by message ID.
        operations.checkpoint('message:after_save')
        from .coordinator_events import capture
        capture(self, st)


    def ack_delivery(self, st, delivery_id, run_id, mailbox=None):
        # check --ack also returns the next batch. Persist it, leave it unacknowledged for next poll.
        receipt = self.effect(st, 'ack:' + run_id + ':' + delivery_id, 'orchestration', 'check', '--ack', delivery_id, '--run', run_id, mailbox=mailbox)
        self.persist_messages(st, receipt, run_id)


    def poll_outputs(self, st):
        cur = st.get('current') or {}
        if not cur.get('attempt_dir') or time.time() - cur.get('output_polled_at', 0) < 15:
            return
        cur['output_polled_at'] = time.time()
        for agent in self.launcher.registry(cur['attempt_dir'])['agents']:
            if not agent.get('dispatch_id'):
                continue
            try:
                result = self.orca('orchestration', 'worker-read', '--dispatch', agent['dispatch_id'],
                              '--limit', '30', mailbox=agent.get('mailbox'))
                cur.setdefault('outputs', {})[agent['dispatch_id']] = json.dumps(result, ensure_ascii=False)[-8000:]
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                cur.setdefault('outputs', {})[agent['dispatch_id']] = str(exc)
        with lock(Path(cur['attempt_dir']) / 'agents.lock'):
            reg = self.launcher.registry(cur['attempt_dir'])
            for agent in reg['agents']:
                if not agent.get('dispatch_id') or agent.get('status') in ('released', 'stopped'):
                    continue
                try:
                    data = self.launcher.observe_agent(agent)
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    continue
                if (not reg.get('fenced') and not reg.get('stop_requested')
                        and not st.get('stop_requested') and not st.get('paused')):
                    try:
                        self.launcher.restore_terminal_panel(cur['attempt_dir'], agent, data)
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                        if agent.get('panel_error') != str(exc):
                            self.log(f"{agent['dispatch_id']}: terminal panel: {exc}")
                        agent['panel_error'] = str(exc)
            self.launcher.save_registry(cur['attempt_dir'], reg)
        self.save_state(st)


    def poll_children(self, st):
        cur = st.get('current') or {}
        if not cur.get('attempt_dir'):
            return
        for run in self.launcher.registry(cur['attempt_dir'])['runs']:
            if run.get('status') != 'ready':
                continue
            receipt = self.orca('orchestration', 'check', '--run', run['run_id'],
                           '--terminal', run['mailbox'], mailbox=run['mailbox'])
            self.persist_messages(st, receipt, run['run_id'])
            self.release_completed_children(cur['attempt_dir'])
            if receipt.get('deliveryId'):
                self.ack_delivery(st, receipt['deliveryId'], run['run_id'], run['mailbox'])


    def accepted_child_reports(self, attempt):
        cur = self.load_state().get('current') or {}
        if cur.get('attempt_dir') != str(attempt):
            return {}
        reports = {}
        for message in cur.get('messages', []):
            if message.get('type') == 'worker_done':
                payload = message.get('payload') or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        continue
                if isinstance(payload, dict):
                    reports[payload.get('dispatchId')] = payload
        return reports

    def release_completed_children(self, attempt):
        reports = self.accepted_child_reports(attempt)
        with lock(Path(attempt) / 'agents.lock'):
            reg = self.launcher.registry(attempt)
            for agent in reg['agents']:
                if agent['role'] == 'coordinator' or agent.get('status') != 'active' or agent.get('keep_session'):
                    continue
                data = self.launcher.observe_agent(agent)
                status = (data.get('dispatch') or {}).get('status')
                report = reports.get(agent['dispatch_id'])
                if not report or (status != 'completed' and not (status == 'failed' and report.get('outcome') == 'failed')):
                    continue
                # Завершение проверено в Orca; журнал сообщений уже сохранён до закрытия.
                try:
                    self.launcher.release_agent(agent, attempt, report)
                    agent.pop('cleanup_error', None)
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    agent['cleanup_error'] = str(exc)
                    self.launcher.save_registry(attempt, reg)
                    raise
                self.launcher.save_registry(attempt, reg)


    def close_completed_questions(self, st, registry):
        cur = st.get('current') or {}
        persisted = self.load_state().get('current') or {}
        if not cur.get('attempt_dir') or persisted.get('attempt_dir') != cur['attempt_dir']:
            return
        def dispatch_id(message):
            payload = message.get('payload') or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    return None
            return payload.get('dispatchId') if isinstance(payload, dict) else None
        owned = {(a.get('run_id'), a.get('dispatch_id')) for a in registry['agents']
                 if a.get('run_id') and a.get('dispatch_id')}
        completed = {(m.get('run_id'), dispatch_id(m)) for m in persisted.get('messages', [])
                     if m.get('type') == 'worker_done'} & owned
        for message in cur.get('messages', []):
            if (message.get('type') == 'question' and not message.get('answered')
                    and (message.get('run_id'), dispatch_id(message)) in completed):
                message.update(closed=True, reply_error='question closed: accepted worker_done')


    def cleanup_accepted_reports(self, st, permitted=None):
        """Settle accepted reports without evaluating or scheduling acceptance work."""
        cur = st.get('current') or {}
        attempt = cur.get('attempt_dir')
        if not attempt:
            return
        reports = self.accepted_child_reports(attempt)
        with lock(Path(attempt) / 'agents.lock'):
            reg = self.launcher.registry(attempt)
            for agent in reg['agents']:
                report = reports.get(agent.get('dispatch_id'))
                if not report or agent.get('keep_session') or agent.get('status') in ('released', 'stopped', 'superseded'):
                    continue
                # Successful coordinators explicitly retain their session for acceptance corrections.
                if agent.get('role') == 'coordinator' and report.get('outcome') == 'succeeded':
                    continue
                if permitted is not None and not permitted():
                    raise RuntimeError('cleanup cancelled by user intent')
                try:
                    self.launcher.release_agent(agent, attempt, report)
                    agent.pop('error', None)
                    agent.pop('cleanup_error', None)
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    agent['cleanup_error'] = str(exc)
                    self.launcher.save_registry(attempt, reg)
                    raise
                self.launcher.save_registry(attempt, reg)


    def child_acceptance_problems(self, st):
        """Keep incomplete work separate from unconfirmed resource effects."""
        cur = st.get('current') or {}
        attempt = cur.get('attempt_dir')
        if not attempt:
            return [], []
        reports = self.accepted_child_reports(attempt)
        resource_problems = []
        with lock(Path(attempt) / 'agents.lock'):
            reg = self.launcher.registry(attempt)
            for agent in reg['agents']:
                if (not agent.get('dispatch_id') or agent.get('role') == 'coordinator'
                        or agent.get('dispatch_id') in reports):
                    continue
                if not agent.get('terminal_closed') and agent.get('status') not in ('released', 'superseded'):
                    try:
                        data = self.launcher.observe_agent(agent)
                        if (data.get('dispatch') or {}).get('terminationReason') == 'operator_close':
                            if not self.launcher.confirm_operator_closed(agent, data, attempt):
                                raise RuntimeError('operator_close identity/process exit unconfirmed')
                            agent.update(status='stopped')
                            agent.pop('error', None)
                            agent.pop('cleanup_error', None)
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                        agent['cleanup_error'] = str(exc)
                        resource_problems.append(f"{agent.get('role')} Dispatch {agent.get('dispatch_id')}: {exc}")
                if agent.get('work_incomplete'):
                    requirements = cur.setdefault('required_child_reports', [])
                    if not any(r['dispatch_id'] == agent['dispatch_id'] for r in requirements):
                        requirements.append({'role': agent['role'], 'function': agent.get('function'),
                            'dispatch_id': agent['dispatch_id'], 'reason': agent['work_incomplete'],
                            'prior_dispatches': [a.get('dispatch_id') for a in reg['agents']]})
            self.launcher.save_registry(attempt, reg)
        problems = []
        for requirement in cur.get('required_child_reports', []):
            fresh = next((a for a in reg['agents']
                if a.get('role') == requirement['role'] and a.get('function') == requirement['function']
                and a.get('dispatch_id') not in requirement['prior_dispatches']
                and reports.get(a.get('dispatch_id'), {}).get('outcome') == 'succeeded'), None)
            if not fresh:
                problems.append(f"{requirement['role']} function {requirement['function']!r} Dispatch "
                    f"{requirement['dispatch_id']}: {requirement['reason']}; new successful worker_done required")
        if cur.get('required_child_reports'):
            self.save_state(st)
        return problems, resource_problems


    def settle_children(self, attempt):
        reports = self.accepted_child_reports(attempt)
        with lock(Path(attempt) / 'agents.lock'):
            reg = self.launcher.registry(attempt)
            # Closing the launch fence prevents a late child from escaping final acceptance.
            reg['fenced'] = True
            self.launcher.save_registry(attempt, reg)
            for agent in reg['agents']:
                if agent['role'] == 'coordinator' or agent.get('status') in ('released', 'superseded'):
                    continue
                if agent.get('status') == 'stopped' and agent.get('terminal_closed'):
                    continue
                if agent.get('dispatch_id') not in reports:
                    agent['cleanup_error'] = 'worker_done missing; child cleanup unconfirmed'
                    self.launcher.save_registry(attempt, reg)
                    return False
                try:
                    self.launcher.release_agent(agent, attempt, reports[agent['dispatch_id']])
                    agent.pop('cleanup_error', None)
                    self.launcher.save_registry(attempt, reg)
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    agent['cleanup_error'] = str(exc)
                    self.launcher.save_registry(attempt, reg)
                    return False
            for run in reg['runs']:
                if run.get('status') == 'closed':
                    continue
                if run.get('status') != 'ready':
                    run['error'] = 'mailbox creation unconfirmed'
                    self.launcher.save_registry(attempt, reg)
                    return False
                try:
                    self.launcher.close_mailbox(attempt, run)
                    run['status'] = 'closed'
                    run.pop('error', None)
                    self.launcher.save_registry(attempt, reg)
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    run['error'] = str(exc)
                    self.launcher.save_registry(attempt, reg)
                    return False
        return True


    def apply_control(self, st, action, data, command_id=None):
        cur = st.get('current') or {}
        if cur.get('attempt_dir'):
            self.context.check_attempt(cur['attempt_dir'])
        if '_target' in data and data['_target'] != (cur.get('attempt_dir') or cur.get('dispatch_id') or cur.get('item')):
            raise ValueError('command belongs to a previous attempt')
        if action == 'technical-reconcile':
            from .recovery import settings, target
            expected = data.get('_target')
            permitted = lambda: (settings(self)['enabled'] and settings(self)['intent'] == 'running'
                                 and target(self.load_state()) == expected)
            if not permitted():
                raise ValueError('technical recovery cancelled by user intent or attempt change')
            self.recover(st, technical_only=True)
            self.cleanup_accepted_reports(st, permitted=permitted)
            cur = st.get('current') or {}
            decision = (cur.get('failed') or cur.get('problems') or 'acceptance_rework' in cur
                        or (cur.get('gate_id') and not cur.get('gate_resolved'))
                        or any(m.get('type') in ('question', 'escalation') and not m.get('closed')
                               and not m.get('answered') for m in cur.get('messages', [])))
            if decision:
                raise ValueError('acceptance or user decision required; technical cleanup completed')
            if (permitted() and cur.get('attempt_dir') and not st.get('recovery_issues')
                    and not st.get('stop_requested') and not st.get('paused')):
                with lock(self.state_dir / 'goal-intent.lock'):
                    if not permitted():
                        raise ValueError('technical recovery cancelled before launch')
                    self.launcher.resume_ready_launches(cur['attempt_dir'], self.state_dir, permitted=permitted)
                self.recover(st, technical_only=True)
            if st.get('recovery_issues'):
                raise RuntimeError('; '.join(st['recovery_issues']))
        elif action in ('reconcile', 'continue-operation'):
            self.recover(st, data.get('operation_id') if action == 'continue-operation' else None)
            if st.get('recovery_issues'):
                raise RuntimeError('; '.join(st['recovery_issues']))
        elif action == 'pause':
            st['paused'] = True
        elif action == 'reply':
            message_id = data.get('message_id')
            text = data.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 32000:
                raise ValueError('answer must contain 1..32000 characters')
            matches = [m for m in cur.get('messages', []) if m['id'] == message_id and (not data.get('run_id') or m['run_id'] == data['run_id'])]
            if len(matches) > 1:
                raise ValueError('message ID ambiguous; specify run_id')
            message = next((m for m in cur.get('messages', []) if m['id'] == message_id and (not data.get('run_id') or m['run_id'] == data['run_id']) and m.get('type') == 'question'), None)
            # Legacy state stores only question IDs.
            if not message and message_id not in cur.get('questions', []):
                raise ValueError('unknown question')
            if message and message.get('answered'):
                return
            if message and message.get('closed'):
                raise ValueError(message.get('reply_error') or 'question is closed')
            run_id = message['run_id'] if message else st['run_id']
            mailbox = None
            if cur.get('attempt_dir'):
                mailbox = next((r.get('mailbox') for r in self.launcher.registry(cur['attempt_dir'])['runs']
                                if r.get('run_id') == run_id), None)
            try:
                self.effect(st, 'reply:' + run_id + ':' + message_id, 'orchestration', 'reply', '--id', message_id, '--body', text, '--run', run_id, mailbox=mailbox)
            except operations.RejectedOperation as exc:
                if message:
                    message.update(closed=True, reply_error=str(exc))
                raise
            if message:
                message['answered'] = True
        elif action == 'resolve':
            if data.get('choice') not in ('start', 'skip', 'stop') or not cur.get('gate_id'):
                raise ValueError('no pending gate or invalid resolution')
            if cur.get('gate_resolved') or cur.get('gate_resolution'):
                return
            self.effect(st, 'resolve:' + cur['gate_id'], 'orchestration', 'gate-resolve', '--id', cur['gate_id'], '--resolution', data['choice'])
            cur['gate_resolution'] = data['choice']
        elif action == 'stop':
            st['stop_requested'] = True
            self.save_state(st)  # Fence runner before interacting with Orca.
            if cur.get('attempt_dir'):
                reg = self.launcher.registry(cur['attempt_dir'])
                if cur.get('dispatch_id') and not any(a.get('dispatch_id') == cur['dispatch_id'] for a in reg['agents']):
                    st['stop_status'] = 'unknown'
                    raise ValueError('current Dispatch missing from ownership registry; reconcile first')
                st['stop_status'] = 'stopped' if self.launcher.stop_attempt(cur['attempt_dir']) else 'unknown'
                if st['stop_status'] != 'stopped':
                    reg = self.launcher.registry(cur['attempt_dir'])
                    errors = [f"{r.get('role') or r.get('run_id') or 'resource'}: {r['error']}"
                              for r in reg['agents'] + reg['runs'] if r.get('error')]
                    st['stop_error'] = '; '.join(errors) or 'stop unconfirmed; inspect Orca resources'
                    raise RuntimeError(st['stop_error'])
            elif cur.get('dispatch_id'):
                # Old state has no child registry. Do not infer ownership or restart anything.
                st['stop_status'] = 'unknown'
                raise ValueError('legacy attempt lacks child ownership registry; inspect Orca resources before retry')
            else:
                st['stop_status'] = 'stopped'
            st.pop('stop_error', None)
            st.pop('last_error', None)
        elif action == 'retry':
            if not cur:
                raise ValueError('no attempt to retry')
            if running(self.state_dir) and st.get('runner_status') == 'running':
                raise ValueError('stop the current attempt before retry')
            if st.get('stop_status') == 'unknown':
                raise ValueError('stop unconfirmed; inspect resources and repeat stop before retry')
            if cur.get('attempt_dir'):
                if st.get('stop_status') != 'stopped':
                    if not data.get('force'):
                        raise ValueError('stop attempt first (or retry --force)')
                    self.apply_control(st, 'stop', {})
                    if st.get('stop_status') != 'stopped':
                        raise ValueError('stop unconfirmed')
                old_config = read_json(Path(cur['attempt_dir']) / 'config.json')
            else:
                raise ValueError('legacy attempt has no ownership registry; retry requires manual reconciliation')
            extra = cur.get('attempt', 1) >= 3
            if extra and data.get('allow_extra_attempt') is not True:
                raise ValueError('three failed attempts: explicitly authorize one extra attempt')
            if extra:
                from .recovery import _unknown
                if st.get('control_uncertain') or st.get('recovery_issues') or _unknown(self, st):
                    raise ValueError('operation outcome unknown; reconcile before extra attempt')
                reg = self.launcher.registry(cur['attempt_dir'])
                if (any(a.get('status') not in ('stopped', 'released', 'superseded') for a in reg['agents'])
                        or any(r.get('status') != 'closed' for r in reg['runs'])):
                    raise ValueError('executors not confirmed stopped')
            cur['recovery_issues'] = list(st.get('recovery_issues') or [])
            if st.get('last_error'):
                cur['last_error'] = st['last_error']
            st.setdefault('attempts', []).append(dict(cur))
            st.pop('recovery_issues', None)
            st.pop('last_error', None)
            options = dict(st.get('options', {}))
            if data.get('saved_settings'):
                if not data.get('refresh'):
                    raise ValueError('saved settings require refresh')
                for field in config.FIELDS | {'fast'}:
                    options.pop(field, None)
            snapshot = (self.load_config(options.get('config'),
                        {k: options.get(k) for k in config.FIELDS | {'fast'}}) if data.get('refresh') else old_config)
            st['options'] = options
            st['current'] = {k: cur[k] for k in ('item', 'start_head', 'dirty_before') if k in cur}
            st['current'].update(attempt=cur.get('attempt', 1) + 1, previous_dispatch=cur.get('dispatch_id'),
                                 gate_resolved=True, retry_config=snapshot,
                                 retry_context_from=None if data.get('refresh') else cur['attempt_dir'],
                                 started_at=dt.datetime.now().isoformat(timespec='seconds'))
            if extra:
                st['current']['manual_retry'] = {'previous_attempt': cur.get('attempt', 1),
                                               'command_id': command_id}
            st.pop('stop_requested', None)
            st.pop('stop_status', None)
            st['paused'] = False
        else:
            raise ValueError('unknown control action')
        if command_id:
            st.setdefault('applied_commands', []).append(command_id)
        operations.checkpoint('command:before_save')
        self.save_state(st)
        operations.checkpoint('command:after_save')


    def recover(self, st, request_id=None, *, technical_only=False):
        cur = st.get('current') or {}
        paths = [self.state_dir / 'operations.json']
        if cur.get('attempt_dir'):
            paths.append(self.launcher.journal(cur['attempt_dir']))
        if request_id:
            path = next((p for p in paths if any(o['request_id'] == request_id for o in read_json(p, {}).values())), None)
            if path is None:
                raise ValueError('unknown operation ID in current attempt')
            if path == paths[0]:
                operations.continue_operation(path, request_id, self.orca)
        issues = operations.reconcile(paths[0], self.orca)
        if st.get('mailbox_dir'):
            issues += operations.reconcile(self.launcher.journal(st['mailbox_dir']), self.orca)
        with lock(self.state_dir / 'start.lock'):
            startup_issues = operations.reconcile(self.launcher.journal(self.state_dir / 'startup'), self.orca)
            issues += startup_issues
            if not startup_issues:
                if running(self.state_dir) and st.get('runner_status') == 'running':
                    atomic_json(self.state_dir / 'startup.json', {'status': 'running'})
                elif not running(self.state_dir):
                    # All creation receipts proved, no process owns runner.lock.
                    atomic_json(self.state_dir / 'startup.json', {'status': 'idle'})
        if cur.get('attempt_dir'):
            recovery_args = {'accepted_reports': self.accepted_child_reports(cur['attempt_dir'])} if technical_only else {}
            issues += self.launcher.reconcile_attempt(cur['attempt_dir'], request_id if request_id and path != paths[0] else None, **recovery_args)
            if not technical_only:
                child_problems, child_resource_problems = self.child_acceptance_problems(st)
                cur['incomplete_checks'] = child_problems
                issues += child_resource_problems
            reg = self.launcher.registry(cur['attempt_dir'])
            self.close_completed_questions(st, reg)
            if cur.get('dispatch_id') and not any(a.get('dispatch_id') == cur['dispatch_id'] for a in reg['agents']):
                issues.append('current Dispatch missing from ownership registry')
            coordinator = next((a for a in reg['agents'] if a.get('role') == 'coordinator' and a.get('status') == 'active'), None)
            if coordinator:
                cur.update(dispatch_id=coordinator['dispatch_id'], terminal=coordinator['terminal'])
        elif cur.get('dispatch_id'):
            issues.append('legacy attempt lacks ownership evidence; recovery blocked')
        for op in read_json(paths[0], {}).values():
            if op.get('status') != 'rejected' or not operations.rejected_reply(op):
                continue
            args = op['args']
            if '--id' not in args or '--run' not in args:
                continue
            for message in cur.get('messages', []):
                if (message.get('id') == args[args.index('--id') + 1]
                        and message.get('run_id') == args[args.index('--run') + 1]):
                    message.update(closed=True, reply_error=op.get('error') or 'Dispatch inactive')
        uncertain = st.get('control_uncertain')
        if uncertain:
            command_path = self.state_dir / 'commands' / (uncertain + '.json')
            command = read_json(command_path, {})
            if 'schema_version' in command:
                # A v1 terminal receipt is immutable. Reconciliation may clear the
                # blocker from applied-state evidence, never rewrite/replay it.
                if uncertain not in st.get('applied_commands', []) and not issues:
                    from .application import _execution_scope
                    envelope = command.get('command') or {}
                    # Complete only an exact, still-current stop intention. Existing
                    # journal keys prohibit replay of an unconfirmed external effect.
                    if (command.get('action') == envelope.get('action') == 'stop'
                            and envelope.get('request_id') == uncertain == command.get('id')
                            and envelope.get('scope', {}).get('identity') == self.context.identity()
                            and command.get('stop_guard', command.get('guard')) == _execution_scope(self, st)):
                        from .recovery import set_intent
                        try:
                            self.apply_control(st, 'stop', {'_target': command['guard']['attempt']}, uncertain)
                            set_intent(self, 'stopped')
                        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
                            issues.append('stop reconciliation: ' + str(exc))
                if uncertain in st.get('applied_commands', []):
                    st.pop('control_uncertain', None)
                else:
                    issues.append('versioned command lacks applied-state evidence; inspect effects')
                uncertain = None
            data = command.get('data') or {}
            if 'schema_version' not in command and command.get('action') == 'reply':
                rejected = next((op for op in read_json(paths[0], {}).values()
                    if op.get('status') == 'rejected' and operations.rejected_reply(op)
                    and '--id' in op['args'] and op['args'][op['args'].index('--id') + 1] == data.get('message_id')
                    and '--run' in op['args'] and op['args'][op['args'].index('--run') + 1] == data.get('run_id')), None)
                if rejected:
                    error = rejected.get('error') or 'question closed: Dispatch inactive'
                    command.update(status='error', error=error, result={'state': 'error', 'error': error})
                    atomic_json(command_path, command)
                    for message in cur.get('messages', []):
                        if message.get('id') == data.get('message_id') and message.get('run_id') == data.get('run_id'):
                            message.update(closed=True, reply_error=error)
                    st.pop('control_uncertain', None)
                    uncertain = None
        if uncertain and not issues:
            command_path = self.state_dir / 'commands' / (uncertain + '.json')
            command = read_json(command_path)
            if uncertain in st.get('applied_commands', []):
                command.update(status='done')
            elif command and command['action'] in ('reconcile', 'continue-operation', 'technical-reconcile'):
                command.update(status='done')
            elif command and command['action'] == 'pause':
                st['paused'] = True
                st.setdefault('applied_commands', []).append(uncertain)
                command.update(status='done')
            elif command and command['action'] == 'stop' and st.get('stop_requested'):
                # The stop intention is durable. Reconciliation never issues a new stop/close.
                st.setdefault('applied_commands', []).append(uncertain)
                command.update(status='done')
            elif command and command['action'] in ('reply', 'resolve'):
                data = command['data']
                key = ('reply:' + next(m['run_id'] for m in cur['messages'] if m['id'] == data['message_id']) + ':' + data['message_id'] if command['action'] == 'reply' else 'resolve:' + cur['gate_id'])
                recorded = read_json(paths[0], {}).get(key, {})
                if recorded.get('status') == 'completed':
                    self.apply_control(st, command['action'], data, uncertain)  # cached receipt only
                    command.update(status='done')
                else:
                    issues.append('command external receipt missing; blocked')
            else:
                issues.append('command has no applied-state evidence; blocked')
            if not issues:
                command.pop('error', None)
                atomic_json(command_path, command)
                st.pop('control_uncertain', None)
        if st.get('stop_requested') and cur.get('attempt_dir'):
            reg = self.launcher.registry(cur['attempt_dir'])
            stopped = all(a.get('status') in ('stopped', 'released', 'superseded') for a in reg['agents'])
            stopped = stopped and all(r.get('status') == 'closed' for r in reg['runs'])
            st['stop_status'] = 'stopped' if stopped and not issues else 'unknown'
            if st['stop_status'] == 'stopped':
                st.pop('stop_error', None)
        if st.get('recovery_issues') != issues or st.get('last_error'):
            st.setdefault('recovery_history', []).append({
                'attempt_dir': cur.get('attempt_dir'), 'issues': st.get('recovery_issues', []),
                'last_error': st.get('last_error'), 'at': dt.datetime.now().isoformat(timespec='seconds')})
        repair = cur.get('acceptance_rework')
        if not technical_only and repair and not issues and not st.get('stop_requested') and cur.get('attempt_dir'):
            task_op = read_json(paths[0], {}).get('acceptance-rework:' + repair['dispatch_id'], {})
            task_id = ((task_op.get('result') or {}).get('task') or {}).get('id')
            completed = next((a for a in reg['agents'] if a.get('role') == 'coordinator'
                and a.get('status') == 'active' and a.get('reuse') == repair['dispatch_id']
                and task_op.get('status') == 'completed' and a.get('task_id') == task_id), None)
            if completed:
                cur.update(dispatch_id=completed['dispatch_id'], task_id=task_id,
                           terminal=completed.get('terminal'), acceptance_reworks=repair['number'])
                cur.pop('acceptance_rework')
                cur.pop('failed', None)
                cur.pop('acceptance_rework_error', None)
        if (not technical_only and cur.get('incomplete_checks') and not issues and not st.get('stop_requested')
                and cur.get('failed') and not cur.get('acceptance_rework')
                and cur.get('acceptance_reworks', 0) < MAX_ACCEPTANCE_REWORKS):
            report = self.accepted_child_reports(cur['attempt_dir']).get(cur.get('dispatch_id'))
            previous = next((h for h in reversed(st.get('history', []))
                if h.get('attempt_dir') == cur.get('attempt_dir') and h.get('dispatch_id') == cur.get('dispatch_id')), {})
            if (report or previous).get('outcome') == 'succeeded':
                cur['acceptance_rework'] = {'number': cur.get('acceptance_reworks', 0) + 1,
                    'dispatch_id': cur['dispatch_id'], 'problems': list(cur['incomplete_checks'])}
                cur.pop('failed', None)
        cur['recovery_issues'] = list(issues)
        st['recovery_issues'] = issues
        st.pop('last_error', None)
        self.save_state(st)


    def process_controls(self, st):
        for path in sorted((self.state_dir / 'commands').glob('*.json'), key=lambda p: p.stat().st_mtime_ns):
            rec = read_json(path)
            if 'schema_version' in rec:
                from .application import process_v1
                process_v1(self, st, path, rec)
                continue
            if rec['id'] in st.get('applied_commands', []):
                rec.update(status='done')
                rec.pop('error', None)
                atomic_json(path, rec)
                continue
            if rec['status'] == 'applying':
                rec.update(status='unknown', error='runner interrupted during command; inspect before repeating')
                st['control_uncertain'] = rec['id']
                self.save_state(st)
                atomic_json(path, rec)
                continue
            if rec['status'] != 'pending':
                continue
            if st.get('control_uncertain') and rec['action'] not in ('reconcile', 'continue-operation', 'technical-reconcile'):
                continue
            rec['status'] = 'applying'
            atomic_json(path, rec)
            try:
                self.apply_control(st, rec['action'], rec['data'], rec['id'])
                rec['status'] = 'done'
            except operations.UncertainOperation as exc:
                st.setdefault('control_uncertain', rec['id'])
                rec.update(status='unknown', error=str(exc))
                self.save_state(st)
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                rec.update(status='error', error=str(exc))
                self.save_state(st)
            rec['result'] = {'state': 'ok' if rec['status'] == 'done' else 'error',
                             'error': rec.get('error') if rec['status'] != 'done' else None}
            atomic_json(path, rec)

