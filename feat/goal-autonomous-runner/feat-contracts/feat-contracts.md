# Общий контракт управления

Дата: 2026-10-17. Реализация не начата.

Общие контракты: [goal-autonomous-runner.md](../goal-autonomous-runner.md).
Зависимости: нет.

## Текущее состояние и область
CLI напрямую вызывает Runner/start_runner; HTTP имеет собственные обработчики и сборку state_view.
service.start_runner и CLI template импортируют helpers из runner_ui. Новые интерфейсы не должны зависеть от HTTP-модуля.
Область: cli.py, runner_ui.py, service.py, engine.py и небольшие прикладные модули по необходимости.
Без полного переписывания Runner/Launcher, замены JSON-хранилища или абстрактного event bus.

## Шаги
1. Зафиксировать примеры v1 JSON для команды, операции, summary, блокировки и available_actions.
2. Перенести общую валидацию, поиск целей/конфига, команды и проекции в слой, доступный CLI и HTTP.
3. Оставить транспортам parsing/auth/serialization. Проверки допустимости выполняет backend даже при прямом вызове.
4. Сохранить существующие диагностические данные через detail/full и документировать адаптеры старых ответов.

## Приёмка и проверки
- CLI и HTTP для одной fixture возвращают одинаковые identity, revision, состояние, blocker и действия.
- Каждая mutation имеет request_id; тот же ID+payload возвращает ту же операцию, иной payload даёт conflict.
- Явно различены принятие команды, её исполнение и завершение цели; pending не считается выполнением.
- Read-only status/list не создаёт сервисы, runner, терминалы и намерение running.
- Неверные project/workspace/attempt и stale revision отклоняются до effects.
- Старые snapshots и команды читаются без изменения содержимого; unsupported version даёт конкретную ошибку.
- Узкие проверки: test_platform, test_project_services, test_recovery_ui_cli, test_template_ui_cli; новые contract fixtures.

## Документация и риски
Описать JSON, таблицу ошибок, совместимость и lock order в корневом плане/README.
Риск: две реализации поведения сохранятся под новым фасадом; приёмка требует одного места принятия решения.
