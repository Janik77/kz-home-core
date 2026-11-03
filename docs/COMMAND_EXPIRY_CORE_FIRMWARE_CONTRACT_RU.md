# Core v0.22 + Firmware v0.12: контракт deadline-permit-v1

Статус: согласованный проект; экспериментальная server-side реализация описана в
[CONTROL_AUTHORITY_CORE_V0.22_RU.md](CONTROL_AUTHORITY_CORE_V0.22_RU.md).
Firmware и hardware acceptance ещё не выполнены; physical gate закрыт.
Этот документ уточняет
[первоначальный проект](COMMAND_EXPIRY_V0.22_DESIGN_RU.md) и
[Core-only foundation](COMMAND_EXPIRY_CORE_V0.22.md). До совместной приёмки
существующий physical gate остаётся закрытым.

## 1. Проверенные исходники и выбор

Core: `feature/command-expiry-core-v0.22`, HEAD `6464bd8`, существующие изменения
не закоммичены и сохранены. Firmware checkout чистый, `4382f7f`, не локальный main;
его tree `9f3bcba67de602bdff9d993465d4580169e063b9` соответствует merged v0.11.0
из предыдущего review. Удалённый main повторно проверен: `d32deaa79ff027487278e24d0414871c4a880f84`.

Повторно прочитаны Protocol v1, topic parser/Gateway, CommandService/CommandClock,
lifecycle и mentor guide; Firmware `device_mqtt`, `protocol`, `command_engine`,
`journal`, `network`, `relay`. Сейчас parser допускает 300 s; проверка freshness
выполняется **до** NVS commit, а GPIO — после него без повторной проверки.
`network_now_ms()`/SNTP не доказывают погрешность относительно Core. Core clock
foundation также не решает continuity после restart и не является time-proof.

**Рекомендация:** одно короткое разрешение на одну команду, получаемое отдельным
nonce round-trip. ESP32 не синхронизирует абсолютные часы и не получает общий
60-секундный допуск управления. Все задержки с начала запроса расходуют разрешение.
Цена — один дополнительный MQTT round-trip на новый intent.

Legacy `set` небезопасен для нового physical профиля: Firmware v0.11 игнорирует
неизвестные envelope fields и может выполнить пакет при downgrade/ошибке классификации.
Поэтому новый профиль использует **отдельный command topic**, а не только новые поля
в старом `set`. Это три новых topics под прежним root; существующие set/ACK не перегружаются.

## 2. Topics, доверие и сообщения

| Topic после `kzhome/v1/{house_id}/{device_id}/` | Направление | Назначение |
|---|---|---|
| `control/command` | Core → device | Неизменяемый кандидат; сам по себе не разрешает GPIO |
| `control/request` | device → Core | Запрос разрешения на конкретный кандидат |
| `control/reply` | Core → device | Кратковременный grant или отказ |
| `ack`, `state`, `status` | device → Core | Прежние схемы ACK/lifecycle/observed state |

Три новых topics: QoS 1, retain=false. MQTT 3.1.1 достаточно. Device ACL разрешает
read только своих command/reply, write своего request и прежних ack/state/status;
**legacy set для этого physical account не разрешается**, Firmware v0.12 его не
подписывает и не исполняет. Core подписывает `kzhome/v1/+/+/control/request`.
Нельзя просто ослабить существующий пятикомпонентный topic parser до произвольного
суффикса: нужны отдельные точные виды шестикомпонентных topics и прежние проверки IDs.
Demo/simulator ACL и его set остаются отдельными и неизменными.

TLS CA/hostname verification и per-device credentials обязательны. Источник reply
обеспечивается broker ACL, не JSON-полем sender. Core не видит broker publisher
principal: доверенный broker — явная предпосылка. Nonce не заменяет authentication
и не является hardware/firmware attestation. Компрометация Core/broker/actuator вне гарантии.

Это opt-in transport profile, не прозрачное изменение всех v1-устройств. Перед
реализацией нужно явно утвердить дополнение к Protocol §2: отдельные control topics
и обязательная expiry-проверка нового профиля. Если frozen v1 не допускает такой
профиль, потребуется отдельная major-версия **только physical adapter**, а не скрытая
смена смысла set. Namespace migration сама по себе не обеспечивает временную безопасность.

Кандидат на `control/command`:

```json
{
  "protocol_version": "v1",
  "execution_profile": "deadline-permit-v1",
  "core_epoch": "<UUID authority>",
  "command_id": "<UUID команды>",
  "correlation_id": "<UUID корреляции>",
  "timestamp": "2026-11-02T10:00:00.000000Z",
  "expires_at": "2026-11-02T10:00:30.000000Z",
  "state": {"on": true}
}
```

Creation/E — точные сохранённые значения, `0<E-created<=30 s`. Epoch/IDs/intent/E
никогда не обновляются при повторе. `intent_sha256` ниже — SHA-256 **точных UTF-8
байтов** этого опубликованного payload. Core фиксирует digest до первого publish;
повтор с другим JSON-представлением считается конфликтом, а не тем же кандидатом.
Это исключает неоднозначную межъязыковую canonicalization и не требует поля hash внутри payload.

До первой постановки request в MQTT outbox ESP32 сохраняет monotonic `m0`, текущие
boot/session и nonce. Request:

```json
{
  "control_version": 1,
  "type": "permit_request",
  "execution_profile": "deadline-permit-v1",
  "core_epoch": "<из кандидата>",
  "boot_id": "<случайные 128 бит, hex>",
  "session_id": "<новые 128 бит при каждом connect, hex>",
  "nonce": "<новые 128 бит на попытку, hex>",
  "command_id": "<из кандидата>",
  "intent_sha256": "<64 hex>"
}
```

Core проверяет topic identity, house binding, active lifecycle, разрешённый оператором
профиль, pending ledger, dispatch_started, текущую epoch и digest. Для old/terminal/
foreign/unknown rows разрешение не выдаётся. Control frames не создают intent,
не подтверждают applied, не меняют ownership/capabilities и не открывают physical gate.

В момент `c` после получения request Core снимает логическое время C и остаток до
исходного monotonic deadline. Grant на `control/reply`:

```json
{
  "control_version": 1,
  "type": "permit_grant",
  "execution_profile": "deadline-permit-v1",
  "core_epoch": "<та же epoch>",
  "boot_id": "<echo>", "session_id": "<echo>", "nonce": "<echo>",
  "command_id": "<та же команда>", "intent_sha256": "<тот же digest>",
  "core_time": "2026-11-02T10:00:01.000000Z",
  "expires_at": "2026-11-02T10:00:30.000000Z",
  "remaining_us": 2000000
}
```

`R=remaining_us` — целое число, округлённое вниз с запасом разрешения timer:
`0<R<=min(E-C, P_MAX)`. **P_MAX=2 000 000 µs** — выбранный верхний срок одного permit,
не утверждение о величине MQTT RTT или точности часов. C/E сериализуются без потери
точности сохранённого E; расчёт использует monotonic deadline, а не повторный wall time.
Поздняя отправка grant не обновляет C/R. Получение grant не запускает новый timer R.
Отказ — `type=permit_denied`, те же binding fields, фиксированный reason
`expired`/`unavailable`/`invalid`; без новых lifecycle ACK statuses или приватных данных.

Frames не более 4 KiB, UTF-8 object, duplicate JSON keys запрещены; exact types,
идентификаторы и hex длины проверяются. Retained/unsolicited/foreign/несовпадающие
replies отбрасываются. Один outstanding request на устройство, ожидание не более
2 s monotonic; не более одного нового запроса в секунду на identity, глобальный
bounded limiter/cache Core. Дубликат той же session/nonce возвращает только прежние
байты grant либо отказ, никогда пересчитанный R. Nonce/session не переиспользуются.
После timeout нет автоматической новой попытки/нового m0 для того же кандидата.

## 3. Консервативная временная проверка

Пусть реальные rates monotonic clocks имеют подтверждённые bounds:

```text
Core:   1-rho_C <= rate_C <= 1+rho_C
ESP32:  1-rho_D <= rate_D <= 1+rho_D
kappa = (1+rho_C)/(1-rho_D)
d = m_now - m0
B_C = (1+rho_C) * G_max
Разрешение GPIO: kappa*d + epsilon + B_C < R
```

Все величины приведены к microseconds; вычисления fixed-point с округлением **вверх**
слева, проверкой overflow и отрицательных elapsed. `epsilon` — суммарная верхняя
граница timer sampling/quantization/serialization/арифметики. `G_max` — реальное
максимальное время от последнего чтения timer до GPIO-register write.

Обоснование: request начинается раньше серверного sample C; значит верхняя граница
Core-времени GPIO — `C+kappa*d+epsilon+B_C`. Она строго меньше `C+R<=E`.
ESP32 не сравнивает E с собственным UTC. Никакой симметрии сети не предполагается.

В d включены **полный MQTT RTT**, outbox, Core scheduling, очередь reply, parsing,
планирование worker и NVS commit. Ограничивать NVS «на глаз» не нужно: после любого
stall timer читается заново, и израсходованный бюджет даёт отказ. Watchdog/reboot
после stall не повторяет pending. Сетевая/очередная задержка уменьшает доступность,
но не увеличивает срок. Отдельный произвольный tolerance +30 s запрещён.

После durable pending выполняется финальная timer/generation/permit проверка внутри
проверенного bounded GPIO primitive. В этом участке нет allocation, NVS, логов,
MQTT или ожидания RTOS locks. Нужны поддерживаемый IRAM/cache-safe путь, короткая
critical section и обоснованная граница interrupt/preemption latency для обеих плат.
Обычный вызов нынешнего `relay_apply()` после проверки в worker такой границы не доказывает.

Численные rho/epsilon/G **сейчас не установлены**. Например, 1000 ppm для каждого
clock, epsilon=2 ms и G=1 ms дают kappa≈1.002002, но это лишь расчётный пример,
не принятые параметры платы. Для release нужны ограничения hardware/timer/power modes,
code-path audit и fault-injection/trace; наблюдённый максимум измерений не равен WCET.
Если finite G или timer bounds обосновать нельзя, physical enablement запрещён;
для более сильной гарантии понадобится аппаратный interlock/другой исполнитель.

## 4. Firmware state machine и journal

1. При boot GPIO инициализируется в безопасное OFF по прежней policy; journal
   загружается до MQTT. Нет восстановления ON из записи. Boot/session/nonce новые.
2. Кандидат без обязательных profile/E/epoch либо из legacy set не исполняется.
   Terminal journal duplicate возвращает прежний ACK без GPIO и без нового permit.
   Тот же ID с иным digest — конфликт; текущий state не подменяется историческим ON.
3. Новый кандидат фиксируется как единственный in-flight **в RAM**, отправляется request.
   Worker — неблокирующая state machine: нельзя ждать reply внутри обработчика,
   который сам должен извлекать reply из очереди. QoS duplicate не меняет m0.
4. Совпавший grant принимается однократно только в текущих boot/session/generation.
   Проверяются E/digest/epoch/R, начальный бюджет; затем durable pending NVS.
5. После NVS — финальная проверка из §3 и максимум одна GPIO-запись. Если поздно,
   reserved pending становится durable terminal rejected/timeout без GPIO.
6. После GPIO — durable applied/failed, затем прежние ACK и actual state. Потеря
   питания/commit даёт unknown execution; pending при reboot → failed без replay.
   Без proof/при timeout — rejected, если это можно сохранить; при unhealthy/full NVS
   ноль GPIO, возможен пропуск ACK. Прежние error codes `timeout`/`protocol_error` достаточны.

Fingerprint включает digest/profile/epoch/E. Нужен versioned NVS upgrade с сохранением
старых identity bindings и tombstones; нельзя менять raw struct и стирать commands
partition для обхода ошибки. Неизвестная/corrupt версия блокирует управление.
600 s dedup retention сохраняется; после reboot минимум retention отсчитывается
заново по monotonic, если старое время нельзя доказать. Early eviction запрещён.
После eviction повтор старого пакета всё равно требует **нового** permit; Core
не выдаёт его terminal/old-epoch команде. Replay grant не совпадёт с новым nonce/session.

Reconnect, clock discontinuity, sleep с неподтверждённым timer или новая generation
инвалидируют все RAM permits и in-flight requests. Смена session не продолжает старый
бюджет. Автоматический повтор intent не добавляется. SNTP остаётся bootstrap для
TLS/date/reporting; не разрешает GPIO и не заменяет permit. Проверки CA/hostname/date
не отключаются ради получения control reply; интернет не нужен.

## 5. Core epoch, restart и lifecycle

Одна physical authority на установку; multiworker/HA пока не разрешены. Новый UUID
epoch при каждом старте authority. Новые physical rows сохраняют profile, epoch,
wire digest вместе с неизменными timestamps **до command publish**. In-memory
monotonic deadline выводится из первоначального creation и сохранённого E; не now+30
при request/reconnect. Grant cache может быть volatile: после restart старой epoch
разрешения не перевыдавать. Existing rows без profile/epoch не получают их задним числом.

Для новых rows нужна также подтверждённая startup-health UTC anchor Core: локальный
доверенный time source/проверенная host time policy, без обязательного интернета.
Nonce-обмен не делает ошибочный boot UTC правильным. Текущий foundation этой health
не доказывает. Если состояние времени неизвестно, новые physical rows/grants запрещены,
даже когда monotonic counter исправен и recovery fence уже завершён.

Скачок UTC закрывает новые physical commands/grants. Уже выданный permit нельзя
считать отозванным мгновенно: он остаётся ограничен R. Deadline/timeout существующих
команд продолжают проверяться исходным monotonic counter, **не скачнувшим wall clock**.
Текущий foundation provider, бросающий ошибку целиком, для этого потребуется разделить
на admission-health и чтение исходной monotonic expiry. Не восстанавливать health автоматически.

После restart старый monotonic deadline неизвестен. Нельзя ни выдавать grant на
старый row, ни сразу материализовать timeout из нового wall time. Выбран простой
recovery fence: запрет physical admission/grants и finalization старых pending до
доказанного истечения максимального исходного lifetime 30 s после старта нового
процесса. При подтверждённом rate_C достаточно:

```text
H_real >= 30 s/(1-rho_C) + recovery_error_bound
F_core_ticks >= (1+rho_C)*H_real   (округление вверх)
```

Старые команды созданы до этого старта; fence покрывает и их E, и P_MAX. Новый
restart обнуляет ожидание и продлевает запрет. В fence old receipts возвращают
503/unknown observation, не applied/timed_out по новой шкале. Затем pending старой
epoch → timed_out/unknown execution; terminal receipts не меняются. Late state
остаётся observed state, ACK не восстанавливает old pending в applied.

Нужен qualified continuous clock, учитывающий suspend, либо запрет suspend для
physical authority. Linux CLOCK_BOOTTIME — кандидат для проверки; нынешний Python
monotonic нельзя без проверки считать достаточным при suspend. При отсутствии bounds
или обнаружении clock fault/suspend anomaly fence не завершается разрешением управления.
Reboot ESP32 запрещает старый grant независимо от Core. Это заменяет невозможное
обещание «Core restart немедленно отменяет permit на отключённом устройстве».

ACK accepted/applied/rejected/failed, correlation и правило ACK+live state до E
сохраняются. GPIO до E с ACK после E оставляет timed_out/unknown execution.
Publish failure/deactivate/revoke не являются доставленной командой отмены:
новые grants запрещаются, уже выданный может завершиться в коротком оставшемся окне.
Срок ограничивает command-driven GPIO write, не длительность ON/механические контакты;
инициализация OFF при boot — отдельная safety policy, не повтор команды.

## 6. Точный порядок реализации и совместимость

1. Утвердить этот профиль, Protocol §2, новые topics и модель bounds/clock recovery.
   Physical gate Core foundation остаётся закрыт. Старый nonce ack/set вариант не реализовывать.
2. Core: явная migration для отдельной связанной control table; исторические ledger
   rows не изменяются и не получают profile/epoch/digest,
   authority/recovery fence, точный control parser, grant checks/cache/limiter,
   общий monotonic deadline и тесты. Ни один control packet не открывает gate.
   Начальный physical путь — только durable `/v1/.../commands`; legacy/scenes/
   automations physical остаются denied, пока им не спроектирован durable admission.
3. Firmware: strict новый command topic, control worker state machine, timer/bounded
   GPIO primitive, versioned journal и fault tests. Обе сборки classic/C6. Legacy
   set и команда без E не имеют fallback. Старый Core с v0.12 управлять не сможет.
4. В isolated fixture обновить **профильные**, не demo, ACL и проверить старый firmware:
   новые команды на новом topic им не исполняются. Старые set sessions/queues изолировать
   в отдельном operator commissioning; текущие installations этой работой не менять.
5. Доказать bounds для разрешённой платы/сборки/host, пройти матрицу ниже и совместный
   review. Только после этого отдельным изменением разрешить точные physical identities
   через trusted operator policy. Версия из status, nonce echo или software allowlist
   никогда не заменяют совместную приёмку. Автоматического enablement не добавлять.

Web v0.19 не меняется: прежние receipt/status/failure_code и 409/503 сохраняются.
У наставника v0.20+software demo остаются прежними; обновлять их сейчас не требуется.
Software profile сохраняет legacy topics, отдельно от нового physical adapter.
Если будущий откат Core не поддерживает профиль — physical control остаётся denied,
а не включается legacy fallback. Ни одной операции deploy/flash/ACL update сейчас нет.

## 7. Матрица приёмки

| Проверка | Ожидаемый результат / уровень |
|---|---|
| До E, достаточный R; ON и OFF | Один GPIO, terminal NVS, обычные ACK/state; host + обе платы |
| Первая доставка при E, E+1 µs, 60 s | Core не выдаёт grant, ноль GPIO; joint fake clock + broker delay |
| Reply задержан/асимметричный RTT/долгий outbox | m0 не меняется, бюджет расходуется, при недостатке ноль GPIO |
| Worker/парсинг/NVS stall до финальной проверки | Обновлённый d запрещает позднюю запись; fault injection |
| kappa*d+epsilon+B_C равно R / больше R | Ноль GPIO; fixed-point границы, overflow, rounding |
| После финальной проверки preemption/cache/IRQ fault | Доказанный G соблюдён либо профиль не допускается; IRAM audit + trace |
| Нет E/policy/epoch/proof; неправильный type/duplicate key | Ноль GPIO, нет fallback; parser fuzz/bounds |
| Повтор candidate/request/reply; иной digest с тем же ID | Нет второго GPIO/нового m0; прежний grant/ACK либо конфликт |
| Foreign/retained/stale nonce, иной boot/session/topic | Отказ; RBAC/identity/ACL negative tests |
| GPIO до E, поздний ACK/state; обратный порядок evidence | Прежний lifecycle, late evidence не делает applied |
| Reboot до reserve / между reserve и GPIO / после GPIO до finish | Новый nonce, pending→failed, ноль replay; NVS power-cut tests |
| Journal full/corrupt/upgrade старых записей/eviction | Ноль unsafe GPIO, нет auto-erase, old IDs не получают новый grant |
| Reconnect/потеря TIME_SYNC/sleep/timer rollback | Permit инвалидирован, session новая, old grant не применим |
| Core restart с reply в пути и недоставленным candidate | Old epoch grant не перевыдаётся; fence; old packet не продлевает E |
| Boot UTC вперёд/назад, UTC step при действующем grant | Нельзя преждевременно timed_out/включить управление из нового wall time |
| Suspend/repeated restart/два authority одновременно | Fence/qualified clock; второй authority запрещён; без доказательства deny |
| v0.11 firmware/downgrade + новый physical topic | Не исполняет; новые credentials не дают legacy set access |
| Core regression, Web v0.19, mentor software demo | Прежние HTTP/ACL software semantics; физическая изоляция сохранена |

Gate для приёмки: unit/fake-clock и parser/NVS tests, isolated MQTT/PostgreSQL,
обе IDF builds, затем hardware timing/fault traces на обеих платах. Одних 526 Core
тестов предыдущего этапа недостаточно для разрешения physical profile.

В этой работе добавлен только этот документ. Runtime, прежние незакоммиченные
изменения, Firmware/Web, private inputs и окружение наставника не изменены.
Новые tests/builds/live acceptance не запускались; никаких commits/PR.
