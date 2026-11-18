# KZ Home: безопасный переход на расписания

**Кандидат, квалифицируется только в CI. На ПК владельца ничего не устанавливаем и не запускаем по этой инструкции сейчас.**
Переход разрешён лишь после успешных проверок точной версии и согласованного окна работ.
Это программный стенд, не production и не испытание ESP32 или физического реле.

## Зафиксированные версии назначения

| Часть | Версия |
|---|---|
| Core runtime, PR #53 | `45b63628829580bc564ff2490af645951926e82e` |
| Web, PR #30 | `f5508461d00ae9a7c9841330a30c83bd6a8250da` |
| Исполняемый tooling | `2512878dedc722f4073c0991a80b228b5af404e9` |
| Схема | `0015_house_schedules` |

Tooling содержит проверку равенства runtime-файлов указанному Core SHA.
Последующие изменения только документации не меняют исполняемую сборку.
Проверки CI и итоговый отчёт должны явно называть проверенный integration SHA;
наличие этой инструкции само по себе не означает прохождение приёмки.

## Поддерживаемые исходные профили

| Источник | Tooling checkout | Core runtime | Web | Схема |
|---|---|---|---|---|
| #42 | `82653a849d2b61479de1c3bb4d74e7f388523667` | `f946a07f67f9ccb1e8f62c99860a0842bf3a062b` | `c2c4fa89a0da3652e8e0cca3629300ecfd7e658f` | `0010_scene_runs` |
| #50 | `c03aa044642e8e09bf61ff51c35a46079d1118f0` | `f6a0826929c87b39dec7e81d22280f1218bf18f1` | `6f8f72f86f668b53f85c13054644c80427fe9eab` | `0013_device_automations` |
| #52 | `244549d3cd6b5b2c5b7bc9757e438d2729553155` | `1ae52afa22431943064b5e85313f671b073b9458` | `74b7238f10a9e56cbc600f9cac21b29a5c15b46e` | `0014_numeric_thresholds` |

Неподдерживаемую пару SHA или другой профиль нельзя «исправлять» вручную.
Сохраняются все исходные данные: пользователи, дома, комнаты, избранное,
права, определения, ревизии, receipts и история. Для #52 сохраняются также
числовые значения, временные метки, watermarks и потреблённые пороги.
Новые поля расписаний пусты; часовой пояс дома NULL, без автоподстановки.
Физический inventory запрещён. Архивы и содержимое секретов не публикуются.

## 1. Сначала только чтение

Использовать уже существующие согласованные checkout и изолированный Python.
Пути `CORE_SOURCE`, `WEB_SOURCE`, `CORE_TARGET`, `WEB_TARGET`, `SOURCE_PROFILE`,
`BACKUP`, `NEW_PROFILE`, `PYTHON` назначаются по фактической проверке, не по предположению.
Не выполнять clone, reset, stash, очистку, смену checkout или установку зависимостей
в используемой/грязной рабочей копии. Незакоммиченные документы и данные сохранить.

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
# CONTAINER выбирается из результата, не угадывается:
docker inspect "$CONTAINER" --format '{{json .Mounts}} {{json .Config.Cmd}}'
```

Проверить, не используются ли checkout работающим процессом, bind mount или reload.
При active-use, dirty, неожиданных `.env`, неизвестном профиле или несовпадении SHA
остановить именно подготовку. Не останавливать контейнеры и не править привязки.
Tooling дополнительно проверяет путь/владельца/identity, exact clean source,
manifest/hash, schema, пустое новое назначение и completion markers.
Native Windows для POSIX tooling не поддерживается; использовать ранее
квалифицированную Linux-среду только в согласованное окно, без sudo и рестарта WSL/Docker.

## 2. Backup → новое назначение

Только после окна работ штатно завершить исходный foreground launcher в его терминале.
Не угадывать PID, не рестартовать Docker/WSL и не трогать соседние проекты.
Backup активного профиля будет отклонён. `BACKUP` и `NEW_PROFILE` должны быть новыми путями.

```bash
"$PYTHON" "$CORE_SOURCE/tools/durable_software.py" backup --state "$SOURCE_PROFILE" --destination "$BACKUP"
"$PYTHON" "$CORE_TARGET/tools/durable_software.py" upgrade --backup "$BACKUP" --state "$NEW_PROFILE" --source-checkout "$CORE_SOURCE"
"$PYTHON" "$CORE_TARGET/tools/durable_software.py" status --state "$NEW_PROFILE"
"$PYTHON" "$CORE_TARGET/tools/durable_software.py" start --state "$NEW_PROFILE" --web-root "$WEB_TARGET"
```

`start` работает в foreground и выводит фактический адрес. Не обещать постоянный
внешний URL или автоматическую публикацию. Новый профиль получает собственную
identity и owned-ресурсы; исходный профиль и backup остаются нетронутыми.
Никакого редактирования `profile.json`, directory/database identity, pins или `.env`.
При ошибке частичное назначение не считается готовым и не очищается автоматически.

## 3. Проверка

- Вход, сохранность структуры/избранного/прав/истории и ревизий.
- Программное реле: команда → ожидание → ACK и совпадающее состояние; неизвестный исход не скрывать.
- Сохранность numeric values/freshness/watermarks/latch, отсутствие повторов старых samples.
- Явное сохранение IANA timezone в настройках дома; никакого выбора по браузеру.
- Расписание на будущую минуту: один запуск, понятный результат, без запуска при сохранении.
- Выключение/редактирование/смена timezone не воспроизводит текущую минуту; startup без catch-up.
- После штатного stop/start и backup→restore в ещё одно новое назначение нет повторных команд.

CI отдельно проверяет PostgreSQL/TLS, actual-clock schedule→ACK/state, падение процесса,
пороги, все три источника, отказные сценарии, rollback и неизменность Docker inventory.
Браузерное видео — отдельная проверка; программную эмуляцию не считать испытанием платы.
Семантика расписаний: [HOUSE_SCHEDULES.md](HOUSE_SCHEDULES.md).

## 4. Rollback

Штатно завершить launcher нового назначения. Запустить **нетронутый исходный**
профиль его старым Core tooling и старым Web из таблицы:

```bash
"$PYTHON" "$CORE_SOURCE/tools/durable_software.py" start --state "$SOURCE_PROFILE" --web-root "$WEB_SOURCE"
```

Не делать downgrade рабочей БД, не переносить файлы профиля и не менять identity.
Если исходник недоступен, его backup восстанавливается старым tooling только в ещё
одно новое назначение. Исходный backup неизменяем. Дополнительные разрешения,
секреты, физическое оборудование и production вне рамок этого перехода.

[Историческая инструкция numeric #52](https://github.com/Janik77/kz-home-core/blob/244549d3cd6b5b2c5b7bc9757e438d2729553155/docs/SOFTWARE_UPGRADE.md)
