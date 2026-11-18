# Observer operations — SHM

Service checkout: `/home/talonmortem/repo/team-runner`. Dedicated project `93f71065-93e6-478f-9fa0-75eb9a4d218b`, workspace `a537ed43-951f-41d4-a01e-185c63f9bf15`. Observed allowlist: SHM project `205b0710-cd16-44c9-adce-0546f351f728` only; current workspace `2146a50e-8c8c-4a0e-89ee-d68363199260`.

Run from an operator shell outside a Runner child attempt. Use the installed CLI/interpreter: system Python with PYTHONPATH does not propagate package availability to Orca terminals. These commands use the exact catalog/state roots. `start` reconnects to a confirmed existing process or starts one when absence is confirmed; it also resumes after pause.

```sh
tr() {
  XDG_CONFIG_HOME=/home/talonmortem/.config \
  TEAM_RUNNER_STATE_ROOT=/home/talonmortem/.local/state/team-runner \
  /home/talonmortem/repo/team-runner/.venv/bin/team-runner "$@"
}
observer_change() {
  observer_action="$1"
  shift
  observer_scope="$(tr observer status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["scope"])')"
  observer_request="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  printf 'request_id=%s scope=%s\n' "$observer_request" "$observer_scope"
  tr observer "$observer_action" --request-id "$observer_request" --scope "$observer_scope" "$@" --json
}
observer_change start
tr observer status --json
tr observer incidents --json
observer_change pause
observer_change mode --mode diagnostic
observer_change mode --mode approval
observer_change mode --mode auto
observer_change stop
```

Run one mutation at a time; the block lists alternatives, not a batch to execute. On lost command response, retain the printed request ID/scope and retry the identical `tr observer ACTION --request-id ID --scope SCOPE ... --json` command. Do not create a replacement ID for an unconfirmed effect. Status includes process PID/boot/stamp/nonce and confirmation.

Initial configuration evidence: `evidence/rollout-config.json`; runtime policy: `/home/talonmortem/.local/state/team-runner/observer/state.json`. Loaded observer Python package files require controlled restart rather than automatic replacement. V1 uses Codex service roles and existing regular-file replacements only. Infrastructure repair roots are Team Runner package/tests and the SHM order-workflow/warehouse-dispatch test harness directories; product logic, deployment scripts and weakened checks are excluded.

## Ready supervision handoff

Переключитесь в надзор за инфраструктурным наблюдателем Team Runner. Используйте команды выше из operator-контекста. Проверяйте инциденты, расходы ресурсов и токенов, качество диагнозов, фактическую область изменений и независимость проверок. Самостоятельно не дублируйте ремонт. Не создавайте искусственных сбоев SHM. При нарушении остановите новые ремонты: `observer_change pause`; сохраните IDs инцидента/попытки/dispatch, snapshots, журналы операций, проверки и расходы; передайте evidence ROOT. Не меняйте работающие попытки SHM, их почту, snapshots, настройки и приёмку. Надзор заканчивается только после подтверждённого полного реального цикла ремонта и следующих 30 минут стабильной работы. Изолированный тест и реальный авторемонт учитываются раздельно.

## Evidence and stopping condition

`Tasks.json` and `Progress.txt` record factual gates. Isolated repair-cycle tests do not prove live auto-repair. If live observation yields no infrastructure defect, keep real-cycle/30-minute completion pending; never manufacture a SHM incident.

## Verified rollout state

2026-11-16: SHM-only `auto`; observer PID `1374461`, nonce `66e7b228-a2f8-4e1a-b8d5-d1047c1addf5`. Manager PID `1374432`. Successful diagnostic dispatch `ctx_87b21c457b5f` in service attempt `/home/talonmortem/.local/state/team-runner/projects/93f71065-93e6-478f-9fa0-75eb9a4d218b/workspaces/a537ed43-951f-41d4-a01e-185c63f9bf15/attempts/b24abd39-d7d0-4222-b4c9-857156a3ee2a`; signal suppressed, worker released, mailbox closed. All15final preservation/process/cleanup checks passed.

No real automatic repair has occurred. The full isolated cycle and successful live diagnosis are separate evidence. Real repair + subsequent30minutes stable supervision remain pending. The first system-Python launch failed before dispatch; ROOT proved and journaled exact owned-terminal cleanup, then restarted via the installed environment. See `evidence/bootstrap-cleanup.json` and `evidence/rollout-final.json`.

## Hourly review and bounded context update

2026-11-16 23:49 MSK: controlled restart completed after the prior diagnosis and
owned mailbox settled. Current observer PID `3938395`, nonce
`b777ae26-e402-4e78-9998-614bf7e397d7`; SHM-only `auto`, singleton confirmed,
fresh polling and no blocker. Periodic semantic reviews now run at most hourly
on meaningful activity; timer-only and identical state rewrites do not trigger
them. Specific fault windows and 10-second polling remain unchanged.

Frozen diagnosis input is bounded to 64 KiB, with a hashed immutable archive of
captured evidence and full policy. Current retained-data comparison: 56,928 bytes
instead of 28,716,751 bytes (504 times smaller); this is an input-size measurement,
not a measured token saving. Pending telemetry retains 100 transitions and reports
overflow explicitly. The next periodic review is eligible no earlier than
2026-11-17 00:37:40 MSK. Validation: `evidence/optimization.json`.
