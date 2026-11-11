# Интеграционная приёмка и документация

Дата: 2026-10-17. Реализация не начата.

Общие контракты: [goal-autonomous-runner.md](../goal-autonomous-runner.md).
Зависимости: contracts, autonomy, agent-interface, ui.

## Область
Интеграция всех предыдущих этапов; tests, README, goal-runner.md и релевантные ролевые инструкции.
Не включать независимые предложения из feat/process-improvements/plan.md и не закрывать исторические live-блокировки.

## Сквозная матрица
- Два проекта и несколько workspace: независимость состояния/портов/ошибок; один зависший RPC не блокирует остальные.
- Холодный CLI execute и UI execute: один intent, сервис и supervisor обеспечены, очередь и final review приняты.
- Два клиента с одним request_id; конфликт payload; потерянный ответ; новый клиент читает прежний результат.
- Crash после intent, во время launch, до первого current, между пунктами, после final acceptance.
- Restart UI, runner и project service; падение сервиса при живом manager; manual service stop не отменяется.
- Pause/stop vs backoff/dispatch/late reply; unknown без replay; чужой PID/incarnation; исчерпание попыток.
- Legacy snapshots/settings сохраняются; confirm не превращается в auto; failed/blocked не превращается в completed.
- UI desktop/mobile/keyboard: основной маршрут, доступность редких действий, черновики и late response isolation.

## Проверки и доказательства
На этапах — только узкие проверки. На финальной интеграции один полный прогон:
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'.
Standalone: python3 tests/test_orca_msg.py; python3 tests/test_orca_recover.py; python3 tests/test_token_usage.py.
JavaScript: node --check src/team_runner/runner-ui/app.js.
Изолированный browser fixture; никакие зарегистрированные рабочие цели и live провайдеры не используются.
Логи содержат команду, cwd, exit code, версию проверяемых входов и путь к артефактам; screenshots без секретных URL.
При отсутствии браузера UI-проверка INCOMPLETE, не PASS; завершение goal до её выполнения запрещено.

## Документация и rollout
README описывает единый запуск, дефолты новых целей, legacy resume, manager lifecycle, JSON/exit codes,
паузы, остановки, unknown и расположение диагностики. Обновить инструкции агентов теми же командами.
Записать совместимость persisted formats, возможный откат и поведение старой версии при новом состоянии.
Не устанавливать пакет в рабочее окружение, не рестартовать реальные сервисы и не менять зарегистрированные настройки.

## Приёмка
Каждый пункт root stopping condition имеет ссылку на положительный и релевантный отрицательный сценарий.
Все новые queue items completed только после локальной приёмки; итоговый отчёт evidence/goal-final-review.md
ссылается на проверки и оставшиеся ограничения. Запрет replay, permissions и старые snapshots проверены отдельно.
