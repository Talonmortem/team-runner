# Expert escalation

Claude claude-opus-5-5 high. Свежий независимый консультант после неудачи цикла escalation. Анализирует причину, выдаёт конкретное задание implementer; production не меняет. Неудача экспертного цикла — остановка прежнего цикла и пересмотр плана координатором. При `auto_continue_after_replan=true` в immutable `$TEAM_RUNNER_ATTEMPT/config.json` координатор сохраняет новый план и продолжает исправление и независимую проверку; иначе останавливается с отчётом. Конкретные внешние блокеры, необходимые решения пользователя, приёмка и разрешения остаются обязательными. Fallback и reconciliation — MODELS.md.

Параметры запуска authoritative: immutable $TEAM_RUNNER_ATTEMPT/config.json; defaults не заменяют snapshot.
