# Автономный Team Runner с понятным UI и интерфейсом для агентов

Дата: 2026-10-17. Только подготовка. Код не меняется; goal не активирован.

## Цель и остановка

После одного явного запуска Team Runner автономно доводит разрешённую цель до проверенного завершения либо конкретной блокировки; человек и внешний агент используют одинаковый контракт, UI показывает только необходимые действия.

Все пять этапов приняты: изолированный CLI/UI запуск требует одного действия; очередь и финальная приёмка завершаются; сбои до/между/во время попыток и перезапуск сервиса не дают дублей; pause/stop и unknown соблюдены; агент получает стабильный JSON и результат по ID; UI проверен на desktop/mobile/клавиатуре; документация и доказательства соответствуют реализации.

## Основание в текущем коде

- cli.py:main вызывает start_runner напрямую; project_services.py:run_service отдельно запускает Supervisor.
- recovery.py:Supervisor.tick прекращает работу при отсутствии current; нужны сценарии до первого и между пунктами.
- Общая логика распределена между engine.py, service.py и HTTP runner_ui.py; CLI/HTTP строят разные status.
- submit_control поддерживает request_id, но cmd_control не передаёт его из CLI.
- runner-ui/index.html ставит сервисы и большую конфигурацию перед целью; app.js выводит технические данные рядом с основными действиями.
- Уже существуют durable journals, identity checks, bounded recovery, immutable snapshots и изоляция workspace.
  Их следует расширить точечно, а не реализовать второй параллельный механизм.

Исторические треки automatic-recovery/project-services/team-templates остаются неизменными.
В goal-runner-ui осталась историческая live-проверка с provider limit: новая цель её не закрывает и не требует live запуска.
feat/process-improvements/plan.md — отдельная работа про manifest/постановку; в эту цель не включена.

## Границы

В scope: общий application contract, durable intent цели, единый execute, supervisor lifecycle,
восстановление до/между попытками, JSON CLI для агента и новый UI поверх тех же действий.
Вне scope: создание checkout/веток, автоматические коммиты/деплой, переключение провайдеров,
замена Orca, MCP server, общая система DAG, database migration, новая библиотека UI, OS autostart после reboot.
Подготовка не даёт разрешения на запуск этой или другой зарегистрированной цели.

## Архитектура и владельцы

CLI / HTTP → общий прикладной слой команд и проекций → persistent intent + operation journal.
Manager обеспечивает жизненный цикл project service в рамках сохранённого разрешения.
Project service / supervisor наблюдает намерение и факты; Runner исполняет очередь и принимает результаты.
Launcher / operations выполняет внешние эффекты с прежними identity/receipt guards.
UI отображает общий summary и available_actions; не вычисляет свою машину состояний.

Один владелец записи каждого состояния и один runner на checkout. Зафиксировать порядок locks до
реализации manager/controller; не держать общий catalog lock во время ожидания RPC.
Сетевые ошибки и неизвестные effects не равны отсутствию ресурса. Статус сервиса не равен статусу цели.

## Общие контракты v1

- Scope: project_id, workspace_id, checkout identity; для действий над исполнением — goal generation и attempt identity.
- Goal intent: schema_version, goal, generation, desired_state running/paused/stopped,
  policy (mode, final review, recovery, пределы), originating request_id. Persist до эффектов.
- Operation: request_id, action, scope/target, канонический payload, status pending/running/succeeded/failed/unknown,
  result/error. Один ID с другим payload — conflict. Терминальные результаты неизменны.
- Summary: schema_version, identity, revision, desired_state, observed_state,
  confirmed, current item/attempt, progress, final_acceptance, blocker, available_actions, recovery/next_check.
- Blocker: стабильный code, человекочитаемая причина, факты, конкретные допустимые действия.
- Ошибка: code, message, details, retryable. retryable не разрешает replay unknown и не создаёт новый UUID.
- Completion операции execute означает принятие/обеспечение запуска; completion цели отдельное наблюдаемое состояние.
  Wait должен явно выбирать, ждёт ли он завершения операции или цели. Приёмка не выводится из exit 0 команды start.
- Привязка к revision/generation не позволяет delayed request управлять новой попыткой. Backend повторно проверяет guards.
- Read-only status, список goals, operation lookup и messages не запускают процессы и не читают Orca mailbox.
  Только runner consumes mailbox; читатели получают persisted сообщения с scoped cursor.

Поля/названия зафиксировать contract fixtures первого этапа; единый adapter преобразует legacy записи.
Не менять старые snapshots; не объявлять legacy данные соответствующими новому протоколу без доказательств.
Не обещать атомарность нескольких JSON-файлов: переходы записывать с recoverable phases и проверять crash boundaries.
Сначала durable intention → проверка фактов/identity → допустимый effect → receipt/result → projection.

## Политика автономности

Новый явный autonomous execute по умолчанию проходит очередь автоматически, включает финальную проверку
и существующее ограниченное восстановление. Старые mode/final/recovery при resume сохраняются.
Режим confirm остаётся доступен в настройках, его gate не обходится автоматически.
Автономность заканчивается конкретным needs_action при нехватке разрешений, пользовательском вопросе,
неразрешённом unknown или исчерпании бюджета попыток. Максимум существующих попыток не повышается.
Успешное завершение: все обязательные пункты и актуальная финальная приёмка; отсутствие next_item недостаточно,
если остались заблокированные/пропущенные пункты или несоблюдённые зависимости.

Supervisor технически восстанавливает разрешённое исполнение; не принимает работу, не утверждает retry/rework
вместо runner и не отвечает на вопросы пользователя. Дочерние агенты не получают права на lifecycle целей/сервисов.
Pause после пункта и stop имеют приоритет, включая конкуренцию с backoff и эффектом запуска.
Ручной service stop сохраняется; обычный просмотр проекта не поднимает остановленный сервис.

## UI: целевой сценарий

Выбор проекта/workspace → цель → «Выполнить» → прогресс/агенты → результат или конкретный запрос решения.
Рабочий экран не требует управления портами/сервисами или знания request/dispatch ID.
Видимые основные действия определяет backend. Настройки и диагностика раскрываются отдельно.
Необходимые recover/retry/extra attempt не удаляются из системы, а показываются в контексте причины.
Отдельный этап UI содержит макет, доступность, responsive размеры, состояния и браузерные критерии.

## Порядок реализации

1. contracts — общий слой, статусы/ошибки/операции, compatibility fixtures.
2. autonomy — сохранённое намерение, единый запуск, controller и service lifecycle.
3. agent-interface — execute/wait/status/messages и headless сценарий.
4. ui — рабочий экран, контекстные действия, адаптивность и browser fixtures.
5. acceptance — сквозные сбои, итоговая документация и evidence.

Каждый этап имеет вложенные Tasks.json, Progress.txt и план. Новые пункты not_started.
Реализовывать малыми изменениями, проверять узко на каждом этапе; полный unittest только на финальной интеграции.

## Миграция и rollout

Отсутствие нового intent у старой попытки не является разрешением её запускать. Создание/конвертация intent
только при явном execute/resume или доказанном прежнем разрешении с совместимой политикой.
Новые поля versioned; непонятная версия останавливает mutations с actionable error.
Старые публичные команды/endpoints сохранить через адаптеры либо документированный явный переход;
никогда не менять смысл stop/retry/resume тихо. Проверить возврат старой версии: она не должна запускать
непонятное состояние; если безопасный downgrade невозможен, документировать ограничение до rollout.
Установка пакета, перезапуск реальных сервисов и изменение зарегистрированных настроек не входят в исполнение goal.

## Доказательства и тесты

Каждый этап пишет evidence в своём каталоге; каждый completed пункт имеет acceptance.md по настроенному профилю.
Корневой evidence/goal-final-review.md сопоставляет критерии остановки с командами/результатами и screenshots.
Только временные checkout, fake Orca, изолированные XDG roots и локальные HTTP/subprocess/browser fixtures.
Для внедрения service manager обязательны fake clock, fault injection, проверка числа эффектов и cleanup процессов.
Повторные запуски тестов только после изменения, сбоя или нерешённого риска.
Browser evidence: desktop/mobile, idle/running/needs_action/offline, реальные видимые клики и клавиатура.
Не считать direct JS click скрытых кнопок проверкой удобства интерфейса.

## Риски и допущения

- Manager service watchdog добавляет процесс-владелец: не смешивать UI uptime с восстановлением цели.
- История множества файлов может давать torn observations: summary должен явно отражать revision/confirmed.
- Автоматический режим не отменяет разрешения и не даёт права менять models/профили в активном snapshot.
- Удобство UI не должно скрыть unknown или потерять черновики при polling.
- Нет гарантии возобновления после выключения машины без запуска manager; OS autostart вне scope.
- Ключевые contracts сначала проверяются на fixtures; live результаты этой целью не заявляются.
