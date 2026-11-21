# Управляющие команды и квитанции

Работай из внешнего операторского контекста. Возьми `PROJECT_UUID` из `project list`, `WORKSPACE_UUID` и checkout из `workspace list` для этого проекта. Для команд с ограждением возьми из свежего подтверждённого `status --json` значения `generation`, `current.attempt` и `revision` (ниже `GENERATION`, `ATTEMPT`, `REVISION`). Передавай строку `null` в CLI для отсутствующих generation или attempt. Для `execute --resume` используй пару `generation` и `epoch` из того же ответа. При `stale_scope` перечитай статус и проверь, что разрешение пользователя всё ещё относится к текущему исполнению.

Для каждой новой операции используй отдельный UUID (`python3 -c 'import uuid; print(uuid.uuid4())'`). До отправки запиши UUID, проект, workspace и исходные аргументы. В примерах `REQUEST_UUID` обозначает UUID конкретной операции. Не выводи секретные токены или необязательные тексты сообщений.

## Потерянный ответ

Для всех workspace-команд на этой странице, включая `execute` и `command --file`, ищи исходную квитанцию в том же workspace:

```sh
team-runner operation show --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
  --request-id "$REQUEST_UUID" --json
team-runner operation wait --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
  --request-id "$REQUEST_UUID" --until operation --timeout 15 --json
```

Сверь `request_id`, действие, scope и результат. Для `pending` или `running` дождись результата в пределах разумного интервала и перечитай статус. При `unknown` смотри [неподтверждённый эффект](recovery.md#неподтверждённый-эффект). При `operation_not_found` проверь UUID, проект, workspace и свежий статус. Отсутствие записи оставляет исход неподтверждённым. При `failed` прочитай ошибку и проверь фактический статус. После таймаута не отправляй новую команду с другим UUID для той же операции. `succeeded` означает успех управляющей операции. Фактический переход или работу агента проверяй отдельно.

У `service start` журнал проектный. Исходная запись лежит в `projects/PROJECT_UUID/service/commands/REQUEST_UUID.json` под `$XDG_STATE_HOME/team-runner` (по умолчанию `~/.local/state/team-runner`). Сначала запроси `service status`, при необходимости выполни `service reconcile`. Для уже записанного запроса `start` с тем же UUID можно запросить исходную операцию той же командой. Если записи нет, повторный вызов может стать первой отправкой. При неизвестном исходе сохрани блокировку. См. [запуск через CLI](../../../../README.md#для-агента-запуск-через-cli).

## Запуск

Для запроса запуска только сервиса проверь, что он подтверждённо остановлен, затем выполни:

```sh
team-runner service start --project "$PROJECT_UUID" --request-id "$REQUEST_UUID"
team-runner service status --project "$PROJECT_UUID"
```

Проверь `state=running`, `confirmed=true` и порт. Для явно согласованного запуска подготовленной цели возьми её путь из `goals` выбранного workspace и проверь, что workspace готов к запуску этой цели. Пример использует согласованный режим `confirm`. Уточни согласованный объём очереди и финальную проверку по плану цели.

```sh
team-runner goals --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" --json
team-runner execute --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
  --goal "$GOAL_PATH" --mode confirm --request-id "$REQUEST_UUID" --json
```

`execute` может поднять сервис. Проверь квитанцию, затем новую generation, текущий пункт/попытку и свежие признаки работы или ожидающий gate.

## Пауза, остановка, подготовка повтора

Выполняй одну нужную команду из примеров, с её собственным UUID и свежим scope.

```sh
team-runner pause --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
  --generation "$GENERATION" --attempt "$ATTEMPT" --revision "$REVISION" \
  --request-id "$REQUEST_UUID" --json
team-runner stop --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
  --generation "$GENERATION" --attempt "$ATTEMPT" --revision "$REVISION" \
  --request-id "$REQUEST_UUID" --json
team-runner retry --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
  --generation "$GENERATION" --attempt "$ATTEMPT" --revision "$REVISION" \
  --request-id "$REQUEST_UUID" --json
```

- **Пауза.** Проверь `desired_state=paused`. При `pausing` текущий пункт ещё заканчивается. Подтверди наблюдаемую паузу или сохранённый gate и отсутствие старта следующего пункта.
- **Остановка.** Проверь `desired_state=stopped`, подтверждённое `observed_state=stopped` и остановку ресурсов этой попытки. `stopping` или `unknown_effect` требуют сверки.
- **Повтор.** После подтверждения остановки прежних ресурсов и устранения неизвестных эффектов проверь, что доступен `retry`. Сверь квитанцию и свежий статус подготовленного пункта, паузу и отсутствие нового запуска. Продолжение выполняй отдельной командой, если оно уже разрешено пользователем. По умолчанию сохраняется снимок настроек. Обновление контекста, дополнительная попытка сверх лимита и другие варианты описаны в разделах [«Управление»](../../../../README.md#управление) и [«Состояние и восстановление»](../../../../README.md#состояние-и-восстановление).

## Передача очереди обязательному предшественнику

1. Сними свежий статус и выполни проверку с квалифицированным ID `PREDECESSOR` в виде `FEATURE/STEP`.

   ```sh
   team-runner prepare-dependency-handoff --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
     --generation "$GENERATION" --epoch "$EPOCH" --attempt "$ATTEMPT" --revision "$REVISION" \
     --predecessor "$PREDECESSOR" --dry-run --json
   ```

2. Если получен `eligible`, выполни отдельно согласованный `stop` по свежему scope и с собственным UUID. Дождись подтверждённого выхода runner, завершения назначений и закрытия ресурсов попытки, включая mailbox. При `unknown_effect` сверь исходную квитанцию и назначение координатора по [процедуре восстановления](recovery.md#неподтверждённый-эффект).
3. Перечитай `generation`, `epoch`, `current.attempt` и `revision` из статуса после остановки. Повтори команду подготовки с этими значениями и `--request-id "$HANDOFF_UUID"` вместо `--dry-run`. При прерванной записи повтори тот же запрос с исходными значениями и UUID. Квитанция подготовки подтверждает изменение очереди и новую generation на паузе. Старый каталог попытки закреплён через `.keep`.
4. При наличии отдельного разрешения выполни `execute --resume` с новой generation, её epoch и отдельным UUID. Сверь свежую активность исполнителя предшественника или ожидающий gate. Для SHM проверь новый Dispatch WA-G04. Runner допустит новую попытку WA-GATE после штатной приёмки WA-G04 и сверки зависимости в `Tasks.json`.

## Продолжение

Для штатной паузы или подготовленного повтора используй сохранённые параметры и свежие generation/epoch. Для старого исполнения с `compatibility=legacy` и без generation опусти оба аргумента при явно разрешённом возобновлении. Для остановленной попытки сначала разберись, требуется ли подготовка повтора. Живую попытку оставь работать.

```sh
team-runner execute --resume --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
  --generation "$GENERATION" --epoch "$EPOCH" --request-id "$REQUEST_UUID" --json
```

Если блокировка — `confirmation_required`, по явному разрешению подтверждай текущий gate отдельной командой:

```sh
team-runner resolve start --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
  --generation "$GENERATION" --attempt "$ATTEMPT" --revision "$REVISION" \
  --request-id "$REQUEST_UUID" --json
```

Проверь квитанцию, снятие соответствующей паузы или gate и свежую активность того же исполнения. При смене попытки сверь новый Dispatch и его работу.

## Вопрос или эскалация

```sh
team-runner messages --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" --json
team-runner reply "$MESSAGE_ID" --run-id "$RUN_ID" --file "$ANSWER_FILE" \
  --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
  --generation "$GENERATION" --attempt "$ATTEMPT" --revision "$REVISION" \
  --request-id "$REQUEST_UUID" --json
```

Возьми `items[].id` и `items[].run_id` активного вопроса или эскалации, сверь по полному статусу регистрацию Dispatch и принадлежность текущей попытке. Используй согласованный ответ. Для эскалации запиши JSON в `COMMAND_FILE`. Подставь исходный объект `identity` из краткого статуса и значения scope. Используй JSON `null` для отсутствующих значений scope.

```json
{
  "schema_version": 1,
  "request_id": "REQUEST_UUID",
  "action": "resolve-escalation",
  "scope": {
    "identity": {
      "project_id": "PROJECT_UUID",
      "workspace_id": "WORKSPACE_UUID",
      "path": "/canonical/checkout",
      "orca_id": "ORCA_ID",
      "orca_instance": "ORCA_INSTANCE"
    },
    "generation": "GENERATION",
    "attempt": "ATTEMPT",
    "revision": "REVISION"
  },
  "payload": {
    "message_id": "MESSAGE_ID",
    "run_id": "RUN_ID",
    "text": "Согласованное решение и причина",
    "resume": false
  }
}
```

```sh
team-runner command --project "$PROJECT_UUID" --workspace "$WORKSPACE_UUID" \
  --file "$COMMAND_FILE" --json
```

`resume: false` фиксирует решение и закрывает эскалацию. При явном разрешении доставить решение и продолжить тот же Dispatch используй `resume: true`. Проверь квитанцию и состояние конкретного сообщения, затем отдельно проверь старт нового хода, если он запрошен. См. [операторское разрешение эскалаций](../../../../README.md#verification-scope-and-coordinator-events).
