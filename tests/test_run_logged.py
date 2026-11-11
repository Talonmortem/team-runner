#!/usr/bin/env python3
"""Offline regression tests for team_runner.run_logged.

They prove the three properties the wrapper exists for: the full output always reaches a log
file outside Git, the agent-facing receipt stays small and marks every truncation, and the
command's own exit code survives untouched. Run: python3 tests/test_run_logged.py
Exit code 0 = all passed. No Orca runtime, network or repository writes are required.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(os.path.dirname(HERE), "src", "team_runner", "run_logged.py")

spec = importlib.util.spec_from_file_location("run_logged", MODULE_PATH)
run_logged = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_logged)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"FAIL {name} {detail}")


def wrapper(logs: str, *argv: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in ("TEAM_RUNNER_LOGS", "TEAM_RUNNER_RUN", "TEAM_RUNNER_ATTEMPT",
                "TEAM_RUNNER_MATERIALS", "XDG_STATE_HOME"):
        env.pop(key, None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, MODULE_PATH, *argv], shell=False,
                          capture_output=True, text=True, env=env)


def run_cmd(logs: str, label: str, *command: str, extra: tuple = ()) -> subprocess.CompletedProcess:
    return wrapper(logs, "run", "--label", label, "--logs-dir", logs, *extra, "--", *command)


def receipt_field(stdout: str, key: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(key):
            return line[len(key):].strip()
    return ""


with tempfile.TemporaryDirectory() as tmp:
    logs = os.path.join(tmp, "logs")

    # Defaults and overrides must work without shell expansion or a live runner.
    attempt = os.path.join(tmp, "attempt")
    state = os.path.join(tmp, "state")
    home = os.path.join(tmp, "home")
    env_logs = os.path.join(tmp, "env-logs")
    base_env = {"HOME": home, "XDG_STATE_HOME": state,
                "TEAM_RUNNER_ATTEMPT": attempt,
                "TEAM_RUNNER_MATERIALS": os.path.join(attempt, "materials")}
    cases = (
        ("attempt-default", {}, (), os.path.join(attempt, "logs")),
        ("attempt-before-run", {}, ("--run", "other"), os.path.join(attempt, "logs")),
        ("env-override", {"TEAM_RUNNER_LOGS": env_logs}, (), env_logs),
        ("cli-override", {"TEAM_RUNNER_LOGS": env_logs}, ("--logs-dir", logs), logs),
        ("xdg-run", {"TEAM_RUNNER_ATTEMPT": "", "TEAM_RUNNER_RUN": "env-run"}, (),
         os.path.join(state, "team-runner", "runs", "env-run", "logs")),
        ("xdg-cli-run", {"TEAM_RUNNER_ATTEMPT": "", "TEAM_RUNNER_RUN": "env-run"},
         ("--run", "cli-run"), os.path.join(state, "team-runner", "runs", "cli-run", "logs")),
        ("home-unbound", {"TEAM_RUNNER_ATTEMPT": "", "XDG_STATE_HOME": ""}, (),
         os.path.join(home, ".local", "state", "team-runner", "runs", "unbound", "logs")),
        ("literal-materials-ignored", {"TEAM_RUNNER_ATTEMPT": "",
                                      "TEAM_RUNNER_MATERIALS": "$TEAM_RUNNER_MATERIALS"}, (),
         os.path.join(state, "team-runner", "runs", "unbound", "logs")),
    )
    for label, env, extra, expected in cases:
        result = wrapper(logs, "run", "--label", label, *extra, "--",
                         sys.executable, "-c", "print('default-path-ok')",
                         env_extra={**base_env, **env})
        out_path = receipt_field(result.stdout, "STDOUT").split("  ")[0]
        check(f"{label} writes logs in the expected directory",
              result.returncode == 0 and os.path.dirname(out_path) == expected
              and os.path.isfile(out_path), result.stderr or out_path)

    # 1. Successful command: exit 0, full stdout on disk, short receipt.
    ok = run_cmd(logs, "success", sys.executable, "-c",
                 "print('line one'); print('line two'); print('ok github.com/Talonmortem/SHM/db')")
    check("success exit code is 0", ok.returncode == 0, f"got {ok.returncode}")
    check("success receipt reports EXIT 0", receipt_field(ok.stdout, "EXIT") == "0",
          receipt_field(ok.stdout, "EXIT"))
    out_log = receipt_field(ok.stdout, "STDOUT").split("  ")[0]
    check("success stdout log exists outside the repo", os.path.exists(out_log), out_log)
    check("success log holds the complete output",
          open(out_log, encoding="utf-8").read().splitlines() ==
          ["line one", "line two", "ok github.com/Talonmortem/SHM/db"])
    check("success receipt stays short", len(ok.stdout.splitlines()) <= 15,
          f"{len(ok.stdout.splitlines())} lines")
    check("success receipt carries the read-back command", "show --log" in ok.stdout)

    # 2. Failing command: original exit code preserved, bounded stderr diagnostics.
    bad = run_cmd(logs, "failure", sys.executable, "-c",
                  "import sys\n"
                  "print('progress line')\n"
                  "sys.stderr.write('boom: constraint violated\\n')\n"
                  "sys.exit(3)")
    check("failure preserves the child exit code", bad.returncode == 3, f"got {bad.returncode}")
    check("failure receipt reports EXIT 3", receipt_field(bad.stdout, "EXIT") == "3",
          receipt_field(bad.stdout, "EXIT"))
    check("failure receipt shows the stderr diagnostic",
          "boom: constraint violated" in bad.stdout)
    err_log = receipt_field(bad.stdout, "STDERR").split("  ")[0]
    check("failure stderr log holds the original",
          "boom: constraint violated" in open(err_log, encoding="utf-8").read())

    for code in (1, 2, 42, 125, 127):
        res = run_cmd(logs, f"exit-{code}", sys.executable, "-c", f"raise SystemExit({code})")
        check(f"exit code {code} survives the wrapper", res.returncode == code,
              f"got {res.returncode}")

    # 3. Large output: full log kept, receipt bounded, truncation stated explicitly.
    big = run_cmd(logs, "big", sys.executable, "-c",
                  "for i in range(20000): print(f'--- PASS: TestNumber{i} (0.01s)')")
    big_log = receipt_field(big.stdout, "STDOUT").split("  ")[0]
    check("big output exits 0", big.returncode == 0, f"got {big.returncode}")
    check("big log keeps all 20000 lines",
          sum(1 for _ in open(big_log, encoding="utf-8")) == 20000)
    check("big receipt stays small", len(big.stdout) < 4000, f"{len(big.stdout)} chars")
    check("big receipt marks truncation", "TRUNCATED" in big.stdout)
    check("big receipt states the real line count", "of 20000 lines" in big.stdout)
    check("big receipt counts markers", "PASS=20000" in big.stdout, receipt_field(big.stdout, "MARKERS"))

    # A single enormous line is clipped with an explicit marker, not silently cut.
    longline = run_cmd(logs, "longline", sys.executable, "-c", "print('x' * 5000)")
    check("long line is clipped with a marker", "[truncated line: +4500 chars]" in longline.stdout)
    long_log = receipt_field(longline.stdout, "STDOUT").split("  ")[0]
    check("long line is complete in the log",
          len(open(long_log, encoding="utf-8").read().strip()) == 5000)

    # 4. Selective read-back of the untouched original.
    head = wrapper(logs, "show", "--log", big_log, "--head", "3")
    check("show --head prints the requested lines", head.stdout.count("\n") == 5, head.stdout[:200])
    check("show --head marks truncation", "TRUNCATED" in head.stdout)
    grep = wrapper(logs, "show", "--log", big_log, "--grep", "TestNumber19999")
    check("show --grep finds the matching line", "TestNumber19999" in grep.stdout)
    check("show --grep stays bounded", len(grep.stdout.splitlines()) <= 6,
          f"{len(grep.stdout.splitlines())} lines")
    capped = wrapper(logs, "show", "--log", big_log, "--grep", "PASS", "--max-lines", "10")
    check("show honours --max-lines", len(capped.stdout.splitlines()) <= 12,
          f"{len(capped.stdout.splitlines())} lines")
    check("show states that the cap hid further matches", "CAP REACHED" in capped.stdout)
    empty = wrapper(logs, "show", "--log", big_log, "--grep", "NoSuchMarker")
    check("show does not call an empty grep a truncation", "TRUNCATED" not in empty.stdout,
          empty.stdout[:200])
    check("show reports an exhaustive grep as complete", "all matches shown" in empty.stdout)
    check("show reports the log hash", "sha256" in capped.stdout)
    check("show leaves the original untouched",
          sum(1 for _ in open(big_log, encoding="utf-8")) == 20000)

    # 5. The run index lets a later agent find evidence without listing the directory.
    index = os.path.join(logs, "run-index.jsonl")
    records = [json.loads(line) for line in open(index, encoding="utf-8")]
    check("index has one record per invocation", len(records) >= 8, f"{len(records)} records")
    check("index records the exit code", any(r["label"] == "failure" and r["exit"] == 3
                                             for r in records))
    check("index records log hashes",
          all(len(r["stdout"]["sha256"]) == 64 for r in records))

    # 6. A repeated label never overwrites earlier evidence.
    first = run_cmd(logs, "repeat", sys.executable, "-c", "print('first')")
    second = run_cmd(logs, "repeat", sys.executable, "-c", "print('second')")
    check("repeated label writes a new file",
          receipt_field(first.stdout, "STDOUT") != receipt_field(second.stdout, "STDOUT"))

    # 7. Arguments are data: no shell ever sees them.
    canary = os.path.join(tmp, "canary")
    hostile = run_cmd(logs, "hostile", sys.executable, "-c", "import sys; print(sys.argv[1])",
                      f"$(touch {canary})`touch {canary}`")
    check("hostile argument is not executed", not os.path.exists(canary))
    hostile_log = receipt_field(hostile.stdout, "STDOUT").split("  ")[0]
    check("hostile argument arrives verbatim",
          f"$(touch {canary})`touch {canary}`" in open(hostile_log, encoding="utf-8").read())
    check("run_logged.py never uses a shell",
          "shell=True" not in open(MODULE_PATH, encoding="utf-8").read())

    # 8. A timeout is reported as such and does not masquerade as a command failure.
    slow = run_cmd(logs, "slow", sys.executable, "-c", "import time; time.sleep(30)",
                   extra=("--timeout", "1"))
    check("timeout exits 124", slow.returncode == 124, f"got {slow.returncode}")
    check("timeout is stated explicitly", "TIMEOUT" in slow.stdout)

    # 9. Logs are refused inside a Git work tree so evidence never lands in the repository.
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", repo], shell=False, check=True, capture_output=True)
    inside = wrapper(logs, "run", "--label", "in-repo",
                     "--logs-dir", os.path.join(repo, "team", "logs"),
                     "--", sys.executable, "-c", "print('x')")
    check("log directory inside Git is refused", inside.returncode == 125, f"got {inside.returncode}")
    check("refusal names the work tree", "refusing to write logs inside the Git work tree"
          in inside.stderr)
    check("refusal wrote nothing", not os.path.exists(os.path.join(repo, "team")))
    check("--allow-repo-logs is the documented escape hatch",
          wrapper(logs, "run", "--label", "in-repo", "--logs-dir",
                  os.path.join(repo, "team", "logs"), "--allow-repo-logs",
                  "--", sys.executable, "-c", "print('x')").returncode == 0)

    # 10. A stray, empty .git directory is not a work tree and must not block logging.
    stray = os.path.join(tmp, "stray")
    os.makedirs(os.path.join(stray, ".git"))
    stray_logs = os.path.join(stray, "logs")
    check("stray .git does not block logging",
          wrapper(logs, "run", "--label", "stray", "--logs-dir", stray_logs,
                  "--", sys.executable, "-c", "print('x')").returncode == 0)

    # 11. Helper-level checks.
    check("inside_git_worktree finds the root",
          run_logged.inside_git_worktree(os.path.join(repo, "team", "logs"))
          == os.path.realpath(repo))
    check("inside_git_worktree returns None outside", run_logged.inside_git_worktree(logs) is None)
    check("git_dir_ancestor fallback finds a .git entry",
          run_logged.git_dir_ancestor(stray_logs) == stray)

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("all checks passed")
