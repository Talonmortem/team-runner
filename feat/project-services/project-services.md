# Сервисы проектов

Один общий UI; отдельный detached Python HTTP service на проект. Workspace runner продолжает работать независимо от обоих уровней управления.

## Реализация
1. service_port (0 либо фиксированный порт), CLI service lifecycle.
2. Durable intent, nonce, process identity, lifetime lock, UUID journal; authenticated health/shutdown; conservative unknown and read-only reconcile.
3. Общий HTTP proxy без replay, project-scoped API без UI и без чужих workspace.
4. Карточки всех проектов; сохранённое неподтверждённое состояние при потере связи; сохранение выбора и черновиков.
5. Изолированные subprocess/HTTP/browser fixtures, регрессии, README.

## Приёмка
Два проекта на разных портах; concurrent/repeated start не создаёт дубликаты. Фиксированный занятый порт не заменяется. Неоднозначный запуск блокирует новый. UI restart сохраняет сервисы, service restart сохраняет runner. Чужие IDs, Host, Origin, Authorization отклоняются; service tokens отсутствуют в public JSON. Проверить crash/lost response/stale PID, checkout locks, профиль и настройки, браузерные ответы и смену проекта. Рабочие цели не запускать.
