# Team Runner

Локальная платформа команд для существующих Git checkout. Python 3.11+, standard library; Orca — терминалы, Task/Dispatch и mailbox. Без автоматического создания checkout, веток или worktree. Техническое восстановление разрешённых запусков выполняет supervisor сервиса проекта.

## Запуск

В Team Runner три отдельных уровня:

- **Общий UI** — страница со всеми проектами. Команда `team-runner ui` работает в открытом терминале до `Ctrl+C`.
- **Сервис проекта** — отдельный фоновый процесс для управления каталогами этого проекта. Запускается кнопкой «Запустить сервис» или командой `service start`.
- **Runner задачи** — процесс выполнения выбранной цели в конкретном workspace. Запускается отдельно; запуск UI задачи не запускает. Сервис продолжает только явно разрешённое восстановление ранее запущенных задач.

В командах ниже `team-runner` — установленный CLI. Если его нет в `PATH`, используйте полный путь `/home/talonmortem/repo/team-runner/.venv/bin/team-runner`. `PROJECT_UUID` и `WORKSPACE_UUID` — места для настоящих ID из `project list` и `workspace list`, а не имена проектов или каталогов.

### Для человека: запуск через браузер

1. Запустите общий UI:

```sh
/home/talonmortem/repo/team-runner/.venv/bin/team-runner ui
```

2. Откройте напечатанный URL целиком, включая `#token=…`, если вкладка не открылась автоматически. Не публикуйте этот URL: он даёт доступ к управлению.
3. Найдите карточку проекта. Если проекта ещё нет, создайте его и подключите существующий Git checkout по инструкции [«Подключение проекта»](#подключение-проекта).
4. Нажмите **«Запустить сервис»**. Дождитесь состояния **`running`** и номера порта. Для обычного запуска оставьте порт `0` — свободный порт выберется автоматически. Настройка порта на карточке относится к сервису проекта.
5. Выберите проект и рабочий каталог. Настройки и управление задачами станут доступны. Если нужно выполнить цель, выберите её и отдельно нажмите **«Запустить»** в разделе «Цель».

Для второго проекта повторите шаги 4–5 в том же UI. Закрытие вкладки или `Ctrl+C` в терминале UI оставляет сервисы и задачи работающими. Чтобы вернуться, снова выполните `team-runner ui` и откройте новый напечатанный URL.

Если UI уже работает, используйте существующую вкладку или URL из его терминала. Ошибка `already running: ui.lock` означает, что второй общий UI запускать не нужно. При `unknown` или пометке «не подтверждено» следуйте инструкции ниже.

UI начинает обслуживать HTTP до открытия вкладки: Orca может ждать загрузки страницы
перед ответом на `tab create`. Ошибка `runtime_unavailable` также может означать обрыв
конкретного RPC, а не остановку всего приложения. `orca-ide repo list --json` на Linux
проверяет связь с runtime, но не проверяет открытие браузера.
Когда Orca отвечает, напечатанный URL UI можно открыть вручную. Ошибка
`orchestration check` в состоянии задачи сохраняется после остановки runner;
при выключенном автовосстановлении восстановление Orca само по себе задачу не возобновляет. Для существующей попытки
используйте «Продолжить» после проверки состояния; при неизвестных операциях сначала
нужен reconcile. Не запускайте новую попытку только из-за перезапуска Orca.

API состояния возвращает `health: {state: "ok" | "error", error: null | string}`;
карточки каталогов показывают эти же `state` и `error`. Это состояние runner, не
проверка доступности Orca. Завершённые команды управления возвращают
`result: {state: "ok" | "error", error: null | string}`; `pending` ещё не означает успех.
Неподтверждённая остановка возвращает `status: "error"` с причиной и сохраняет
`stop_status: "unknown"`. Успех означает подтверждённую остановку ресурсов попытки.
Если остановка подтверждена и текущего пункта нет, «Запустить» или «Продолжить»
снимает остановку после проверки параметров; повтор несуществующей попытки не нужен.
После выполнения новых пунктов прежнее успешное финальное ревью больше не заменяет
финальную проверку обновлённой цели.

Подтверждённое закрытие проверяющего без `worker_done` возвращает замечание
координатору в той же попытке (не более двух возвратов). Выполненная реализация
сохраняется; приёмка требует нового успешного отчёта той же роли и функции.
Закрытый терминал сам по себе не означает успешную работу. При неизвестном
эффекте или неподтверждённой identity автоматического повтора нет; состояние
попытки показывает роль, Dispatch и причину блокировки.

### Для агента: запуск через CLI

UI для запуска сервиса через CLI не требуется. Все команды выполняйте от того же пользователя и с теми же `XDG_CONFIG_HOME` / `XDG_STATE_HOME`, что и UI, чтобы использовать один каталог проектов.

1. Найдите зарегистрированный проект и проверьте его сервис:

```sh
team-runner project list
team-runner service status --project PROJECT_UUID
```

2. Если ответ содержит `"state": "running"` и `"confirmed": true`, сервис уже готов. При подтверждённом `stopped` запустите его. Для операции создайте UUID, сохраните его до отправки команды и используйте именно его при повторном запросе результата:

```sh
SERVICE_REQUEST_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
printf '%s\n' "$SERVICE_REQUEST_ID"
team-runner service start --project PROJECT_UUID --request-id "$SERVICE_REQUEST_ID"
team-runner service status --project PROJECT_UUID
```

3. Критерий успешного запуска — **`state=running`, `confirmed=true` и заполненный `port`** в ответе `service status`. Проверяйте JSON: одного кода завершения команды `status` недостаточно. Сообщите пользователю ID проекта, состояние и порт. Секретный токен сервиса читать или передавать не требуется.

Если ответ на `start` потерян, сначала запросите `status` и при необходимости `reconcile`. Повтор той же команды `start` с **сохранённым UUID** возвращает прежнюю операцию и не создаёт второй сервис. Не генерируйте новый UUID для автоматического повтора после таймаута. UUID в переменной выше сохраняется только в текущем shell; для продолжения в другой сессии используйте заранее записанное значение.

Запрос «запусти сервис» выполнен после подтверждения `running`. Команды `run`, `--resume`, запуск рабочих целей и live smoke в эту инструкцию не входят. Дочерний агент активной попытки (`TEAM_RUNNER_ATTEMPT`) не может управлять жизненным циклом сервисов; это делает внешний управляющий агент или пользователь.

### Если сервис не запустился

```sh
team-runner service reconcile --project PROJECT_UUID
team-runner service status --project PROJECT_UUID
```

В UI этому соответствует кнопка **«Проверить состояние»**. `reconcile` проверяет факты; он не запускает сервис, не перезапускает задачи и не повторяет неизвестную операцию.

| Результат проверки | Что делать |
| --- | --- |
| `running`, `confirmed=true` | Сервис готов, дополнительных действий не нужно. |
| `stopped`, `confirmed=true` | Если сервис нужен, выполните новый явный `service start`. |
| `error`, `confirmed=true` | Прочитайте `last_error`, устраните причину и выполните новый явный `service start`. |
| `starting` или `stopping` | Дождитесь перехода и снова прочитайте `status`; второй запуск не отправляйте. |
| `unknown` или `confirmed=false` | Выполните `reconcile`. Если результат остаётся неподтверждённым, остановите автоматические действия и сообщите ID проекта и `last_error` для разбора. |

Например, если фиксированный порт занят, можно выбрать автоматический порт и затем явно запустить сервис:

```sh
team-runner project update --project PROJECT_UUID --service-port 0
team-runner service start --project PROJECT_UUID
```

При неподтверждённом исходе не удаляйте lock, `instance.json` или журнал операций и не убивайте процесс по сохранённому PID: так можно потерять сведения о работающем экземпляре. Переход в `unknown` не означает, что задачи остановились.

### Остановка, перезапуск и общий HTTP-сервис

```sh
team-runner service stop --project PROJECT_UUID
team-runner service restart --project PROJECT_UUID
# Общий UI на фиксированном порту, без автоматического открытия вкладки:
team-runner serve --port 8765
```

`serve --port 8765` задаёт порт общего UI; порт проекта задаётся через `project update --service-port` или карточку проекта.

Общий управляющий сервис слушает 127.0.0.1. Откройте напечатанный URL с токеном. `ui.lock` защищает только этот процесс. Host, token и Origin проверяются. Браузер обращается только к общему UI; сервисы проектов используют отдельные токены, которые не передаются браузеру и не записываются в журналы.

Каждый проект имеет отдельный Python-сервис на своём loopback-порту. Он запускается явно из карточки проекта или CLI, независимо от Orca. Закрытие браузера и завершение общего UI не останавливают сервисы и runner. Повторный запуск UI проверяет authenticated health и identity существующих экземпляров. Нет автозапуска при входе в систему или автоматического возобновления целей.

Stop дожидается завершения принятых HTTP-запросов и останавливает только сервис проекта. Runner-процессы и их задачи продолжаются. Restart создаёт новый экземпляр лишь после подтверждённой остановки прежнего. CLI-команды задач по-прежнему работают напрямую с выбранным workspace.

## Подключение проекта

```sh
team-runner project add --name Example --profile-file /path/profile.json --service-port 0
team-runner project list
team-runner workspace add --project PROJECT_UUID --path /existing/git/checkout
team-runner workspace list --project PROJECT_UUID
team-runner workspace update --project PROJECT_UUID --workspace WORKSPACE_UUID --profile-file /path/profile.json
team-runner workspace disable --project PROJECT_UUID --workspace WORKSPACE_UUID
```

Адрес Orca разрешается по точному пути; при нескольких адресах нужны `--orca-id` и `--orca-instance` из `orca worktree show`. Проверяются Git root, canonical path, host=local и incarnation. Смена пути — новое подключение. Disable запрещён при active/unknown; история сохраняется. Те же действия доступны в панели.

`project update --project ID --name NAME|--profile-file FILE|--roles-file FILE|--service-port PORT` меняет проектные defaults; `workspace update` — overrides каталога. `service_port=0` выбирает свободный порт через bind; `1..65535` задаёт фиксированный порт. Занятый фиксированный порт даёт ошибку, без подмены. Изменение порта применяется при следующем запуске сервиса. Старые записи каталога без `service_port` используют `0`.

Карточки показывают сервис, его порт, ошибку и сводку каждого workspace: цель, пункт, прогресс и ожидание ответа. При недоступном сервисе сохранённое состояние помечается как неподтверждённое; это не означает остановку runner. Управление задачами и их настройками в панели отключается, регистрация каталогов и настройка порта остаются доступны. Выбор workspace и введённые ответы сохраняются при обновлении карточек.

Профиль:

```json
{
  "instructions": ["AGENTS.md"],
  "requirements": ["docs/requirements.md"],
  "tracking_root": "feat",
  "evidence_dir": "evidence",
  "acceptance": {"require_commit": false, "require_clean": false},
  "checks": [{"cwd": ".", "argv": ["python3", "-m", "unittest", "discover"]}],
  "workflow": {"allow_commits": false, "sequential_agents": true},
  "constraints": ["Preserve existing edits"]
}
```

Instructions/requirements — локальные относительные или абсолютные пути. Их текст и SHA256, профиль, точная identity checkout/Orca и копия общих материалов фиксируются в `attempts/UUID/context.json` и `materials/`. Настройки ролей — `config.json`. Изменения применяются к следующим попыткам. `checks` — команды, обязательные для координатора/тестера; HTTP сам произвольные команды не выполняет.

Приоритет ролей: package defaults → project → workspace → сохранённые настройки каталога/`--config` → явные CLI overrides координатора → snapshot. Role override-файл — объект `{"tester":{"model":"..."}}`. Полный `--config` — `{"version":1,"roles":{...}}`. Secrets и содержимое provider credentials не копируются.

### OpenCode 2 и Cloud.ru

Для checkout внутри `~/repo/sber` в настройках роли доступен `opencode2`: provider `cloudru`, модель из локального `$XDG_CONFIG_HOME/opencode/opencode.json` (по умолчанию `~/.config/opencode/opencode.json`). Используется список `providers.cloudru.models`; адрес подключения остаётся в OpenCode. CLI должен быть установлен как `opencode2`. Profile пустой; effort передаётся через вариант модели и `settings.reasoningEffort`. Старый `default` остаётся читаемым.

Встроенные редактируемые шаблоны: «Текущие агенты» (`current`), «Cloud.ru» (`cloudru`) и «OpenAI-only» (`openai-only`). UI заполняет черновик, сохранение действует на следующие попытки. CLI: `team-runner template list|show|apply [NAME] --project PROJECT_UUID --workspace WORKSPACE_UUID`. Снимки текущих попыток не переписываются.

`openai-only`: все роли — Codex через `codex-lb`; координатор — `gpt-6.1-sol / medium`, критический ревьюер и эксперт эскалации — `gpt-6.1-sol / high`. Остальные роли и пустые резервы — как в `current`.

Конфигурация v2 содержит `template`, `roles`, `reserves`; v1 читается, старые обновления не стирают резервы. Добавлен `implementer_medium`: выбор реализации внутри утверждённого контракта. Полная таблица моделей и ролей — [MODELS.md](src/team_runner/MODELS.md).

Резерв: `team-runner launch ROLE --variant reserve --reason "причина" --spec /absolute/spec.md --title 'Replacement'`. Только настройки снимка и свежая сессия; предыдущий исполнитель должен завершиться или остановиться, неизвестные операции требуют reconcile. Автоматического fallback нет.

`CLOUD_API` читается без shell из `/home/talonmortem/repo/sber/AI/.env`, проверяется до запуска и передаётся только окружению дочернего OpenCode перед exec. Смена ключа/effort запрещает reuse. Запрошенный effort и подтверждённые факты показаны отдельно; наличие параметра в API JSON не доказывает его применение провайдером.

После остановки прежней попытки: сохраните шаблон, включите **«Обновить настройки при повторе»**,
нажмите **«Подготовить повтор»**, затем **«Продолжить»**. Обновление через UI использует сохранённые
настройки ролей без старых CLI overrides координатора. CLI `retry --refresh` сохраняет явные overrides.

Проверяется канонический путь checkout: похожий префикс `~/repo/sber-other` и ссылка из `sber` наружу не дают доступ. Ограничение действует в настройках, наследовании и перед dispatch, включая сохранённые snapshots. Новая сессия запускается через `opencode2 --standalone`; модель фиксируется через `OPENCODE_CONFIG_CONTENT`, вложенные агенты отключены. Сохранённые overrides Codex/Claude не меняются. Изменения настроек применяются к следующим попыткам.

## Управление

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

Для новой выборки пунктов той же цели после принятия предыдущей очереди:

```sh
team-runner prepare-scope --project PROJECT_UUID --workspace WORKSPACE_UUID \
  --generation OBSERVED_GENERATION --epoch OBSERVED_EPOCH \
  --request-id REQUEST_UUID --items 'FEATURE/STEP-1,FEATURE/STEP-2' --json
```

Команда архивирует прежнее разрешение в `goal-authorizations/`, сохраняет историю,
создаёт новую generation **на паузе** и возвращает её ID/epoch. Она не запускает
сервис, runner или агентов. Активная попытка, неизвестные эффекты, непринятые пункты
предыдущей выборки и устаревшие generation/epoch запрещают замену. Повтор с тем же
UUID возвращает прежнюю квитанцию; не возобновляет уже остановленную generation.
Настройки ролей, режим, final и recovery сохраняются; меняется только `items`.
Отдельный явный запуск: `execute --resume --generation NEW_GENERATION --epoch 1
--request-id NEW_REQUEST_UUID` с теми же project/workspace.

Дочерние роли получают TEAM_RUNNER_PROJECT/WORKSPACE/STATE/CHECKOUT/ATTEMPT/MATERIALS. В этом контексте ID можно не повторять; конфликт explicit IDs с окружением отклоняется:

```sh
team-runner launch tester --spec /absolute/spec.md --title 'Checks' --function check-one --keep-session
team-runner messages
team-runner launch tester --spec /absolute/next.md --title 'Follow-up' --function check-one --reuse DISPATCH_ID --context-percent 25
```

По умолчанию runner закрывает дочернюю сессию сразу после принятого Orca `worker_done`, включая отчёт с ошибкой. Для запланированного reuse запустите роль с `--keep-session`; она всё равно закрывается при завершении пункта. Координатор закрывается после принятого отчёта, даже если проверка пункта не прошла. Таймаут чтения mailbox повторяет чтение, не создавая новых агентов.

Reuse только implementer_standard/medium/complex и tester/tester_complex, внутри одной попытки, роли, функции, неизменных параметров и connection fingerprint. Прежний Dispatch должен завершиться, терминал — иметь прежний handle/incarnation/owner. Контекст 0..50% должен быть измерен, неизвестный/NaN запрещён. Отказ не создаёт новую сессию. Reviewer всегда свежий.

## Хранение и восстановление

В рабочем poll runner проверяет панели своих живых агентов. При `orphaned=true`
и подключённом доступном для записи терминале выполняет один `terminal switch`
на терминал и подтверждает привязку через `terminal show`. Восстановление может
переключить активную вкладку Orca. Handle, incarnation, workspace и владелец Dispatch
должны совпадать; закрытые пользователем и завершённые терминалы не восстанавливаются.
`panel_status`/`panel_error` сохраняются в `agents.json`, эффект — отдельно в
`panel-operations.json`. Потерянный ответ или повторный отрыв панели требуют ручной
проверки: команда автоматически не повторяется. `reconcile` остаётся читающим.

### Очистка временных данных старых попыток

Из корня репозитория (использует текущую версию исходников):

```sh
PYTHONPATH=src .venv/bin/team-runner cleanup --project PROJECT_UUID --workspace WORKSPACE_UUID
PYTHONPATH=src .venv/bin/team-runner cleanup --project PROJECT_UUID --workspace WORKSPACE_UUID --apply
```

Без `--apply` команда только показывает кандидатов и причины исключения. По умолчанию
сохраняются данные, изменённые за последние 7 дней; `--days 1` оставляет сутки.
Удаляются только распознанные изолированные PostgreSQL-кластеры, `node_modules`
рядом с `package.json` и Go build caches с официальным README внутри старых попыток.
Исходники тестовых копий, отчёты, логи, скриншоты, manifests, snapshots, tracking,
operation receipts и общий кеш проекта сохраняются. Каталоги с вложенными логами
или evidence также исключены. Проверки, которым нужны удалённые зависимости/БД,
потребуют их повторного создания; сохранённый PASS не означает наличия среды.

Текущая попытка (включая failed/paused), источник подготовленного retry, `.keep`
в каталоге, открытые агенты/mailbox, неизвестные операции и неподтверждённая
остановка исключаются. Требуются закрытый launch fence и подтверждённое завершение
ресурсов. Проверяются видимые ссылки процессов через Linux `/proc`, PostgreSQL PID,
symlink и mount boundaries. `/proc` некоторых привилегированных desktop-процессов
недоступен: их PID перечислены в `restricted_processes`; это ограничение наблюдения,
не полное доказательство отсутствия любых открытых файлов. Не запускайте внешние
процессы вручную в очищаемых старых каталогах одновременно с cleanup.

Журнал intent/result удаления сохраняется в `workspace/cleanup/UUID.json`.
Повтор команды повторно проверяет кандидатов; целые попытки и их история не удаляются.
Опция `--legacy-retired` явно объявляет исторические `~/.local/state/shm-team/runs`
выведенными из эксплуатации и включает те же типы данных из них. Она не нужна
для регулярной очистки Team Runner; `shm-team/cache` не затрагивается.

Для регулярной очистки используйте пользовательский systemd timer с ежечасным
вызовом этой команды, явными project/workspace и `--days 1 --apply`. Укажите
абсолютный `WorkingDirectory` репозитория и `Environment=PYTHONPATH=.../src`;
`ExecStart=.../.venv/bin/python -m team_runner cleanup ...`. Нужен отдельный timer
для каждого выбранного workspace. Ошибка блокировки означает конкурирующую операцию;
повторить после её завершения. Сервис проекта и рабочая очередь для cleanup не запускаются.

### Тяжёлые копии внутри продолжающейся попытки

Очистка старых попыток не ограничивает рост `bundle`/`copy` внутри одного длинного
пункта. Для них есть lossless content-addressed архив: одинаковые файлы сжимаются
и хранятся один раз в `workspace/artifact-store/objects`. Проверяемые manifests
в `<run>/.artifact-archives` сохраняют SHA256, пути, права и mtime. После проверки
архива освобождаются только распознанные каталоги копий/зависимостей/временной БД;
receipts, обычные отчёты, скриншоты и логи вне этих каталогов остаются на месте.
Вложенные данные тоже сохранены побайтно и доступны через restore.

```sh
# Из корня team-runner; IDs как в остальных командах.
PYTHONPATH=src .venv/bin/python -m team_runner artifacts compact --project PROJECT_UUID --workspace WORKSPACE_UUID --run /absolute/attempt/t1-runs/finished
# --apply подтверждает, что завершён review и у подпроверки больше нет читателей/писателей.
PYTHONPATH=src .venv/bin/python -m team_runner artifacts compact --project PROJECT_UUID --workspace WORKSPACE_UUID --run /absolute/attempt/t1-runs/finished --apply
PYTHONPATH=src .venv/bin/python -m team_runner artifacts restore --project PROJECT_UUID --workspace WORKSPACE_UUID --run /absolute/attempt/t1-runs/finished --tree copy --apply
```

Нужны терминальные verification receipts и доказанное закрытие ресурсов; ссылки
живых процессов, mount points, symlinks и `.keep` блокируют архивирование. PASS
не требуется: FAIL с чистыми ресурсами тоже можно архивировать, сохраняя причину.
Restore проверяет manifest и все объекты до записи, восстанавливает только
отсутствующее дерево; существующее не перезаписывает. Не изменяйте object store
вручную. Незавершённое удаление (`deleting`) продолжает тот же compact только после
проверки исходного manifest, всех архивных объектов и оставшихся файлов; новый
архив поверх частичного дерева не создаётся. Возврат к старому исполнению или переигрывание эффектов
restore не разрешает; переиспользование PASS всё ещё требует verification reuse.

`artifacts sweep --apply` архивирует подпроверки только полностью закрытых попыток.
`--attempt /absolute/attempt` — явное освобождение завершённых подпроверок текущей
попытки её координатором; выполняется между шагами, после чтения результатов.
Для дочернего агента разрешена только его собственная попытка.

Все новые тяжёлые подготовки SHM должны проходить через `artifacts prepare`:
точный argv preparer сохранить JSON-массивом в файле, затем вызвать
`artifacts prepare --attempt "$TEAM_RUNNER_ATTEMPT" --argv-file FILE --apply`.
CLI тот же (`PYTHONPATH=/absolute/team-runner/src .../.venv/bin/python -m team_runner`);
project/workspace наследуются агентом. Перед запуском выполняется sweep; затем
проверяются резерв 25 GiB, ожидаемый пик 12 GiB и лимит резидентных копий 40 GiB
(`--reserve-gib`, `--required-gib`, `--limit-gib`). При нехватке preparer не стартует.
Это проверка допуска и сериализация подготовок, а не файловая квота: команду,
обходящую helper, она не ограничивает. После review результата вызвать sweep снова;
не копить десятки копий до окончательной приёмки всего POA-02.

Пользовательский timer SHM каждый час пробует cleanup и artifacts sweep под
`flock --nonblock --conflict-exit-code 0 <workspace>/runner.lock`. Пока runner работает,
фоновая очистка пропускается: активную попытку обслуживает явный prepare/sweep
координатора. Так timer не конкурирует за archive store с подготовкой.
Проверки archive writers внутри активной попытки тоже выполняются последовательно.

### Состояние и восстановление

Автовосстановление работает в сервисе проекта при закрытом UI, без LLM-воркера и отдельной БД.
Переключатель «Автовосстановление» относится к workspace, отдельно от шаблонов моделей.
CLI: `team-runner recovery status|on|off --project PROJECT_UUID --workspace WORKSPACE_UUID`.
Для новых запусков оно включено по умолчанию; ранее существующие цели требуют явного включения.
Пауза и остановка сохраняют намерение пользователя и запрещают автоматическое продолжение.
Ранее запрошенную остановку сервис может завершить после сверки доказательств.

Состояния: «Наблюдение», «Восстановление», «Ожидание проверки», «Нужно действие», «Выключено».
Настройка `recovery-settings.json` хранится в namespace состояния workspace.
Журнал `recovery.json` хранит инцидент, попытку, причину, доказательства, действие,
число проверок и следующую проверку. Интервалы: 2, 5, 15, 30, 60, 120 секунд;
после исчерпания требуется действие пользователя. Перезапуск сервиса не сбрасывает бюджет.
Неизвестные эффекты сверяются по сохранённому request ID; `pending` и `absent` не повторяются.
Чужой incarnation, неизвестная живость и отсутствие активности не доказывают смерть.
Исчезнувший локальный терминал считается остановленным только при совпадении владельца,
incarnation и записи запуска, подтверждённом завершении исходного процесса и полном
списке терминалов без исходного handle/PTY. Новые записи сохраняют идентификатор загрузки ОС.
Отсутствие этих доказательств или неизвестная операция сохраняют блокировку.
Подтверждённая ручная остановка убирает старую ошибку из текущего статуса; журнал остаётся.
C12, провал приёмки и вопросы пользователю не вызывают автоматический retry.
Повтор после подтверждённой смерти координатора сохраняет снимок и автоматический предел трёх попыток.
После подтверждённой остановки пользователь может нажать «Разрешить ещё одну попытку»
или выполнить `retry --allow-extra-attempt`. Разрешение действует на один ручной повтор;
счётчик и история сохраняются. Затем нажмите «Продолжить». Неизвестные операции блокируют повтор.
Живой runner получает техническую сверку через свою очередь команд. Уже работающий
runner старой версии не заменяется при обновлении пакета: при техническом сбое
панель сообщает, что требуется его подтверждённое завершение. После него восстановление
использует установленную версию. Здоровая текущая попытка продолжает работу.
Запуск сервиса при входе в систему не устанавливается.

Конфигурация: `$XDG_CONFIG_HOME/team-runner` (default `~/.config/team-runner`). Состояние: `$XDG_STATE_HOME/team-runner` (default `~/.local/state/team-runner`). Namespace: `projects/PROJECT_UUID/workspaces/WORKSPACE_UUID`. Lock и durable lease по SHA256 canonical checkout предотвращают второй runner даже через другой project или symlink. Unknown не освобождает владение.

Один отдельный процесс runner потребляет mailbox своего каталога. HTTP только читает проекции и пишет UUID-команды. Повтор UUID возвращает прежний результат. Effects записывают intent до Orca и receipt после; неизвестный исход требует reconcile, не автоматического replay. `continue-operation` повторяет только записанный request ID со статусом pending/completed. Отсутствие процесса или receipt не доказывает безопасную повторную отправку. Чужой incarnation блокирует действия.

Сервис хранит `projects/PROJECT_UUID/service/instance.json`, lifetime `instance.lock`, журнал UUID-операций `commands/` и одноразовые nonce claims. Каталог сервиса доступен только владельцу (`0700`), JSON-файлы — `0600`. Запись экземпляра содержит nonce, PID, process stamp, порт, секретный токен, состояние и последнюю ошибку. История попыток не мигрирует.

Состояния: `stopped`, `starting`, `running`, `stopping`, `error`, `unknown`. Intent сохраняется до создания процесса; таймаут не запускает второй экземпляр. При `unknown` используйте `service reconcile`: он только проверяет факты, не повторяет запуск, POST или незавершённые этапы restart. Один PID или открытый порт не подтверждает identity. Если запуск потерян до сохранения PID и ребёнок не опубликовал readiness, состояние остаётся неподтверждённым и новый запуск блокируется; истечение времени не является доказательством остановки. Недоступный процесс не убивается по сохранённому PID. Проверка process stamp использует `/proc`; при недостаточных доказательствах остановка после аварии остаётся `unknown`.

API: `/api/projects`, `/api/projects/PROJECT/workspaces`, `/api/projects/PROJECT/workspaces/WORKSPACE/{state,config,profile,recovery,start,pause,stop,retry,reply,resolve,reconcile,continue-operation,disable}`. Глобального current project и глобальных управляющих endpoints нет.

API сервисов общего UI: `GET /api/services`, `GET /api/projects/PROJECT/service`, `POST /api/projects/PROJECT/service/{start,stop,restart,reconcile}`. Все service POST требуют `{"request_id":"UUID"}`. Workspace URL сохранены; управление и настройки перенаправляются соответствующему сервису, каталог редактируется общим процессом. Сервис принимает только свой project/workspace; отдельного веб-интерфейса нет. Ошибка связи не вызывает автоматический запуск или повтор POST; сетевые ожидания ограничены, проекты обслуживаются независимо.

## Проверки

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_project_services.py'
# Legacy helper scripts содержат собственные assertions:
PYTHONPATH=src python3 tests/test_orca_msg.py
PYTHONPATH=src python3 tests/test_orca_recover.py
PYTHONPATH=src python3 tests/test_token_usage.py
PYTHONPATH=src python3 tests/runner_browser_check.py --evidence /tmp/team-runner-browser
.venv/bin/python tests/runner_live_smoke.py --runtime codex --state /tmp/fresh-smoke --checkout /path/repo --orca-id 'exact-id'
```

Browser использует два временных проекта, реальные HTTP/сервисы/runner-процессы и fake Orca. Проверяет карточки, независимые порты, остановку и рестарт сервиса без потери runner, рестарт общего UI, ответы и запоздалые ответы другого workspace. Live smoke требует отдельного явного разрешения; запускает только безвредное задание и continuation. При лимите провайдера или неизвестном контексте результат INCOMPLETE. Рабочую очередь тесты не запускают.

SHM migration inventory: `migration/source-manifest.json`. Старое состояние архивируется вне Git с checksum/read-only permissions, не импортируется как исполняемая попытка. Бизнес-правила и исторические отчёты остаются в SHM.

### Token speed

В карточках агентов UI и `team-runner status` (`resources.agents[].token_speed`)
для OpenCode показываются последний замер и средневзвешенная скорость ответов:
`(output + reasoning tokens) / сумма длительностей ответов` по локальным меткам
`time.created` → `time.completed`. Это эффективная скорость с ожиданием API,
не чистая скорость декодирования; паузы между ответами не входят.
Есть разбивка по записанным в ответах модели/варианту effort, числа токенов,
ответов и обрывов `length`. Незавершённые/ошибочные ответы и неполные замеры
исключаются, отсутствие данных показывается как неизвестное, не как ноль.
SQLite OpenCode читается только для чтения; привязка требует точного checkout
и внедрённого preamble с Task, Dispatch и terminal. Следующий Dispatch в той же
сессии не включается. Для других runtime скорость пока неизвестна.

При подтверждённом `terminal_handle_stale` основного mailbox runner создаёт
новый mailbox и привязывает его к прежнему Run. Снимок попытки, Dispatch агентов
и очередь сообщений сохраняются. Чужой владелец Run, несовпадение терминала
или неизвестная операция блокируют восстановление до проверки; повторный
`worker_done` не требуется.

## Verification scope and coordinator events

Profile checks may set `scope: "item"` or `scope: "stage"`. Omitted scope retains legacy behavior.
A stage-ending tracking item sets `check_scope: "stage"`, `gate_roots`, `gate_command`, and depends on
all working items of that stage. `gate_evidence` contains checkout-relative `path` (verification result),
`manifest`, `sha256`, plus an absolute unused `accepted` receipt path in the current attempt.
Runner checks the live candidate, child results and resource settlement before writing that receipt.
Later stages may change the tree without reopening an already accepted boundary.

`team-runner events list` reads pending events in the inherited attempt. After processing, the exact
coordinator acknowledges IDs with `team-runner events ack --dispatch DISPATCH --ids ID,ID`.
Message reads and mailbox ACKs do not acknowledge processing. The runner sends one journaled message
to the exact live coordinator's Dispatch mailbox; Orca schedules delivery even when activity is unknown
or the agent is working. A queued message does not prove a started turn or processing. Unknown sends are only reconciled through
request receipts; no automatic retransmission. Events older than five minutes appear in the UI and
raise a desktop notification. Pause/stop and unknown identity prevent automatic notification.

Create a frozen manifest with `team-runner manifest create --checkout PATH --root backend --root frontend
--root scripts --root config --manifest FILE`. Save its JSON receipt; feed the returned digest
programmatically to `team-runner manifest verify --checkout PATH --manifest FILE --sha256 DIGEST
--copy TEST_CHECKOUT --output PREFLIGHT_JSON`. Copies need their Git inventory; additions, removals,
content/mode changes and paths outside the checkout fail validation. Never count or transcribe hashes.

Use `team-runner verification run --checkout PATH --manifest FILE --sha256 DIGEST --known-scope
--toolchain VERSION --environment JSON --output RESULT -- COMMAND ARGS`. Store attempt results under
`$TEAM_RUNNER_ATTEMPT/verification/`. This runs preflight before execution and postflight afterwards,
records immutable results/logs, tool binaries and harness hashes, and uses a controlled environment
(no ambient credentials/flags). Supply all required non-secret parameters explicitly. Unknown
input dependencies must not be declared `--known-scope`.

`verification reuse` takes the same inputs plus `--source OLD_RESULT` and a fresh `--output`; only a
matching PASS with intact logs qualifies. It writes a new applicability record, never imports old
Dispatches or executable operations. Full gates must cover all executable code, tests, dependencies
and checking configuration. Review/fix blocker/high findings before expensive checks. Diagnose FAIL
and fix the candidate before another full run.

`verification run --heavy` serializes heavy DB/mutation/review probes across workspaces of this execution
user. Linux child-subreaper ownership tracks detached descendants; leftover owned processes fence the
next run. Harnesses may use `team_runner.heavy_resource.acquire()` directly and register their ports.
Acquire before creating PostgreSQL; never nest the same lease. A crash with incomplete cleanup proof
requires inspection; a released file lock alone does not establish cleanup. Queue wait and execution
are measured separately. SHM starts with four DB shards and its retained duration log.

При `workflow.sequential_agents=true` координатор запускает не более одного активного worker: следующий dispatch разрешён после подтверждённого завершения или остановки предыдущего. Политика фиксируется в context.json попытки; для старых снимков без поля поведение не меняется. Все роли используют текущий checkout и установленные зависимости; отдельные окружения, копии toolchain/node_modules и реестр сред не требуются.

### Shared Go cache and compact completion

Optional profile `runtime.go_cache` is an absolute dedicated cache directory. The frozen attempt profile supplies both `GOCACHE` and Codex writable roots. Runner probes write access before dispatch; existing snapshots remain unchanged. Set this to the same Go build cache for sequential workers, then refresh the next attempt.

Workers use `python3 -m team_runner.worker_report --help`: a short JSON result generates report metadata and sends `worker_done` using the exact assigned lifecycle IDs. Detailed evidence stays in linked files. Completion receipts prevent duplicate sends; unknown effects require reconciliation. `orca_msg send --body-file FILE` transports arbitrary message text without shell evaluation.
