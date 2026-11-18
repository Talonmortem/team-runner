implementer ({PROFILE}) / run {RUN} / task {ITEM} шаг {STEP} / base SHA {BASE}

Рабочий каталог {CHECKOUT}, текущая ветка, без веток и worktree. RUN_STATE={RUN_STATE}.
Обязательно прочитай (если ещё не читал в этой сессии): $TEAM_RUNNER_MATERIALS/worker-brief.md, $TEAM_RUNNER_MATERIALS/defect-classes.md. Регламент и план целиком
не читай — всё нужное для шага ниже в карточке. Решения пользователя и координатора: {DECISIONS}.
Предыдущие шаги приняты и закоммичены: {PREV} (diff — `git show --stat`, отчёты RUN_STATE/reports/{ITEM}-impl-*.md — только если
карточка на них ссылается).

## Карточка шага {STEP}
{CARD}

## Передача
До передачи — применимые дешёвые format/lint, непустые целевые тесты, реальные fixture paths/inputs и selfcheck изменённого harness. Команды/exit codes/логи/входы приложи к отчёту. Native/heavy до обязательного source review не запускать; отложенные gates явно DEFERRED с владельцем. Исправление охватывает весь назначенный подтверждённый класс; второй возврат класса требует диагностики и изменённого плана координатора перед следующим fix.
Гейты — $TEAM_RUNNER_MATERIALS/worker-brief.md §«Гейты передачи»; timings для раннера: {TIMINGS}. Отчёт RUN_STATE/reports/{ITEM}-impl-{SLUG}.md
до 500 слов с выводами и ссылкой на полное отдельное приложение самопроверки по классам; frozen manifest RUN_STATE/manifests/{ITEM}-{SLUG}-frozen.json. Без коммита. Вопрос по
бизнес-правилу — `orca orchestration ask`, не придумывать.
