# Оптимизация SHM runner

Источник намерения: план пользователя от 2026-10-24, все шесть направлений обязательны.

1. Сверить основной исходник, installed и autonomous; сохранить локальные изменения и installed fix missing-terminal stop.
2. Persist pending events после question/escalation/worker_done; отдельный processed ACK; через 60 секунд одно журналируемое пробуждение точного live idle coordinator; unknown receipt только reconcile. Через 300 секунд UI/desktop warning.
3. Manifest CLI: digest, безопасные пути, source/copy hashes, additions/deletions; JSON и ненулевой exit при несовпадении.
4. item/stage checks с legacy compatibility; обязательные завершающие gates E9/E10/E11, зависимости и итоговый manifest. Review/fixes перед дорогими проверками; FAIL требует диагностики.
5. Три указанных конкурентных теста: operation/PID-specific PostgreSQL barriers, diagnostics, 10 repeats плюс bounded load; измерить setup/execution трёх длинных матриц.
6. Host flock для DB/mutation/heavy review, до PostgreSQL; 4 DB shards; separate queue/run timings, crash residue checks.
7. Evidence: PASS reuse только после совпадения всех известных inputs/toolchain/environment; новая applicability запись в каждой попытке. UI отдельные статусы и метрики.
8. Narrow regression + independent review; backup, confirmed stop, install, service/profile update, fresh attempt и resume незавершённого пункта.

Приёмка: duplicate/restart/unknown/busy/stale/pause/stop events; malformed/path-escape/changed copy manifest; mandatory gate blocks next stage; interprocess lock/crash; evidence invalidation code/tests/dependencies/environment. Старые receipts и snapshots неизменны. Решения SHM «все А» сохранены. Полный SHM suite не запускать до stage boundary.
