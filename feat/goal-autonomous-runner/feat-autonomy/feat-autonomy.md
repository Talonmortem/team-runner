# Автономное выполнение и восстановление

Дата: 2026-10-17. Реализация не начата.

Общие контракты: [goal-autonomous-runner.md](../goal-autonomous-runner.md).
Зависимости: contracts.

## Текущее состояние и область
Supervisor работает только внутри project service; CLI run не обеспечивает его наличие.
Supervisor.tick возвращается при отсутствии current. running/paused/stop_requested и recovery intent распределены по файлам.
Область: service.py, project_services.py, recovery.py, engine.py, runner_operations.py/runner_store.py только по необходимости.

## Шаги
1. Persist versioned goal intent с goal identity, generation, разрешённой политикой и request_id до effects.
2. Execute обеспечивает подтверждённый project service, затем запуск/продолжение ровно этой цели.
   Неизвестный service start сначала reconcile; новый UUID после таймаута не создаётся.
3. Controller сопоставляет intent с наблюдаемым состоянием. Runner остаётся единственным владельцем приёмки и продвижения очереди.
4. Покрыть промежутки до current и после принятия пункта; завершённую цель не запускать заново при обычном resume.
5. Восстановление использует существующие квитанции/identity, backoff и ограниченные попытки.
   Проверки permission/generation непосредственно перед effect; stop не отменяется запоздалым execute.
6. Отдельно от восстановления runner обеспечить восстановление project service после подтверждённого падения
   при живом локальном manager: ограниченный watchdog, persisted service intent и identity, без повторов unknown.
   Ручной service stop подавляет watchdog. Закрытие UI не должно отключать активный manager.
   На этапе implementation выбрать минимальный локальный процесс-владелец и зафиксировать lifecycle/lock order;
   OS autostart после перезагрузки машины вне scope. После явного запуска manager восстанавливает только ранее разрешённые intents.

## Приёмка и проверки
- Холодный execute запускает сервис и цель без ручного service start; обычный read-only запрос ничего не запускает.
- CLI/UI concurrent execute и lost response создают один run/dispatch; конфликт другой цели явно отклоняется.
- Искусственный crash после durable intent, перед current, после приёмки пункта и перед следующим dispatch не теряет продвижение.
- Падение сервиса при живом manager восстанавливает supervisor; живые runner/dispatch не дублируются.
- Восстановление не создаёт новые acceptance/rework решения: ими занимается возобновлённый runner по прежним правилам.
- Pause после пункта, stop во время ожидания/launch/backoff и ручная остановка сервиса сохраняются после рестарта.
- Unknown, чужая incarnation, исчерпание лимита, вопрос и запрещённое workflow действие дают точный blocker.
- Политика нового явного autonomous запуска: auto, финальная проверка включена, существующие лимиты.
  Старые сохранённые confirm/final/recovery настройки не меняются; продолжение использует сохранённую политику.
- Fake Orca и временные checkout: test_project_services, test_recovery, test_runner_recovery,
  test_recovery_isolation, test_stop_refresh, test_extra_attempt, test_acceptance_rework + crash-boundary cases.

## Документация и риски
Таблица переходов, владельцы записи/locks, завершение процессов, ручная диагностика manager и миграция intents.
Риски: новый владелец процессов, split brain, гонка cancellation/effect. Допуск только по доказательствам identity;
не расширять глобальную блокировку на сетевые ожидания и другие workspace.
