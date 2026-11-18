# Numeric integration candidate: supported transfer (0014)

This section supersedes the historical target below. No user profile was migrated
while developing this branch. Acceptance must be green before using the commands.

Target runtime: `3ba219e8d6b776ad4024e8b23ea894a43c22eb6d` (Core #51).
Target Web: `03065357005b48b7ffd69ee879ab45caebd2f55a` (Web #29 candidate).
Target schema: `0014_numeric_thresholds`. Tooling: `integration/numeric-web29`.
The runtime-root git equality guard remains mandatory. This is a support addition
in code, never a manual edit to profile.json or database identity.

Supported source contracts (both remain tested):
- #50 tooling `c03aa044642e8e09bf61ff51c35a46079d1118f0`, runtime
  `f6a0826929c87b39dec7e81d22280f1218bf18f1`, Web
  `6f8f72f86f668b53f85c13054644c80427fe9eab`, schema `0013_device_automations`.
- #42 tooling `82653a849d2b61479de1c3bb4d74e7f388523667`, runtime
  `f946a07f67f9ccb1e8f62c99860a0842bf3a062b`, Web
  `c2c4fa89a0da3652e8e0cca3629300ecfd7e658f`, schema `0010_scene_runs`.

The #50 fingerprint includes all pre0014 columns, including favorites, authority,
state timestamps, command receipts and execution metadata. Only new numeric maps
and latch default empty. #42 retains its legacy-column/default verification.
Physical inventory is refused. Archives/hashes, clean exact source checkout,
source identity, empty new target, stopped backup, database identity, no reseeding,
completion markers and ownership checks remain mandatory.

Example on supported Linux / local Docker Desktop (not native Windows):

```sh
# Stop source foreground launcher normally first; use its OWN old tooling.
python /path/to/core50/tools/durable_software.py backup --state /private/source-profile --destination /private/source50-backup
# Clean numeric integration checkout: creates only a NEW destination.
python tools/durable_software.py upgrade --backup /private/source50-backup --state /private/numeric-profile --source-checkout /path/to/core50
python tools/durable_software.py start --state /private/numeric-profile --web-root /path/to/web29
python tools/durable_software.py status --state /private/numeric-profile
```

Rollback: stop target launcher; restart untouched original with its exact old
Core/Web. Do not downgrade0014 in place or copy/rebind profile files. Backup stays
immutable. If original is unavailable, restore its backup with old tooling to a
NEW path. New target backup/restore uses new tooling only.

CI matrix tests #42 and #50, refusal before any target creation, and intentionally
failed partial restores. Numeric acceptance uses real PostgreSQL/TLS MQTT, saves
a consumed threshold latch, restores into a new identity, compares numeric value,
freshness, watermarks and latch, and proves startup/old/duplicate/still-low reports
create no extra command. Shutdown may mark a sensor offline and clear freshness:
this preserves unknownness, not a reset of the watermark or consumed latch.
Original profile/backup hashes and unrelated Docker inventory must stay unchanged.
No private backup artifacts are uploaded.

---
## Historical release notes (previous target0013)

# KZ Home: отдельный программный стенд автоматизаций

Версия-кандидат: Core integration branch `integration/web28-persistent` поверх #49 (runtime неизменён), Web `6f8f72f86f668b53f85c13054644c80427fe9eab`.

**Статус:** отдельный кандидат с Web #28. Результаты прежней пары Core f4/Web #27 не заменяют CI этой пары: перед установкой проверить все четыре workflows на точном integration SHA из отчёта/PR. Профиль владельца не изменён. Проверки GitHub не подтверждают состояние локальных папок или Docker владельца.

## 1. Сначала только проверка существующих папок

Открыть обычный Ubuntu/WSL-терминал пользователя `janik`, без `sudo`. Исторически использовались эти папки; несмотря на слово `disposable`, повторно клонировать их для этой инструкции не нужно:

```bash
set -euo pipefail
CORE="$HOME/kzhome-test/disposable-20261115/core"
WEB="$HOME/kzhome-test/disposable-20261115/web"
printf 'Core: %s\nWeb: %s\n' "$CORE" "$WEB"
for P in "$CORE" "$WEB"; do
  test -d "$P/.git" -o -f "$P/.git" || { echo "Нет ожидаемого checkout: $P"; exit 1; }
  git -C "$P" remote get-url origin
  git -C "$P" rev-parse HEAD
  git -C "$P" status --short
  test -z "$(git -C "$P" status --porcelain)" || { echo "Есть изменения. Остановиться, ничего не перезаписывать."; exit 1; }
  find "$P" -maxdepth 1 -name '.env*' ! -name '.env.example' -print
 done
python3.12 --version
node --version
npm --version
docker info --format '{{.ServerVersion}}'
```

Условия продолжения:
- origin относится именно к `Janik77/kz-home-core` и `Janik77/kz-home-web`;
- обе рабочие папки чистые, нет реальных `.env`/`.env.local` и неизвестных локальных файлов конфигурации;
- подтверждено, что никакой текущий стенд/процесс не использует эти checkout;
- Python 3.12, Node 24, Docker доступны без изменения прав, групп или настройки демона.

Если путь, состояние или назначение папки отличается, **остановиться**. Не применять `reset --hard`, `clean`, удаление/перезапись, автоматический stash. Не переключать checkout, который используется другим процессом. Не останавливать всю WSL/Docker и другие проекты. Не трогать Windows-папки `C:\projects\...`, их изменения и документы наставнику.

## 2. Зафиксировать версию только после успешной проверки

Этот раздел не следует запускать автоматически вслед за проверкой: сначала подтвердить перечисленные условия. Он не меняет `main`, а переводит только проверенные свободные рабочие папки на точные тестовые коммиты.

```bash
set -euo pipefail
: "${CORE_SHA:?Укажите точный integration SHA из принятого отчёта с зелёными CI, не старый f4/617c}"
WEB_SHA=6f8f72f86f668b53f85c13054644c80427fe9eab
git -C "$CORE" fetch origin integration/web28-persistent
git -C "$WEB" fetch origin feature/visible-live-device-state
git -C "$CORE" cat-file -e "$CORE_SHA^{commit}" && git -C "$WEB" cat-file -e "$WEB_SHA^{commit}"
git -C "$CORE" switch --detach "$CORE_SHA"
git -C "$WEB" switch --detach "$WEB_SHA"
test "$(git -C "$CORE" rev-parse HEAD)" = "$CORE_SHA"
test "$(git -C "$WEB" rev-parse HEAD)" = "$WEB_SHA"
```

Создать отдельное окружение только для стенда, не заменяя прежнее:

```bash
VENV="$HOME/.local/share/kzhome-software/venv-web28-pilot"
test ! -e "$VENV" || { echo 'Окружение уже существует: сначала проверить его назначение, не перезаписывать.'; exit 1; }
python3.12 -m venv "$VENV"
"$VENV/bin/python" -m pip install -r "$CORE/requirements-dev.txt"
"$VENV/bin/python" -m pip check
npm --prefix "$WEB" ci
```

## 3. Новый постоянный профиль: создать ровно один раз

Это новая изолированная программная лаборатория, не восстановление старого тестового дома. Старый `acknowledgement-profile` и его служебные привязки оставить как есть. Не редактировать `profile.json`, inode/device identity, hashes, pins или Docker labels вручную.

```bash
STATE="$HOME/.local/share/kzhome-software/web28-pilot-v1"
test ! -e "$STATE" || { echo 'Профиль уже существует: init повторять нельзя.'; exit 1; }
"$VENV/bin/python" -B "$CORE/tools/durable_software.py" init --state "$STATE"
"$VENV/bin/python" -B "$CORE/tools/durable_software.py" start --state "$STATE" --web-root "$WEB"
```

Launcher напечатает локальный адрес. Открыть именно его в браузере Windows. Вход `owner@example.test`; пароль находится в локальном приватном `profile.json`. Не присылайте этот файл и не загружайте профиль/backup в GitHub. Если адрес из Windows недоступен, не менять сеть/VPN/защиту: проверить отдельно доступность loopback WSL.

Только программное MQTT-реле; физических устройств в этом стенде нет. Общий путь: создать и сохранить отключённое правило → «Проверить условия (без команд)» → включить → изменить программное состояние → проверить итог и квитанции действий. ACK без подтверждённого состояния не считается успехом. На неизвестный исход не нажимать повторно вслепую.

## 4. Остановка и повторный запуск

**Это foreground-запуск, не проверенный фоновый supervisor.** Оставить терминал открытым. Для остановки нажать `Ctrl+C` в этом терминале: процессы стенда остановятся, PostgreSQL volume и профиль сохранятся. Закрытие терминала/выключение ПК не следует считать эквивалентом штатной остановки.

После штатной остановки:

```bash
"$VENV/bin/python" -B "$CORE/tools/durable_software.py" status --state "$STATE"
"$VENV/bin/python" -B "$CORE/tools/durable_software.py" start --state "$STATE" --web-root "$WEB"
```

В новом терминале заново задать `CORE`, `WEB`, `VENV`, `STATE` теми же значениями. **Не повторять `init`, clone или создание окружения.** Если launcher уже завершился, но его контейнеры остались, предусмотрена команда `stop --state "$STATE"`; она действует только на ресурсы этого профиля. Когда launcher активен, сначала нужен `Ctrl+C`: другие lifecycle-команды не обходят его lock.

Удобный start/stop из отдельной команды, автозапуск после входа, systemd/supervisor здесь не обещаны. Отдельный WIP #48 в эту версию не входит.

## 5. Backup и восстановление в новый профиль

Только после первого успешного запуска и штатной остановки. Папки назначения не должны существовать. Backup содержит пароли и данные, хранить локально приватно.

```bash
BACKUP="$HOME/.local/share/kzhome-software/backups/automation-pilot-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$(dirname "$BACKUP")"
"$VENV/bin/python" -B "$CORE/tools/durable_software.py" backup --state "$STATE" --destination "$BACKUP"
RESTORED="$HOME/.local/share/kzhome-software/automation-pilot-restored-v1"
test ! -e "$RESTORED" || { echo 'Папка восстановления уже существует.'; exit 1; }
"$VENV/bin/python" -B "$CORE/tools/durable_software.py" restore --backup "$BACKUP" --state "$RESTORED"
"$VENV/bin/python" -B "$CORE/tools/durable_software.py" start --state "$RESTORED" --web-root "$WEB"
```

Это exact-version restore в новый профиль; исходный профиль и backup не перезаписываются. Для дальнейшей работы выбрать один профиль, не запускать две лаборатории с ожиданием общей базы. Возврат: остановить восстановленный launcher и запустить исходный `STATE`. Не делать downgrade схемы или ручное копирование служебных JSON.

При `PROFILE_FAILURE`, несовпадении identity, дрейфе часов или незавершённом restore сохранить файлы, остановиться и передать только тип ошибки/безопасный лог, без секретов. Не «чинить» проверку вручную и не повторять команды управления с неизвестным исходом.


## Приложение: единственный поддерживаемый межверсионный upgrade

Для нового стенда выше этот раздел **не нужен**. Он не разрешает экспериментальное восстановление старого проблемного профиля. Межверсионный `upgrade` принимает только собственный доверенный backup со следующими точными исходными версиями:

- Core #42 tooling `82653a849d2b61479de1c3bb4d74e7f388523667`;
- исходный runtime `f946a07f67f9ccb1e8f62c99860a0842bf3a062b`;
- Web #20 `c2c4fa89a0da3652e8e0cca3629300ecfd7e658f`;
- схема `0010_scene_runs`, нулевой физический inventory;
- полные, неизменённые identity/owner/path/device/inode/database поля и manifest.

Назначение: runtime `f6a0826929c87b39dec7e81d22280f1218bf18f1`, Web `6f8f72f86f668b53f85c13054644c80427fe9eab`, схема `0013_device_automations`; tooling из integration/web28-persistent. Backup из промежуточных #43/#44/#45/#46/#47, иных Web pins,0011/0012 или произвольных ревизий **не поддержан** этой командой. Не заменять их pins, не править identity и не выдавать ordinary restore за межверсионную миграцию.

Только после отдельного подтверждения исходных checkout/профиля и остановки его foreground launcher можно задать абсолютные `OLD_CORE`, `OLD_WEB`, `OLD_PYTHON`, `OLD_STATE` (исходное положение профиля) и выполнить:

```bash
set -euo pipefail
: "${OLD_CORE:?нужен проверенный исходный checkout}"
: "${OLD_WEB:?нужен проверенный исходный checkout Web}"
: "${OLD_PYTHON:?нужен Python исходного окружения}"
: "${OLD_STATE:?нужен исходный абсолютный путь профиля}"
test "$(git -C "$OLD_CORE" rev-parse HEAD)" = 82653a849d2b61479de1c3bb4d74e7f388523667
test "$(git -C "$OLD_WEB" rev-parse HEAD)" = c2c4fa89a0da3652e8e0cca3629300ecfd7e658f
test -z "$(git -C "$OLD_CORE" status --porcelain)"
test -z "$(git -C "$OLD_WEB" status --porcelain)"
SOURCE_BACKUP="$HOME/.local/share/kzhome-software/backups/source42-$(date -u +%Y%m%dT%H%M%SZ)"
UPGRADED="$HOME/.local/share/kzhome-software/automation-from42-v1"
test ! -e "$SOURCE_BACKUP" && test ! -e "$UPGRADED"
"$OLD_PYTHON" -B "$OLD_CORE/tools/durable_software.py" backup --state "$OLD_STATE" --destination "$SOURCE_BACKUP"
"$VENV/bin/python" -B "$CORE/tools/durable_software.py" upgrade --source-checkout "$OLD_CORE" --backup "$SOURCE_BACKUP" --state "$UPGRADED"
"$VENV/bin/python" -B "$CORE/tools/durable_software.py" start --state "$UPGRADED" --web-root "$WEB"
```

Upgrade проверяет whitelist/checksums, исходную схему и identity, создаёт **новые** profile/network/volume/owner/path bindings, восстанавливает и мигрирует только новый PostgreSQL. Исходный профиль и backup не перемещаются и не изменяются. Старые колонки всех таблиц сравниваются ordered row fingerprints/counts; отдельно проверены defaults добавленных колонок: scene source=scene/manual, authority и observed state=NULL, favorites из исходной0010 отсутствуют и после upgrade пусты. Существующие правила требуют явного authorized edit/enable перед использованием.

`initialized` появляется атомарно только после успешных проверок, marker writes и штатной остановки. Partial target сохраняется для диагностики, его нельзя переиспользовать, reseed или вручную объявить initialized. Manifest проверяет целостность, но не является цифровой подписью: использовать только собственный доверенный SQL-backup (dump способен выполнять SQL).

Откат — остановить новый launcher и открыть **неизменённый** старый своим старым checkout/окружением; это не downgrade БД. Изменения, сделанные только в новом профиле, назад не копируются. Сохранить обе истории. Не следует заключать, что реальное устройство выключено, только по восстановленной записи или откату.

Acceptance этой версии: workflows `durable-software.yml` и `software-upgrade.yml` на том же точном integration SHA, что выбран выше. Не считать их пройденными до зелёного завершения. Проверены изолированные PostgreSQL/MosquittoTLS и Python-реле, не ПК владельца и не оборудование. Очистка CI относится лишь к собственным временным тестовым ресурсам; CLI не предоставляет prune/delete.

## Ограничение смены Web pin

Новый launcher принимает только Web #28. Профиль прежней пары Web #27 нельзя перепривязать редактированием `profile.json`: он останется доступен только со своим прежним launcher и Web. Межверсионный путь #49/Web27 → Web28 здесь не реализован; не переносить данные вручную. Новый профиль создаётся один раз и сохраняется при последующих stop/start и exact-version backup/restore. Никаких дополнительных копий checkout на ПК эта инструкция не требует.
