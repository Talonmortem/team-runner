# Team Runner

Локальная платформа команд для существующих Git checkout. Python 3.11+, standard library; Orca — терминалы, Task/Dispatch и mailbox. После явного execute — автономная очередь и ограниченное техническое восстановление; без автоматического создания checkout, веток или worktree.

## Запуск

```sh
/home/talonmortem/repo/team-runner/.venv/bin/team-runner ui
# Только HTTP-сервис, без открытия вкладки:
/home/talonmortem/repo/team-runner/.venv/bin/team-runner serve --port 8765
```

Сервис слушает 127.0.0.1. Откройте напечатанный URL с токеном. Host, token и Origin проверяются. Закрытие общего UI не останавливает runner. Manager и supervisor продолжают только сохранённое разрешённое выполнение в пределах policy.

В UI выберите проект/workspace и цель, нажмите **«Выполнить»**. Manager и сервис обеспечиваются общим execute; ручной service start не нужен. По умолчанию новая цель: auto, final и recovery включены. «Продолжить» использует сохранённую политику. Очередь N из M и финальная приёмка показываются отдельно.

Роли, резервные роли, Fast, профиль и политика запуска находятся в «Настройках»; сохранение явно и не меняет активный снимок. Сервисы, порты, reconcile, операции и журнал находятся в «Диагностике». При unknown новый запрос не отправляется автоматически; сохранённый ID можно проверить без повторения эффекта. Вопросы и требуемые решения показаны над командой.

## Подключение проекта

```sh
team-runner project add --name Example --profile-file /path/profile.json
team-runner project list
team-runner workspace add --project PROJECT_UUID --path /existing/git/checkout
team-runner workspace list --project PROJECT_UUID
team-runner workspace update --project PROJECT_UUID --workspace WORKSPACE_UUID --profile-file /path/profile.json
team-runner workspace disable --project PROJECT_UUID --workspace WORKSPACE_UUID
```

Адрес Orca разрешается по точному пути; при нескольких адресах нужны `--orca-id` и `--orca-instance` из `orca worktree show`. Проверяются Git root, canonical path, host=local и incarnation. Смена пути — новое подключение. Disable запрещён при active/unknown; история сохраняется. Те же действия доступны в панели.

`project update --project ID --name NAME|--profile-file FILE|--roles-file FILE` меняет проектные defaults; `workspace update` — overrides каталога.

Профиль:

```json
{
  "instructions": ["AGENTS.md"],
  "requirements": ["docs/requirements.md"],
  "tracking_root": "feat",
  "evidence_dir": "evidence",
  "acceptance": {"require_commit": false, "require_clean": false},
  "checks": [{"cwd": ".", "argv": ["python3", "-m", "unittest", "discover"]}],
  "workflow": {"allow_commits": false},
  "constraints": ["Preserve existing edits"]
}
```

Instructions/requirements — локальные относительные или абсолютные пути. Их текст и SHA256, профиль, точная identity checkout/Orca и копия общих материалов фиксируются в `attempts/UUID/context.json` и `materials/`. Настройки ролей — `config.json`. Изменения применяются к следующим попыткам. `checks` — команды, обязательные для координатора/тестера; HTTP сам произвольные команды не выполняет.

Приоритет ролей: package defaults → project → workspace → сохранённые настройки каталога/`--config` → явные CLI overrides координатора → snapshot. Role override-файл — объект `{"tester":{"model":"..."}}`. Полный `--config` — `{"version":2,"template":"current","roles":{...},"reserves":{...}}`; v1 читается, старое обновление сохраняет резервы. Secrets и содержимое provider credentials не копируются.

Встроенные редактируемые шаблоны: `current` («Текущие агенты»), `cloudru` («Cloud.ru») и `openai-only` («OpenAI-only»).

`openai-only`: все роли — Codex через `codex-lb`; координатор — `gpt-6.1-sol / medium`, критический ревьюер и эксперт эскалации — `gpt-6.1-sol / high`. Остальные роли и пустые резервы — как в `current`.
В UI выбор заполняет черновик; сохранение влияет только на следующие попытки.
Fast — необязательный boolean `fast` в основной роли и резерве: true/false либо отсутствие поля
для наследования runtime. `execute --fast` / `--no-fast` и совместимый `run` задают override
координатора для новой попытки. Codex: `service_tier="priority"` / `"default"`;
Claude: `--fast-mode on` / `off` только для поддерживаемых model/provider/profile.
OpenCode Fast не поддерживает. Доступность аккаунта проверяет runtime; сохранённый запрос
не доказывает применение провайдером. Активные snapshots не переписываются.

CLI требует явные `--project` и `--workspace`:

```sh
team-runner template list --project PROJECT_UUID --workspace WORKSPACE_UUID
team-runner template show cloudru --project PROJECT_UUID --workspace WORKSPACE_UUID
team-runner template apply current --project PROJECT_UUID --workspace WORKSPACE_UUID
```

Cloud.ru разрешён только внутри канонической границы `~/repo/sber`, включая резервные настройки.
Ключ `CLOUD_API` читается без shell из `/home/talonmortem/repo/sber/AI/.env` перед запуском;
он передаётся только окружению дочернего OpenCode. Смена ключа/effort запрещает reuse старой сессии.
Запрошенный effort, параметры запроса и подтверждённое применение провайдером — разные факты.

## Для агента: автономная цель без UI

Команды используют один каталог XDG; `PROJECT_UUID`, `WORKSPACE_UUID` и путь цели замените
значениями из `project list`, `workspace list` и `goals`. `execute` сохраняет разрешение
до запуска manager, сервиса проекта и runner. Для новой цели defaults: `mode=auto`,
финальная проверка и ограниченное восстановление включены. Явные overrides:
`--mode confirm`, `--no-final`, `--no-recovery`, `--fast` / `--no-fast` (при поддержке runtime).
Существующие snapshots не меняются; разрешения workflow и пределы попыток не расширяются.

```sh
PROJECT_ID=PROJECT_UUID
WORKSPACE_ID=WORKSPACE_UUID
GOAL_PATH=feat/goal-example
team-runner goals --project "$PROJECT_ID" --workspace "$WORKSPACE_ID" --json

# Создать файл один раз; повтор использует уже сохранённый UUID и тот же payload.
if [ ! -f execute-request-id ]; then
  python3 -c 'import uuid; print(uuid.uuid4())' > execute-request-id
fi
EXECUTE_ID="$(cat execute-request-id)"
team-runner execute --project "$PROJECT_ID" --workspace "$WORKSPACE_ID" \
  --goal "$GOAL_PATH" --request-id "$EXECUTE_ID" --json > execute.json
team-runner operation wait --project "$PROJECT_ID" --workspace "$WORKSPACE_ID" \
  --request-id "$EXECUTE_ID" --until goal --timeout 30 --json > wait.json
team-runner status --project "$PROJECT_ID" --workspace "$WORKSPACE_ID" --json > status.json
team-runner messages --project "$PROJECT_ID" --workspace "$WORKSPACE_ID" \
  --limit 100 --json > messages.json
```

Проверьте каждый код завершения и JSON перед следующим действием; не используйте
безусловный цикл повторных запусков. В новой shell прочитайте `execute-request-id`:
`operation show --request-id "$EXECUTE_ID" --json` с теми же project/workspace возвращает
квитанцию без mutation. После потерянного ответа можно повторить исходный `execute`
с тем же UUID и теми же аргументами. Другой payload с этим UUID — конфликт.
Новый UUID не разрешает повтор неизвестного внешнего эффекта.

Для ответа выберите конкретное открытое сообщение в `messages.json`, прочитайте
вопрос и задайте свой `ANSWER`. Используйте scope из уже проверенного `status.json`:

```sh
MESSAGE_ID='message-id-from-messages.json'
ANSWER='your approved answer'
if [ ! -f reply-request-id ]; then
  python3 -c 'import uuid; print(uuid.uuid4())' > reply-request-id
fi
REPLY_ID="$(cat reply-request-id)"
scope_value() {
  python3 -c 'import json, sys; s=json.load(open("status.json")); k=sys.argv[1]; v=s["current"]["attempt"] if k=="attempt" else s[k]; print("null" if v is None else v)' "$1"
}
team-runner reply "$MESSAGE_ID" "$ANSWER" --project "$PROJECT_ID" --workspace "$WORKSPACE_ID" \
  --request-id "$REPLY_ID" --generation "$(scope_value generation)" \
  --attempt "$(scope_value attempt)" --revision "$(scope_value revision)" --json
```

Задержанный ответ с устаревшим scope отклоняется. Повтор после потерянного ответа
использует тот же reply UUID, текст и сохранённый scope; не обновляйте scope под
старым UUID. Новый статус и новая попытка требуют нового решения и нового UUID.
Те же обязательные guards у JSON `pause`, `stop`, `retry`, `resolve`, `reconcile`,
`continue-operation`: `--request-id`, `--generation`, `--attempt`, `--revision`.
Отсутствующие generation/attempt передаются буквальным `null`.
`command --file envelope.json --json` принимает полный versioned envelope с явной identity.

| JSON exit | Значение |
| --- | --- |
| `0` | Запрошенное чтение или подтверждённая операция/ожидание успешны. |
| `2` | Неверный запрос, конфликт UUID или устаревший scope. |
| `3` | Операция pending/running/failed/unknown либо ожидание заблокировано; изучите JSON. |
| `4` | Истёк bounded wait; его `wait_status=timeout`, mutation не отменена. |

`operation wait --until operation` ждёт только квитанцию запуска, по умолчанию 30 секунд;
`--until goal` привязан к generation исходного execute. Максимум `--timeout` — 3600 секунд,
`0` означает однократную проверку. Wait только читает файлы, не удерживает workspace lock,
не вызывает Orca и не повторяет запуск. Прерывание клиента также не отменяет работу.
Неизменная квитанция `unknown` сохраняется даже при последующем подтверждённом завершении цели.

Для приёмки цели нужны `confirmed=true`, `observed_state=completed` и
`final_acceptance.status=accepted`; exit 0 у execute этого не доказывает.
При сохранённом `final=False` результат — `observed_state=queue_completed`,
`final_acceptance.status=not_required`: очередь завершена без финальной приёмки.
`status --json --detail` читает подробную проекцию; старые `--summary`, `--full` и
`operation UUID` сохранены. В JSON-режиме stdout содержит только JSON; диагностика — stderr.

Следующую страницу сообщений читайте через
`messages --after CURSOR --limit 100 --json` с теми же project/workspace,
сохраняя `next_cursor` до следующего чтения; `has_more` указывает оставшуюся страницу.
Cursor привязан к identity/generation/run/attempt, лимит 1..1000. Он остаётся на исходной
попытке при переходе очереди; для текущей попытки запросите messages без `--after`.
Чужой или устаревший cursor отвергается. Чтение не забирает сообщения из mailbox Orca.

Возобновление: `execute --resume --generation GENERATION --epoch EPOCH --request-id UUID --json`
с явными project/workspace и scope из проверенного статуса. Сохранённые mode/final/recovery,
включая legacy-policy, не заменяются defaults нового запуска; options вместе с resume запрещены.
Новый UUID сохраняйте до отправки. Дочерний агент `TEAM_RUNNER_ATTEMPT` имеет только
`launch/messages/reply/status` в своей текущей попытке; execute, operation wait, service,
manager и lifecycle controls ему запрещены.

Manager — отдельный detached процесс текущего интерпретатора и явных XDG roots.
`execute` обеспечивает его запуск; вручную доступны:

```sh
team-runner manager status
team-runner manager start --request-id REQUEST_UUID
team-runner manager stop --request-id STOP_REQUEST_UUID
```

`manager status` только читает persisted ownership; не запускает процессы и не делает RPC.
Успех `manager stop` означает запрос остановки: дождитесь подтверждённого `stopped`.
Он не останавливает уже работающие project services и runner. Ручной `service stop`
сначала отзывает разрешение watchdog, поэтому manager не запускает сервис снова
по прежнему разрешению. Следующий явный execute/start — новое разрешение.
Unknown/исчерпанный бюджет восстановления требует разбора; отсутствие receipt не разрешает replay.

## Управление (совместимый run)

```sh
team-runner run --project PROJECT_UUID --workspace WORKSPACE_UUID --goal feat/goal-example --mode confirm
team-runner status --project PROJECT_UUID --workspace WORKSPACE_UUID
team-runner pause --project PROJECT_UUID --workspace WORKSPACE_UUID
team-runner run --project PROJECT_UUID --workspace WORKSPACE_UUID --resume
team-runner reply MESSAGE_ID 'answer' --project PROJECT_UUID --workspace WORKSPACE_UUID
team-runner resolve start --project PROJECT_UUID --workspace WORKSPACE_UUID
team-runner stop --project PROJECT_UUID --workspace WORKSPACE_UUID
team-runner retry --project PROJECT_UUID --workspace WORKSPACE_UUID --refresh
team-runner reconcile --project PROJECT_UUID --workspace WORKSPACE_UUID
team-runner continue-operation REQUEST_UUID --project PROJECT_UUID --workspace WORKSPACE_UUID
```

Первый пункт запускается сразу; confirm создаёт gate перед следующими. Pause завершает текущий пункт либо сохраняет ожидающий gate. Stop останавливает только зарегистрированные ресурсы. Retry подготавливает новую попытку; запуск/возобновление отдельные. Без `--refresh` retry сохраняет прежние настройки, инструкции и профиль. После трёх попыток нужен разбор задачи.

При `worker_done succeeded` runner отдельно проверяет приёмку: статус `completed`,
`<goal>/<feature>/<evidence_dir>/<item-id lowercase>/acceptance.md` и требования профиля.
Ошибки приёмки возвращаются координатору до двух раз: новый Task/Dispatch в той же
подтверждённой сессии и неизменной попытке, с конкретными замечаниями. Старый
`worker_done` не засчитывается повторно. Это исправление текущего пункта, отдельное
от `retry`, который создаёт новую попытку. При `failed`, исчерпании возвратов,
неизвестном состоянии ресурсов или неподтверждённом cleanup runner останавливается,
сохраняя сессию координатора для разбора. Unknown effects требуют reconcile;
автоматического replay нет. Координатор закрывается после успешной приёмки и cleanup
либо по явному Stop. Финальное ревью использует свой `goal-final-review.md`.

Дочерние роли получают TEAM_RUNNER_PROJECT/WORKSPACE/STATE/CHECKOUT/ATTEMPT/MATERIALS. В этом контексте ID можно не повторять; конфликт explicit IDs с окружением отклоняется:

```sh
team-runner launch tester --spec /absolute/spec.md --title 'Checks' --function check-one
team-runner messages
team-runner launch tester --spec /absolute/next.md --title 'Follow-up' --function check-one --reuse DISPATCH_ID --context-percent 25
```

Reuse только implementer_standard/medium/complex и tester/tester_complex, внутри одной попытки, роли, функции, неизменных параметров и connection fingerprint. Прежний Dispatch должен завершиться, терминал — иметь прежний handle/incarnation/owner. Контекст 0..50% должен быть измерен, неизвестный/NaN запрещён. Отказ не создаёт новую сессию. Reviewer всегда свежий.

Резерв запускается явно: `team-runner launch ROLE --variant reserve --reason "причина" --spec /absolute/spec.md --title 'Replacement'`.
Только свежая сессия, без автоматического fallback. Предыдущий исполнитель должен завершиться или быть
остановлен; неизвестные операции сначала сверяются. Резерв отсутствующей в старом снимке роли не добавляется.
`templates/mkspec.py` поддерживает такие же `--variant reserve --reason` вместе с явным `--role`.

## Хранение и восстановление

Конфигурация: `$XDG_CONFIG_HOME/team-runner` (default `~/.config/team-runner`). Состояние: `$XDG_STATE_HOME/team-runner` (default `~/.local/state/team-runner`). Namespace: `projects/PROJECT_UUID/workspaces/WORKSPACE_UUID`. Lock и durable lease по SHA256 canonical checkout предотвращают второй runner даже через другой project или symlink. Unknown не освобождает владение.

Один отдельный процесс runner потребляет mailbox своего каталога. HTTP читает проекции и принимает UUID-команды; явный execute обеспечивает manager, сервис и runner через общий application layer. Повтор UUID возвращает прежний результат. Effects записывают intent до Orca и receipt после; неизвестный исход требует reconcile, не автоматического replay. `continue-operation` повторяет только записанный request ID со статусом pending/completed. Отсутствие процесса или receipt не доказывает безопасную повторную отправку. Чужой incarnation блокирует действия.

API: `/api/projects`, `/api/projects/PROJECT/workspaces`, `/api/projects/PROJECT/workspaces/WORKSPACE/{state,summary,goals,messages,config,profile,recovery,execute,start,pause,stop,retry,reply,resolve,reconcile,continue-operation,disable}`. Глобального current project и глобальных управляющих endpoints нет.

## Совместимость состояния и ограничения rollout

Исходники `feat/goal-autonomous-runner` находятся в отдельной копии проекта. По отдельному запросу пользователя wheel установлен в рабочую `.venv`; сервисы и runner не перезапускались. Уже работающие процессы сохраняют загруженную версию кода до завершения. Изменение установленного пакета само по себе не возобновляет остановленные цели.

Settings v1/v2 читаются совместимо; старые `context.json`, `config.json` и материалы попыток не переписываются и не дополняются. Defaults auto/final/recovery действуют для нового `execute`. Продолжение существующей цели использует сохранённую политику; legacy confirm не превращается в auto. Несовместимые schema/identity и старая execute-квитанция без service authorization отклоняются, а не получают новое разрешение автоматически. Недоступный сохранённый Fast можно убрать через «Наследовать»; запуск с ним до исправления остаётся запрещён.

Новые goal/service intents, epochs, manager/controller journals, completion receipts и v1 commands нельзя считать совместимыми со старым исполняемым кодом. Downgrade поверх активного или unknown состояния не поддерживается: старый runner может не понимать cancellation fences и формат команд. Не удаляйте журналы, locks, receipts или снимки для обхода ошибки. Unknown требует наблюдения исходного request ID и сверки фактов; timeout, отсутствие PID или receipt сами по себе не разрешают replay.

Для проверки старой версии используйте отдельные чистые XDG roots и fixture checkout, исключающие владение действующим runner. Изолированную установку можно убрать после подтверждённой остановки всех её manager/service/runner процессов; её состояние сохраняют отдельно, не подсовывают старой версии. Реальный rollout требует отдельного разрешения, сохранения исходного окружения и полного состояния, проверки остановки старых владельцев и только затем переключения версии. Перезапуск рабочих сервисов и проверка на live целях/провайдерах здесь не выполнялись.

## Проверки

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
# Legacy helper scripts содержат собственные assertions:
PYTHONPATH=src python3 tests/test_orca_msg.py
PYTHONPATH=src python3 tests/test_orca_recover.py
PYTHONPATH=src python3 tests/test_token_usage.py
PYTHONPATH=src python3 tests/runner_browser_check.py --worktree path:/EXPLICIT/ORCA/CHECKOUT --evidence /tmp/team-runner-browser
.venv/bin/python tests/runner_live_smoke.py --runtime codex --state /tmp/fresh-smoke --checkout /path/repo --orca-id 'exact-id'
```

Browser использует два временных проекта, реальные HTTP/процессы и fake Orca. Live smoke требует отдельного явного разрешения; команда выше не входит в обычный fixture-прогон. Он запускает только безвредное задание и continuation; при лимите провайдера или неизвестном контексте результат INCOMPLETE. Рабочую очередь тесты не запускают.

SHM migration inventory: `migration/source-manifest.json`. Старое состояние архивируется вне Git с checksum/read-only permissions, не импортируется как исполняемая попытка. Бизнес-правила и исторические отчёты остаются в SHM.

Новые роли: tester_complex (Sol high), final_reviewer_critical и escalation_expert (Opus high). Риск выбирают planner и координатор по MODELS.md. mkspec.py поддерживает --role для совместимого шаблона; отсутствующая роль snapshot отклоняется до записи. Legacy settings v1 загружаются поверх новых defaults; старый полный API payload сохраняет настройки новых ролей. Старые snapshots и материалы не дополняются; изменения действуют только для будущих попыток. Локальный каталог и saved overrides автоматически не мигрируются. Финальная проверка требует полный completed tracking и acceptance-артефакты, commit/clean по профилю.

Для вопросов с одинаковым message ID в разных дочерних run передайте `reply --run-id RUN_ID`; в JSON payload поле `run_id` выбирает ровно соответствующий сохранённый вопрос. Без него неоднозначный ID отклоняется.

Терминал, потерянный до передачи задания (Dispatch), восстанавливается автоматически только при точной квитанции создания, совпадении identity и подтверждённом завершении процесса и точном закрытии терминала Orca либо его отсутствии в полном списке локальных терминалов. При разрешённом running создаётся одна замена с новым UUID; старые квитанции и snapshot сохраняются. Повторная потеря исчерпывает лимит. Pause/stop запрещают замену; запрошенная остановка завершается по доказательствам. Неизвестный исход Dispatch блокирует перезапуск. Старый инцидент, уже остановленный в needs_action, после установки исправления можно сверить через «Диагностика → Восстановление → Сверить с Orca»; это не возобновляет цель.

Галочка «Автоматически продолжать исправления после пересмотра плана» в настройках агентов сохраняет `auto_continue_after_replan` (по умолчанию false). При true после неудачи экспертного цикла координатор сохраняет пересмотренный план и продолжает исправления и независимую проверку. Внешние блокеры и необходимые решения пользователя сохраняют остановку. Настройка действует в новой попытке; сохранение не меняет текущий snapshot и не возобновляет уже остановленную попытку. Для неё нужен повтор с обновлением настроек.
