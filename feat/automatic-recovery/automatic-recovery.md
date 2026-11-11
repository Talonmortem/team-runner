# Автоматическое восстановление

Supervisor сервиса проекта независимо наблюдает рабочие области при закрытом UI.
Отдельные настройки и журнал: incident, attempt, reason, evidence, stage, checks, next_check.
Проверки через 2/5/15/30/60/120 секунд; затем требуется действие пользователя.
Восстановление текущей попытки после подтверждённой смерти runner; следующая попытка после подтверждённой смерти координатора и всех исполнителей, без неизвестных операций, с прежним snapshot и общим лимитом 3.
Пауза/остановка отменяют продолжение. Неизвестная живость, C12 и acceptance failures не дают права retry.

Приёмка: изолированные terminal stale/identity, request receipt, closed question, worker_done, process death, crash window, manual intent, attempt limit и workspace isolation проверки. Обновить .venv и затронутые HTTP сервисы; включить текущий SHM без тестового перезапуска цели.
