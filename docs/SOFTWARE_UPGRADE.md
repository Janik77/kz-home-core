# KZ Home: безопасная проверка общей версии

**Кандидат для квалификации в CI. На компьютере владельца ничего не устанавливать и не перезапускать по этой инструкции без согласованного окна.** Наличие инструкции не означает прохождение приёмки. Это программный стенд, не production и не проверка физического ESP32/реле.

## Точные версии

| Компонент | Версия |
|---|---|
| Core runtime, PR #64 | `b897b93726ef4d02e596e0b83c568df269e29bcc` |
| Web, PR #38, Android/desktop pilot | `9c2b6dbeb85de019ffb86f135378e0331512a5b9` |
| Исполняемый tooling | Следующий tooling-коммит в ветке квалификации; точный SHA фиксируется в отчёте CI |
| Схема | `0018_scene_drafts` |

Tooling проверяет равенство runtime-файлов указанному SHA. Итоговый отчёт должен отдельно назвать проверенный integration SHA и реальные результаты CI/браузера. Последующая документация не меняет исполняемый tooling.

## Поддерживаемые исходные профили

| Источник | Tooling checkout | Core runtime | Web | Схема |
|---|---|---|---|---|
| #42 | `82653a849d2b61479de1c3bb4d74e7f388523667` | `f946a07f67f9ccb1e8f62c99860a0842bf3a062b` | `c2c4fa89a0da3652e8e0cca3629300ecfd7e658f` | `0010_scene_runs` |
| #50 | `c03aa044642e8e09bf61ff51c35a46079d1118f0` | `f6a0826929c87b39dec7e81d22280f1218bf18f1` | `6f8f72f86f668b53f85c13054644c80427fe9eab` | `0013_device_automations` |
| #52 | `244549d3cd6b5b2c5b7bc9757e438d2729553155` | `1ae52afa22431943064b5e85313f671b073b9458` | `74b7238f10a9e56cbc600f9cac21b29a5c15b46e` | `0014_numeric_thresholds` |
| #54 | `2142dcd584f990031d0c02ed6b4c9f1a90d14eed` | `45b63628829580bc564ff2490af645951926e82e` | `96c953f40f3a8eff464e2b57b5e7817456569206` | `0015_house_schedules` |
| #56 | `f2ec065426e72fb60b3e944ca6fd852bdffde48a` | `af6dcadddcfc2d5bee97da4386021495a54d1f9a` | `2ccf4172579563ad5e6cb1b0f4dba4147aada558` | `0016_durable_waits` |
| #58 | `cfe6ce70ed8e2f3392567ec27a807343d5e866bc` | `fc232035f78c99171fd72ac82c78eb1b5bb657fa` | `31dd1029173af6c7ff4ed4e9bf0c80679c185b3b` | `0017_metadata_cas` |

Неподдерживаемый SHA нельзя «исправлять» вручную. Сохраняются пользователи, права, комнаты, избранное, определения, ревизии, receipts и история; numeric values/freshness/watermarks/latch; timezone, курсор и потреблённые расписания. Новые поля ожиданий не активируют старый `delay`. Физический inventory запрещён. Архивы и секреты не публикуются.

Короткий ежедневный запуск: [UNIFIED_SOFTWARE_MVP.md](UNIFIED_SOFTWARE_MVP.md). Следующие шаги выполняются один раз при согласованном обновлении, не при каждом запуске.

## 1. Проверить окружение только чтением

Используйте уже существующие согласованные checkout и изолированный Python. Переменные `CORE_SOURCE`, `WEB_SOURCE`, `CORE_TARGET`, `WEB_TARGET`, `SOURCE_PROFILE`, `BACKUP`, `NEW_PROFILE`, `PYTHON` определяются по фактической проверке, не по предположению.

Не выполнять clone, reset, stash, clean, смену checkout или установку зависимостей в используемой/грязной папке. Незакоммиченные документы и данные сохранить.

```bash
git -C "$CORE_SOURCE" rev-parse HEAD
git -C "$CORE_SOURCE" status --short
git -C "$WEB_SOURCE" rev-parse HEAD
git -C "$WEB_SOURCE" status --short
git -C "$CORE_TARGET" rev-parse HEAD
git -C "$CORE_TARGET" status --short
git -C "$WEB_TARGET" rev-parse HEAD
git -C "$WEB_TARGET" status --short
find "$CORE_TARGET" "$WEB_TARGET" -maxdepth 1 -name '.env*' -print
docker ps --format '{{.Names}} {{.Image}} {{.Command}}'
# CONTAINER выбрать из результата, не угадывать:
docker inspect "$CONTAINER" --format '{{json .Mounts}} {{json .Config.Cmd}}'
```

Проверьте active-use, bind mounts и reload. При грязном checkout, неожиданном `.env`, несовпадении SHA или неизвестном профиле остановить только подготовку. Не останавливать контейнеры и не редактировать служебные привязки. Native Windows для POSIX tooling не поддерживается: ранее квалифицированная Linux-среда только в согласованное окно, без sudo и изменений WSL/Docker.

## 2. Backup и НОВОЕ назначение

Только в согласованное окно штатно завершить исходный foreground launcher. Не угадывать PID, не перезапускать Docker/WSL, не трогать соседние проекты. Активный backup запрещён. `BACKUP` и `NEW_PROFILE` должны быть новыми путями.

```bash
"$PYTHON" "$CORE_SOURCE/tools/durable_software.py" backup --state "$SOURCE_PROFILE" --destination "$BACKUP"
"$PYTHON" "$CORE_TARGET/tools/durable_software.py" upgrade --backup "$BACKUP" --state "$NEW_PROFILE" --source-checkout "$CORE_SOURCE"
"$PYTHON" "$CORE_TARGET/tools/durable_software.py" status --state "$NEW_PROFILE"
"$PYTHON" "$CORE_TARGET/tools/durable_software.py" start --state "$NEW_PROFILE" --web-root "$WEB_TARGET"
```

Для восстановления backup той же новой версии вместо `upgrade`:

```bash
"$PYTHON" "$CORE_TARGET/tools/durable_software.py" restore --backup "$BACKUP" --state "$NEW_PROFILE"
```

`start` работает в foreground и печатает фактический локальный адрес. Публичный URL и production deploy не создаются. Проверяются clean source, manifest/hash, schema, пустая новая БД, directory/database identity и completion markers. Не редактировать `profile.json`, pins, identity или `.env`. Частичное назначение не считается готовым и не удаляется автоматически.

## 3. Ожидания: перезапуск не равен копированию

В **той же БД** подтверждённый шаг A может перейти в сохранённое ожидание и затем один раз в ещё не отправленный шаг B. Grace возобновления 30 секунд; просроченная, claimed/sent/unknown команда не повторяется.

При **backup → НОВАЯ копия** продолжение ожидания отменяется только в новом назначении с причиной `restore_invalidation`. Иначе исходник и копия могли бы отправить один и тот же следующий шаг. Уже подтверждённое действие не отменяется физически. Исходная БД и backup не изменяются; исходник сохраняет своё намерение.

Backup 0016/0017/0018 содержит fingerprints всех таблиц/колонок/строк. В одной транзакции restore сначала проверяет полное совпадение и только затем меняет лишь три поля известных parked runs: `execution_finished`, `execution_error`, `cancelled_at`. Проверяются точный ожидаемый diff и неизменность остальных данных. `restore-policy.json` содержит частный аудит; receipts, ID, сроки и checkpoints сохраняются. Повторный backup/restore не оживляет отменённое продолжение. Для новых действий пользователь должен явно запустить новое намерение.

При переходе #58 (0017) → 0018 полный manifest проверяется до DDL в той же транзакции. Единственное изменение старых строк — `scenes.is_draft=false`; версия Alembic становится 0018. Существующие metadata revisions не сбрасываются. Для #56 дополнительно допускаются только четыре прежних `metadata_revision=1`. Полное вычисленное ожидаемое состояние становится baseline restore-policy; waits, receipts и история не исключаются.

Копии сцен сохраняются как черновики и не запускаются; копии правил остаются отключёнными без скопированных полномочий/состояния. Backup/restore 0018 сохраняет `is_draft=true`, а не активирует копии. Явная подготовка сцены не отправляет команд; запуск отдельный. Downgrade при любом черновике запрещён. [Контракт сцен](SCENE_EDIT_COPY.md).

### Совместимость редактирования

Имена и переносы требуют `expected_metadata_revision` из свежего GET. Старый metadata-клиент получает 422; stale draft получает 409 `metadata_revision_conflict`, а не перезаписывает данные. Обновлять Core и Web согласованной парой. После конфликта пользователь явно сверяет черновик; автоматический повтор записи с новым токеном запрещён. Heartbeat не меняет эту revision; timezone-only использует прежний отдельный CAS. Откат схемы после использованной metadata revision запрещён, используйте сохранённый исходник/backup. Подробности: [METADATA_CAS.md](METADATA_CAS.md).

## 4. Приёмка

- Авторизация, права, черновики/CAS, комнаты и избранное сохраняются.
- Команда показывает ожидание до ACK и соответствующего state; неизвестный результат остаётся неизвестным.
- Пороговые правила и расписания не воспроизводят старые samples/пропущенные минуты.
- A → durable wait → B проверяется на программном реле; cancel/disable/revision/timezone/rights и просрочка останавливают оставшиеся шаги.
- Same-DB restart возобновляет только допустимое ожидание; новая копия не отправляет клонированные команды. Второе восстановление сохраняет отмену.
- История для clean-backup фиксируется после завершения executor; внезапное выключение может сохранить interruption metadata, не отменяя уже подтверждённый receipt.

CI отдельно проверяет PostgreSQL, MQTT TLS, реальные процессы и неизменность постороннего Docker inventory. Браузерные видео/скриншоты относятся к точной паре Core/Web. Проверки физической платы не выполнялись. Контракты: [DURABLE_WAITS.md](DURABLE_WAITS.md), [HOUSE_SCHEDULES.md](HOUSE_SCHEDULES.md).

## 5. Rollback

Штатно завершить новый launcher. Запустить **нетронутый исходный** профиль его исходным tooling/Web из таблицы:

```bash
"$PYTHON" "$CORE_SOURCE/tools/durable_software.py" start --state "$SOURCE_PROFILE" --web-root "$WEB_SOURCE"
```

Не делать downgrade рабочей БД и не менять identity. В исходнике может продолжиться его собственное допустимое ожидание; копия его не выполняет. Изменения и история нового профиля не переносятся обратно: сохранить оба профиля и backups, откат не является синхронизацией. Если исходник недоступен, старый backup восстанавливается старым tooling только в другое новое назначение.

[Историческая инструкция schema0017 #58](https://github.com/Janik77/kz-home-core/blob/cfe6ce70ed8e2f3392567ec27a807343d5e866bc/docs/SOFTWARE_UPGRADE.md)
