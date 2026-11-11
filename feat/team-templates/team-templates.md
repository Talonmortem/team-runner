# Шаблоны команд high/xhigh

Источник требований — план пользователя от 2026-10-13. Два встроенных редактируемых шаблона: current («Текущие агенты») и cloudru («Cloud.ru»). Выбор меняет черновик, сохранение — будущие попытки. Пользовательских именованных шаблонов нет.

Шаги: конфигурация v2/template/roles/reserves с чтением v1 и overrides; implementer_medium; снимки обоих вариантов; CLI/UI; явный резерв со свежей сессией и причиной; варианты OpenCode и reasoningEffort/thinking; CLOUD_API из Sber .env только дочернему процессу; материалы и проверки.

Приёмка: совместимость v1/v2, сохранение резервов при старом update, неизменность снимков/retry; CLI/UI draft/save/restore; reserve reason/fresh session/unknown effects; исходящий JSON пяти моделей и продолжение tool call на локальном API; смена effort/key блокирует reuse; dotenv missing/empty/quoted/export/no leaks; canonical Sber boundary/symlinks; узкие регрессии и изолированный UI.

Реальное применение effort провайдером требует отдельной живой проверки; рабочие цели не запускать.
