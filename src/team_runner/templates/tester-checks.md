tester T2 — собственные проверки (параметры из снимка роли) / run {RUN} / task {ITEM} раунд {ROUND} / HEAD {HEAD} + frozen {MANIFEST}

Рабочий каталог {CHECKOUT} — только чтение исходников; production-код и тесты в checkout не менять. RUN_STATE={RUN_STATE}.
Прочитай $TEAM_RUNNER_MATERIALS/worker-brief.md и $TEAM_RUNNER_MATERIALS/defect-classes.md (C1–C12). Гейты уже прошли в T1 (RUN_STATE/reports/{ITEM}-tester-{ROUND}-gates.md) — не повторяй их.
Для complex координатор выбирает --role tester_complex: отдельная Sol-сессия после T1 PASS.
Самостоятельно спроектируй T2; матрица рисков включена в требования. Изменение входов аннулирует PASS.
Требования, матрица рисков и решения: {REQUIREMENTS}. Отчёты implementer — заявления, не доказательства.

Проверки в текущем checkout и существующем окружении через настоящий router и реальные команды. Только для мутаций используй временную копию затронутых исходников, без копирования зависимостей:
{CHECKS}
Мутации: {MUTATIONS} — baseline PASS / mutant функциональный FAIL, копию восстанавливать после каждой.
Любой дефект — FAIL с воспроизведением, без исправления production. Сомнение по бизнес-правилу — `orca orchestration ask`.
Отчёт RUN_STATE/reports/{ITEM}-tester-{ROUND}.md: формат worker, вердикт PASS/FAIL по раунду (гейты — ссылкой на T1), таблица
проверок и мутаций. Артефакты — префикс tester{ROUND}-*.
