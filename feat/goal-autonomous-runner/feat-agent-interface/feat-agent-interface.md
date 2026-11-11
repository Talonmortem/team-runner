# Интерфейс для управляющих агентов

Дата: 2026-10-17. Реализация не начата.

Общие контракты: [goal-autonomous-runner.md](../goal-autonomous-runner.md).
Зависимости: contracts, autonomy.

## Текущее состояние и область
service CLI имеет --request-id, остальные control-команды его не передают в существующий submit_control.
status выдаёт большой JSON; messages возвращает текущие сохранённые сообщения целиком; ошибки CLI печатаются строкой.
Область: cli.py, общий прикладной слой, engine.py и документация инструкций агентам.

## Контракт команд (зафиксировать точный синтаксис до реализации)
- team-runner execute --project P --workspace W --goal PATH --request-id UUID --json.
- team-runner operation show|wait --project P --workspace W --request-id UUID [--until operation|goal] [--timeout SEC] --json.
- team-runner status --project P --workspace W --json [--detail].
- Существующие pause/stop/reply/retry/resolve получают request_id и явную привязку к целевой generation/attempt.
- messages --after CURSOR --limit N возвращает items, next_cursor, has_more; cursor scoped к workspace/run/attempt.
Для --until goal операция execute связывает ожидание с точной goal generation; состояние новой цели не завершает старое ожидание.
Wait только читает результат и не повторяет mutation; timeout означает всё ещё pending, а не failure/cancellation.
Долгое ожидание не держит workspace lock и не блокирует stop. JSON ошибки имеют code/message/details/retryable.
Коды выхода документировать отдельно для success, invalid/conflict, blocked/unknown и wait timeout.

## Приёмка и проверки
- Агент из новой shell-сессии находит проект/workspace/goal, запускает цель, ждёт и получает итог без UI/чтения токенов/ручного JSON state.
- Повтор запроса после потерянного ответа с прежним UUID возвращает прежнюю операцию; конфликт payload отклоняется.
- Задержанный reply/stop для предыдущей попытки не применяется к новой.
- Курсор не теряет сообщения; повтор чтения детерминирован; чужой курсор отвергается. UI/CLI не consume Orca mailbox.
- stdout в JSON режиме содержит только JSON, диагностика идёт в stderr без секретов.
- Дочерний агент сохраняет allowlist launch/messages/reply/status; execute/service/control не расширяют его права.
- CLI subprocess fixtures без браузера и провайдера; parity с HTTP из contracts; timeout, pending, unknown, interrupted client.

## Документация и риски
README и goal-runner.md: короткий исполняемый пример execute→wait→status→reply, повтор по сохранённому ID.
Риск: автоматический клиент принимает exit 0 за завершение цели; критерий успеха обязан включать terminal result.
