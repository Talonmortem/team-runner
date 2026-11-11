# Team Runner

Источник требований — план пользователя от 2026-10-08. Перенос актуального team из SHM в /home/talonmortem/repo/team-runner: Python 3.11+, stdlib, отдельный virtualenv, loopback UI и CLI.

Последовательность: baseline и перечень → пакет/контексты/каталог → адаптеры и scoped UI/CLI → fixtures и live smoke → проверка отсутствия активных/unknown попыток → неизменяемый архив вне Git → регистрация SHM без запуска → удаление общей реализации из SHM.

Приёмка: два временных проекта и несколько checkout изолированы, канонический checkout защищён от двойного runner, snapshots фиксируют инструкции и профиль; сохранены crash/reconcile/reuse и идемпотентность. Проверки UI, Codex/Claude first+reuse+cleanup, go test ./..., frontend build. Provider limit оставляет проверку незавершённой. SHM бизнес-правила, исторические отчёты и пользовательские изменения сохраняются.

## Приёмка 2026-10-08

Выполнено. Пакет установлен в `/home/talonmortem/repo/team-runner/.venv`; UI запускается `.venv/bin/team-runner ui`. Регистрация/архив/authority — `evidence/cutover.json`; browser и smoke receipts — `evidence/*.json`; screenshot установленной панели — `evidence/shm-platform.png`. 53 unittest, самостоятельные helper-регрессии, браузер двух временных проектов, оба provider smoke, Go/build — PASS. Временные sber fixtures не обращались к реальным sber-каталогам. Старые пользовательские терминалы не останавливались; working queue не запускалась.
