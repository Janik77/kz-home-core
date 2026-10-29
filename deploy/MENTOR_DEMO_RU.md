# KZ Home v0.14 — демо для технического наставника

Это **реальные Core + PostgreSQL + Mosquitto TLS + собранный Web**, но устройства
программные. Web работает в Core mode и остаётся read-only. Данные сохраняются
между остановками. Production/local установки не используются.

## 1. Что установить

- Windows 11, Git и Python **3.12+** с launcher `py`. Host pip/Node не нужны.
- [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/):
  включите аппаратную виртуализацию, WSL 2 и **Linux containers**. Запустите Desktop
  и дождитесь готовности Engine. При необходимости установите WSL командой
  `wsl --install` в административном PowerShell и перезагрузитесь.
- Docker Compose **2.24.4+**: overlay использует
  [`!override`](https://docs.docker.com/reference/compose-file/merge/).
- Интернет для первого получения образов и npm-пакетов; свободное место для
  Docker, PostgreSQL и двух сборок. Для Windows разумно выделить Docker 4 ГБ RAM
  или больше; это стартовый бюджет, не измеренный предел производительности.

Проверьте в обычном PowerShell:

```powershell
git --version
py -3.12 --version
docker version
docker compose version
```

Нужен доступ к двум GitHub-репозиториям. Клонируйте их рядом:

```powershell
mkdir C:\projects -ErrorAction SilentlyContinue
cd C:\projects
git clone https://github.com/Janik77/kz-home-core.git
git clone https://github.com/Janik77/kz-home-web.git
cd kz-home-core
```

**Версия исходников:** владелец должен передать опубликованный Core ref с v0.14
(`deploy/demo.py`, `compose.demo.yaml`) и проверенный Web ref. Текущая работа не
коммитится и не публикуется автоматически; обычный clone main до публикации может
ещё не содержать v0.14. Проверьте наличие `deploy/demo.py` и согласуйте refs перед
демонстрацией. Helper не выполняет checkout/pull/merge и не меняет ветки.

## 2. Подготовка и первый запуск

Из корня Core, без прав администратора (если Docker уже доступен пользователю):

```powershell
py -3.12 -u deploy/demo.py prepare --run --web ..\kz-home-web
py -3.12 -u deploy/demo.py start --run
py -3.12 deploy/demo.py credentials --run
```

Первый запуск может занять несколько минут. `prepare`:

- создаёт отдельный проект `kzhome-demo`, случайные DB/JWT/MQTT/user секреты,
  собственный demo CA и серверный сертификат SAN `mosquitto`;
- ограничивает доступ к папке текущим Windows SID и SYSTEM (на Linux — `0700` /
  секретные файлы `0600`); хранит входные данные только в игнорируемой
  `deploy/local/demo/kzhome-demo/`, вне Docker build context Core;
- копирует tracked-файлы чистого Web checkout в отдельный build context Core.
  Исходный Web, его `.env`, `node_modules` и `dist` не изменяются и не используются;
- собирает Web через `npm ci` внутри Node-контейнера, только с публичными
  `VITE_DATA_MODE=core`, `VITE_API_URL=/core-api`. Секретов в VITE нет.

`start` собирает образы, поднимает БД/брокер, создаёт отдельную несуперпользовательскую
роль БД, явно применяет Alembic, запускает bootstrap и затем Core/Web. Core остаётся
`APP_ENV=production`, MQTT TLS и проверка hostname включены; in-process simulator
выключен. Никакого auth bypass или anonymous MQTT нет.

`credentials` **намеренно** показывает только demo email/пароль в интерактивном
терминале. Не запускайте его с записью transcript/видеотрансляцией, не копируйте
секреты в чат или Git. Команда отказывается выводить их в pipe/файл. Остальные
команды секреты не печатают. Локальные файлы содержат секреты в открытом виде:
ограничение доступа не является шифрованием. Docker-администраторы могут их читать.

Откройте **http://127.0.0.1:18081**, войдите с показанными demo credentials.
Swagger для оператора: **http://127.0.0.1:18080/docs**.
Public/LAN HTTP не публикуется; БД и брокер вообще не имеют host ports.

Повторный `prepare` сохраняет конфигурацию и пароли. Повторный `bootstrap` безопасен
для этой demo-установки: проверяет пароль/ownership/структуру, не сбрасывает state,
не меняет пароль и не создаёт дубликаты. Чужие пользователи/дома и конфликтующие
определения приводят к отказу, а не перезаписи. При неполном `prepare` после сбоя
не удаляйте данные вслепую: проверьте причину или выберите новое имя demo-проекта.

## 3. Что показать

После входа доступны один **DEMO · виртуальный дом**, этаж, две комнаты, три
устройства с метками **SIM**, две сцены и отдельная automation через API.

- Home, Rooms, room detail и Scenes получают реальные HTTP-ответы Core из PostgreSQL.
- Изначально реле может не иметь reported state; это корректно, не mock-подмена.
- Температура 22.5 °C — явно статический simulated snapshot, не живой термометр.
- Устройства offline после завершения одноразового сценария — ожидаемо.
- Web не отправляет команды устройствам и не запускает сцены. Он показывает
  состояние и описание поддерживаемых действий. Нажимайте **Refresh home** после
  симуляции: подписки realtime для браузера в этом milestone нет.
- В Settings можно сменить тему и выйти. После reload вход требуется повторно:
  токены хранятся только в памяти вкладки.

В другом PowerShell из Core выполните по очереди:

```powershell
py -3.12 -u deploy/demo.py simulate --run
py -3.12 -u deploy/demo.py simulate --run --scenario automation
```

Первый сценарий переиспользует `simulator.e2e_acceptance`: HTTP ON/OFF → MQTT v1
команда → настоящий программный Relay → соответствующие программные ACK/state →
persisted EventLog/GET. Второй переиспользует `simulator.e2e_automation`: MQTT motion
→ существующий AutomationService → relay → correlated ACK/state → PostgreSQL.
Второй оставляет реле включённым/offline, правило — выключенным. Повторный первый
сценарий вернёт его в OFF. Запускайте сценарии последовательно, без конкурирующего
relay-процесса или ручных команд на те же IDs.

Оба клиента имеют отдельные пароли и прежние узкие ACL на
`e2e_house/e2e_relay` и `e2e_house/e2e_motion`. Они работают только во внутренней
demo-сети, не получают DB/JWT-секреты. Логин и MQTT-пароли передаются через stdin,
не аргументы процесса. Refresh-сессия оператора отзывается при завершении сценария.
ACK подтверждает **программное** применение; это не физическая проверка реле.
Прошивка, GPIO, электрика, физический onboarding и реальная доставка на ESP32 не
демонстрируются. Новый MQTT-протокол не вводится.

## 4. Проверка, остановка, перезапуск и обновление

```powershell
py -3.12 deploy/demo.py verify --run
py -3.12 deploy/demo.py bootstrap --run
py -3.12 deploy/demo.py stop --run
py -3.12 -u deploy/demo.py restart --run
```

`verify` проверяет HTTP Web/deep link, `/health`, `/ready` и печатает статусы.
`/ready` не доказывает MQTT readiness; MQTT-путь проверяют отдельные simulation
сценарии. Mosquitto/proxy в `ps` имеют статус running, не встроенный Docker healthcheck.

`stop` сохраняет контейнеры, ключи и тома `kzhome-demo_postgres_data` /
`kzhome-demo_mosquitto_data`. `restart` выполняет graceful stop/start с повторной
проверкой миграций/bootstrap. Даже обычный повторный `start` останавливает только
demo Core/proxy перед миграциями; небольшой перерыв ожидаем. Existing local/
production проекты остаются нетронутыми. **Не используйте `down -v`, prune или
удаление томов**. Потеря локальной папки секретов не исправляется генерацией новых
паролей для старого PostgreSQL volume.

Перед обновлением сохраните согласованный backup БД, MQTT и приватных inputs;
процедура в [ARM64_DEPLOYMENT.md](ARM64_DEPLOYMENT.md). Её `dc` нужно настроить
на demo-проект и его overlays; не запускайте команды production-проекта для demo.
Владелец вручную выбирает/доставляет проверенные новые refs Core/Web. Затем:

```powershell
py -3.12 -u deploy/demo.py update --run
```

Helper не выполняет Git-операции: берёт текущие чистые tracked-файлы Web в новый
snapshot, сохраняет прежний snapshot, пересобирает Core/Web и повторяет безопасный
запуск. Он не вращает пароли, не меняет major PostgreSQL и не скачивает новые
DB/broker версии автоматически. Обновление инфраструктурных образов и откат схемы
требуют отдельного плана из v0.13. Базовые image tags и transitive Python packages
не полностью locked: сохраните source refs, image digests и backup проверенного
демо. Это воспроизводимая процедура, не обещание побайтово одинаковой сборки.

## 5. Если не работает

| Симптом | Что проверить |
| --- | --- |
| Docker недоступен | Desktop запущен, Linux containers/WSL 2 готовы, `docker version` показывает Server. |
| `!override` не распознан | Обновить Compose до 2.24.4+; не редактировать production defaults. |
| Первичная сборка не проходит | Интернет/DNS/proxy Docker и доступ к npm/registry; свободное место/RAM. Вывод внешних команд намеренно скрыт, чтобы не утекли secrets. |
| Порты заняты | Для **нового** проекта задать `prepare --project kzhome-demo-mentor --api-port 18280 --web-port 18281`; тот же `--project` передавать всем командам. Повторный prepare не меняет старые порты. |
| Login не принят | Использовать `credentials --run` именно того проекта. Не использовать production или Web mock аккаунты. |
| Readiness не проходит | Проверить приватный compose config через `config --quiet`, DB health, успешные migrations; не сбрасывать volume. |
| Симуляция не подтверждена | Проверить `verify`, время хоста, срок demo-сертификата, Core MQTT subscription log; исключить параллельные сценарии/чужие процессы. HTTP 200 ещё не ACK. |
| Данные в Web не меняются | Refresh home; убедиться, что открыт demo порт 18081, а не Vite/mock/старый local stack. |
| Отказ bootstrap | Не исправлять reset/подменой пользователя: проверить сохранённые inputs, ownership и конфликтующие demo IDs. |

Для конфигурации/ограниченных логов PowerShell helper (не печатайте полный config):

```powershell
$envFile = "deploy/local/demo/kzhome-demo/compose.env"
docker compose -p kzhome-demo --env-file $envFile -f compose.production.yaml -f compose.demo.yaml config --quiet
docker compose -p kzhome-demo --env-file $envFile -f compose.production.yaml -f compose.demo.yaml logs --tail 50 core mosquitto proxy
```

Секреты хранить и просматривать только локально; не включать debug/body/header logs.
Demo CA/сертификат действуют год. По истечении срока остановите демо и отдельно
переиздайте demo CA/leaf с SAN `mosquitto`, сохранив пароли; это не продление
public HTTPS. До такой процедуры для разового показа допустим **новый** demo-проект
с собственными пустыми томами, не удаляя прежний.

## 6. Linux ARM64 / Orange Pi

Нужны поддерживаемые конкретной платой 64-bit Linux/boot/storage, Docker Engine,
Compose 2.24.4+, Git и Python 3.12+. Модель платы, ОС-образ, public domain и CA
не предполагаются. Выбор RAM/SSD/питания/UPS — по
[ARM64 readiness v0.13](ARM64_DEPLOYMENT.md), с последующим native тестом.

Клонируйте те же репозитории рядом, выберите согласованные refs. Из Core:

```sh
python3 deploy/demo.py prepare --run --arm64 --web ../kz-home-web
python3 -u deploy/demo.py start --run
python3 deploy/demo.py credentials --run
python3 deploy/demo.py simulate --run --scenario automation
```

Helper добавит существующий `compose.arm64.yaml` между production и demo overlays.
Demo override оставит интерфейс на loopback HTTP без требования публичного домена;
внутренний MQTT сохранит TLS. На Windows компьютере наставника можно открыть
туннель (заменить только SSH user/адрес платы):

```powershell
ssh -L 18081:127.0.0.1:18081 -L 18080:127.0.0.1:18080 user@board-address
```

Затем открыть http://127.0.0.1:18081 локально. Не расширяйте HTTP binding на LAN.
Для отдельной реальной LAN/HTTPS установки используйте v0.13 с выбранными
сертификатами, DNS и firewall. Нативная сборка Web на малой плате может быть
медленной/не вместиться в RAM; альтернативой служат отдельно проверенные ARM64
образы из build pipeline. Проверка на реальной плате в этой работе не заявляется.

Подробные результаты developer acceptance: [DEMO_VALIDATION_V0.14.md](DEMO_VALIDATION_V0.14.md).
