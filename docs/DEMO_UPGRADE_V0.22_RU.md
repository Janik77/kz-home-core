# Core v0.20 → v0.22: проверка и инструкция для демо наставника

Проверено 2026-11-03 на изолированной синтетической установке.
**Локальный upgrade drill прошёл; обновление наставника не выполнялось.**
Проверенный кандидат Core — `e73180a20468dda1b2da318cc26411bee340c4e1`
(draft PR #29), исходный Core — `2930ee9` (v0.20), Web —
`809266f5c0e44f2a2b591c494c62b692429fbce0` (v0.19).
Draft PR и этот документ сами по себе не являются разрешённым релизом:
наставнику выполнять процедуру только после review и передачи точного approved
Core commit с проверенным runtime-содержимым. Сейчас не переходить на feature-ветку
и не обновлять наставника из draft. Firmware v0.12 для software demo не требуется.

## Что проверено

Создан новый `kzhome-demo-check-v02244ef7cdde8` с собственными credentials,
CA/certificates, loopback ports и project-owned Docker volumes. Настоящий Core
v0.20 собран и запущен в отдельном worktree, затем source обновлён **в том же
каталоге** до v0.22. Исходные private paths и volumes не переносились.
Данные/секреты наставника не читались и не копировались.

| Проверка | Результат |
|---|---|
| Реальные `upgrade-check`, `backup`, `upgrade`, `verify` | PASS |
| Настоящий PostgreSQL `0006_command_lifecycle` → `0007_control_authority` | PASS; единственная новая миграция после v0.20 |
| 2 пользователя, membership, дома/этажи/комнаты, 4 устройства, scenes/automations | Все сохранённые поля совпали после upgrade |
| Старый ledger: applied, failed и pending | Все поля совпали до первого последующего lifecycle-запроса |
| Исходный deadline pending-команды | Не изменён, после E получен timed_out |
| Password hashes, user/simulation inputs, CA/server certificates/keys, ACL/password file | Хеши совпали |
| API/Web ports, project identity, DB/MQTT mounts и volumes | Сохранены; DB/MQTT container/image IDs и StartedAt не менялись при upgrade |
| Потеря ответа после commit миграции | Journal сохранён, повтор того же upgrade восстановил установку |
| Повтор уже завершённого upgrade | Verify без повторной миграции/recreation |
| Предварительный pg_dump | Восстановлен в отдельную `audit_restore` DB; 2 users, 4 devices, 4 commands, schema 0006 |
| Старый ACL без `/control/` | Software relay продолжил MQTT TLS работу |
| Собранный Web v0.19 в Chrome, без HTTP mocks | Login/permissions, ON/OFF, повтор OFF, fail, настоящий ~30 s timeout, offline disable, anonymous 401, logout PASS |
| Итоговый ledger | applied=5, failed=2, timed_out=2; у каждого applied есть matching ACK/state/boolean |
| Полный restart только тестовых сервисов | Данные сохранились |
| Чужие ресурсы Docker | Inventory/configuration/start times/restart counts сохранены |

Тест использовал реальные PostgreSQL, Mosquitto TLS, nginx, Core HTTP и software
relay; моделировалась только потеря ответа после настоящего commit миграции.
ESP32/GPIO не участвовали, physical gate оставался закрыт. После приёмки удалены
только собственные test containers, volumes, network, images, private inputs,
backups и worktree. Базовый Docker build cache не очищался.

Профильные pytest: **109 passed, 2 config-gated skipped**; эти две Compose
проверки amd64/arm64 затем запущены opt-in: **2 passed**, 1.14 s. Ruff Core/deploy/tests/simulator/Alembic
и whitespace check прошли. ARM64 runtime этой приёмкой не подтверждён.

## Совместимость и ограничения helper

`demo.py` изменять не потребовалось. Существующий `demo_upgrade.py` кандидата
уже распознаёт 0007. `upgrade` сначала строит новые Core/Web images, затем
останавливает API/proxy, делает **собственный финальный backup**, явно запускает
Alembic без bootstrap, переключает metadata и проверяет HTTP. PostgreSQL и
Mosquitto при upgrade не пересоздаются и не обновляются.

Миграция 0007 добавляет отдельную `command_control` с RESTRICT FK, constraints
и UNIQUE permit binding. Старые command rows не меняются и не получают фиктивный
physical profile. Software demo не создаёт control rows; в приёмке их было 0.
Существующее PostgreSQL/ORM отличие `users.email` из 0003 не исправляется этой
миграцией и не потребовало изменения схемы auth.

Overlay v0.22 добавляет точную trusted software identity
`MQTT_SOFTWARE_DEVICES=[["e2e_house","e2e_relay"]]`. Не расширять список и не
назначать эту identity физической плате. Inventory physical gate имеет приоритет.
HTTP/Web контракт сохраняется, Web остаётся на v0.19; его source заново собирается
из того же проверенного commit в новый context/image, без изменения репозитория Web.

Private `mqtt/acl` при upgrade **сохраняется**; новый tracked ACL не копируется
поверх установленного. Отсутствие новых control rights не отключило legacy MQTT
demo в реальном тесте. Core умеет продолжать legacy subscriptions, если broker
отказывает experimental subscription. Некоторые версии broker принимают SUBACK,
но фильтруют запрещённые сообщения: наличие SUBACK/`ready` не доказывает control
authority readiness. Ни новые ACL, ни physical credentials для этого upgrade не нужны.

`upgrade-check` — prerequisite guard, а не полный аудит установки: он проверяет
ownership, loopback isolation, известный schema head, согласованность DB/JWT/MQTT
inputs/runtime и совпадение running infrastructure images с текущими tags.
Он не гарантирует свободное место, срок сертификатов, все MQTT права, отсутствие
внешних writers или аппаратные временные границы. До upgrade нужен успешный
software ON/OFF; после него — повторная проверка, не только `/ready`.

## PowerShell: инструкция после approval релиза

Сохранить существующий checkout, Windows account, private directory и project
`kzhome-demo-mentor2`. Не выполнять prepare/start/bootstrap/update, не переносить
private inputs и не использовать другой project name. Закрыть посторонние окна
управления демо; дождаться terminal receipts текущих команд. `timed_out` означает
неизвестный результат, а не отмену. Предусмотреть downtime и место для двух builds,
двух backups и protected копии. Эта приёмка — linux/amd64; для иной платформы
нужна отдельная runtime-проверка до обновления.

### 1. Идентификация и остановка только software relay

```powershell
$ErrorActionPreference = 'Stop'
$core = 'C:\projects\kz-home-core'
$web = 'C:\projects\kz-home-web'
$project = 'kzhome-demo-mentor2'
$expectedWeb = '809266f5c0e44f2a2b591c494c62b692429fbce0'
Set-Location $core
git status --short --branch
git -C $web status --short --branch
if ((git status --porcelain) -or (git -C $web status --porcelain)) {
    throw 'Сохраните рабочие изменения отдельно; upgrade пока не выполнять.'
}
if ((git -C $web rev-parse HEAD).Trim() -ne $expectedWeb) {
    throw 'Web отличается от проверенного v0.19; не переключать автоматически.'
}
docker version
if ($LASTEXITCODE -ne 0) { throw 'Docker недоступен.' }
docker compose version
if ($LASTEXITCODE -ne 0) { throw 'Compose недоступен; нужен >=2.24.4.' }

# Показать ТОЛЬКО публичные поля metadata, не весь JSON/env.
$public = & {
    $s = Get-Content -Raw -LiteralPath "deploy/local/demo/$project/state.json" | ConvertFrom-Json
    [pscustomobject]@{Project=$s.project; APIPort=$s.env.DEMO_API_PORT;
      WebPort=$s.env.DEMO_WEB_PORT; Platform=$s.env.DEMO_PLATFORM; WebRevision=$s.web_revision}
}
$public
if ($public.Project -ne $project -or $public.Platform -ne 'linux/amd64') {
    throw 'Неожиданная установка/платформа; нужна отдельная проверка.'
}
py -3.12 deploy/demo.py verify --run --project $project
if ($LASTEXITCODE -ne 0) { throw 'Исходная установка не прошла verify.' }

$relay = @(docker ps -aq --filter "label=kzhome.demo.virtual=$project")
if ($LASTEXITCODE -ne 0 -or $relay.Count -gt 1) { throw 'Проверьте software clients.' }
if ($relay.Count -eq 1) {
    py -3.12 deploy/demo.py virtual-relay-stop --run --project $project
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось остановить software relay.' }
}
```

Если software relay запущен другим способом, остановить его штатным способом.
Не запускать два клиента с одной identity. Не останавливать PostgreSQL/Mosquitto.
Не выводить `docker inspect ... Config/Env`, `docker compose config` без `--quiet`,
state.json, compose.env, user.json или private key/password files. Не включать transcript.

### 2. Получение точного approved Core и prerequisite check

Значение ниже предоставляет сопровождающий **после review/публикации и проверки
релизного содержимого**. Placeholder намеренно останавливает сценарий сейчас.
Не подставлять draft commit, не merge/cherry-pick PR #29 вручную.

```powershell
$approvedCore = 'PASTE_APPROVED_RELEASE_COMMIT_AFTER_REVIEW'
if ($approvedCore -notmatch '^[0-9a-f]{40}$') { throw 'Нет approved Core release.' }
if ((git branch --show-current).Trim() -ne 'main') {
    throw 'Ожидается существующий main; не переключать ветку автоматически.'
}
git fetch origin
if ($LASTEXITCODE -ne 0) { throw 'Fetch не выполнен.' }
git merge-base --is-ancestor $approvedCore origin/main
if ($LASTEXITCODE -ne 0) { throw 'Approved commit не опубликован в main.' }
# Только локальный fast-forward source до точного release; это не merge PR.
git merge --ff-only $approvedCore
if ($LASTEXITCODE -ne 0) { throw 'Невозможен fast-forward; сохранить checkout и остановиться.' }
if ((git rev-parse HEAD).Trim() -ne $approvedCore) { throw 'Core HEAD отличается от approved.' }

py -3.12 deploy/demo.py upgrade-check --run --project $project --web $web
if ($LASTEXITCODE -ne 0) { throw 'Prerequisite check отклонён; ничего не обходить.' }
```

Web не обновлять через pull: его проверенный hash остаётся прежним. Если guard
сообщает image mismatch после чужого pull инфраструктурных tags, не пересоздавать
брокер/БД ради обхода: нужен отдельный разбор. Не менять credentials или TLS.

### 3. Backup и protected копия

```powershell
py -3.12 deploy/demo.py backup --run --project $project
if ($LASTEXITCODE -ne 0) { throw 'Backup не завершён; upgrade запрещён.' }

# Новая закрытая NTFS-папка; не менять ACL существующей папки.
$copy = Join-Path $env:USERPROFILE ('KZHomePrivateBackups\mentor2-v022-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $copy) { throw 'Выберите новую backup destination.' }
New-Item -ItemType Directory -Path $copy -Force | Out-Null
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls $copy /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Не удалось защитить backup destination.' }
Copy-Item -LiteralPath "deploy/local/demo/$project/backups" -Destination $copy -Recurse
```

Backup содержит настоящий dump, credentials и private keys. Не отправлять его в
чат/Git/публичное облако. Копия в USERPROFILE защищает от случайного удаления рабочего
каталога, но не от отказа диска: перед upgrade сохранить также protected offline
копию на отдельном носителе по локальной политике владельца. Не удалять исходные backups.

### 4. Upgrade, verify, virtual relay

```powershell
py -3.12 -u deploy/demo.py upgrade --run --project $project --web $web
if ($LASTEXITCODE -ne 0) { throw 'Upgrade остановился; сохранить pending journal и backups.' }
py -3.12 deploy/demo.py verify --run --project $project
if ($LASTEXITCODE -ne 0) { throw 'Verify не прошёл; relay пока не запускать.' }
Start-Process "http://127.0.0.1:$($public.WebPort)"
```

Во **втором PowerShell**, оставить процесс foreground:

```powershell
Set-Location 'C:\projects\kz-home-core'
py -3.12 -u deploy/demo.py virtual-relay --run --project kzhome-demo-mentor2
```

В Web войти прежней учётной записью, обновить страницу, дождаться online для
«SIM · реле». Выполнить ON, затем OFF; каждый раз дождаться
«Выполнено · данные Core обновлены» и фактического показанного состояния.
202/online/HTTP health не заменяют ACK+state. Проверить историю прежних команд,
пользователей и устройств. Для остановки из первого PowerShell:

```powershell
py -3.12 deploy/demo.py virtual-relay-stop --run --project $project
if ($LASTEXITCODE -ne 0) { throw 'Проверьте состояние software relay.' }
```

После обновления страницы offline-кнопки должны быть недоступны. При необходимости
снова запустить обычный foreground virtual-relay. Установка наставника не требует
Firmware, control ACL или открытия physical gate.

## Прерывание и ограничения отката

- Нет атомарного upgrade и автоматического rollback. При pending journal повторять
  только `upgrade` с **тем же source fingerprint/Web commit**, private inputs и
  сохранёнными candidate images. Не удалять/переименовывать journal ради обхода,
  не применять start/bootstrap/update/restart к незавершённому upgrade.
  Между попытками не обновлять source: fingerprint включает также файлы `deploy/`,
  в том числе инструкцию наставника. Даже изменение только этого текста может
  отклонить resume; сначала завершить операцию с исходным проверенным source.
- Предварительный backup и финальный backup upgrade различаются: после отдельного
  backup API запускается снова. Для восстановления выбирать согласованный момент
  и соответствующие private inputs; все записи после выбранного dump будут потеряны.
- Старый Core v0.20 ожидает schema 0006: просто вернуть старый image при DB 0007
  недостаточно, readiness может отказать. Downgrade 0007 требует online check и
  запрещён при любой control history. Отсутствие control rows в software демо
  не превращает downgrade в проверенную операторскую процедуру отката.
- PostgreSQL major/binary version не меняется этим upgrade. Не запускать старый
  major на новом data volume, не выполнять down -v/volume prune/erase, не pg_restore
  --clean в единственную существующую DB. Восстановление планировать отдельно,
  сначала side-by-side в новый volume/DB с сохранёнными ролями и совместимыми tools.
  Успешный restore в audit_restore доказывает читаемость dump, не полный disaster rollback.
- CA/server key/cert, MQTT password/ACL, JWT/DB credentials должны соответствовать
  выбранному backup. Не regenerate prepare inputs и не ротировать сертификаты при
  этом обновлении. Истечение/неподходящий SAN сертификата требует отдельной процедуры.
- pg_dump не включает Mosquitto persistence, retained state, sessions/outbox и
  messages in flight. Live MQTT volume сохраняется при upgrade, но не включён в
  backup helper. Нужен отдельный согласованный snapshot для полного disaster recovery;
  нельзя считать restored DB синхронной с произвольным MQTT snapshot.
- После restart software relay начинает OFF и сообщает текущее software state;
  terminal receipts и timeout не доказывают отмену уже отправленного действия.
  Physical commands по-прежнему запрещены, никаких control permits в production.

## Воспроизведение приёмки и изменённые файлы

```powershell
& C:/projects/kz-home-core/.venv/Scripts/python.exe -B -u tests/demo_upgrade_v022_acceptance.py `
  --run --web C:/projects/kz-home-web
```

Runner требует доступных Docker Linux/Compose, Node/Chrome/Playwright для существующих
Web tests и Python dependencies Core. Создаёт свой worktree/project от 2930ee9,
обновляет только собственную копию до e73180a, выполняет реальный drill и удаляет
свои ресурсы. В обычном режиме runner сам выбирает новый случайный project;
внутренние phase/checkout/project аргументы не предназначены для операторского запуска.

Добавлены только этот документ и `tests/demo_upgrade_v022_acceptance.py`; ссылка
в `deploy/MENTOR_DEMO_RU.md` обновлена. `demo.py`, upgrade-helper, runtime, migrations,
ACL и Compose не менялись. Firmware/Web сохранены. Коммитов, PR, merge и реального
обновления наставника в этом этапе нет.
