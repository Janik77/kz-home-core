# Единый срок MQTT-команды: Core v0.22 → Firmware v0.12

Статус: исходное предложение, 02.11.2026; **не утверждённый wire-контракт**.
Итог критического review и реализованный Core-only этап описаны в
[COMMAND_EXPIRY_CORE_V0.22.md](COMMAND_EXPIRY_CORE_V0.22.md). Они имеют приоритет
над предложениями ниже: ack/set не перегружены, time-proof/physical enablement
не реализованы, новая миграция не нужна. Исходный текст сохранён для review.
Область: relay `on_off`, обе платы ESP32, Device Protocol v1; без облака,
нового MQTT namespace, изменения прав людей или автоматического commissioning.

## 1. Проверенный baseline и обнаруженная проблема

| Репозиторий | Фактическое состояние перед работой |
|---|---|
| Core | чистый `main`, `6464bd88cdefa30c625e12e1d788a93e16c5c889` (v0.21) |
| Firmware | чистая `feature/mqtt-compat-v0.11.0`, `4382f7f0d4d1f7fc5c5cbf7d9f0cb84192ac06ff`; локальный main ещё `ed6ebab` |
| Web | чистый HEAD `809266f5c0e44f2a2b591c494c62b692429fbce0` (v0.19), только чтение |

Удалённый Firmware main проверен через `git ls-remote`: `d32deaa79ff027487278e24d0414871c4a880f84`.
GitHub Git API для этого [merge-коммита](https://github.com/Janik77/kz-home-firmware/commit/d32deaa79ff027487278e24d0414871c4a880f84)
возвращает tree `9f3bcba67de602bdff9d993465d4580169e063b9`, равный локальному
`4382f7f^{tree}`. Таким образом, прочитанные исходники соответствуют merged v0.11.0.
Checkout/fetch/merge и изменения Firmware/Web не выполнялись.

Прочитаны:

- Core: [DEVICE_PROTOCOL](../DEVICE_PROTOCOL.md), [command lifecycle](../COMMAND_LIFECYCLE_V0.15.md),
  `app/services/command_service.py`, `device_service.py`, `state_validator.py`,
  `app/transports/{base,mqtt,mqtt_models,mqtt_client}.py`, command ORM/API и тесты lifecycle/MQTT.
- Firmware: `kz_relay/main/{device_mqtt,protocol,command_engine,journal,network}.c`,
  соответствующие headers; `tests/{test_protocol,test_mqtt,test_core_compat}`.
- Web: `src/data/core/relay-control.ts`; Core: `app/demo_relay.py`,
  `simulator/mqtt_relay.py`, [инструкция наставника](../deploy/MENTOR_DEMO_RU.md),
  [safe upgrade v0.20](DEMO_UPGRADE_V0.20.md).

Факты реализации:

- Core сохраняет `expires_at = created_at + 30 s` до публикации; publish await ограничен 5 s.
  Timeout материализуется лениво при GET, matching ingress или следующем submit,
  а не точным фоновым таймером. После deadline ACK/state не делают команду `applied`.
- `applied` требует matching ACK applied **и** live correlated state нужного boolean
  до deadline. Late state всё ещё может обновлять observed Device; terminal receipt неизменяем.
- Gateway сейчас создаёт `timestamp=utc_now()` при отправке; сохранённый deadline
  в MQTT отсутствует. Legacy routes/automations тоже вызывают transport, обходя ledger API.
- Firmware принимает возраст до 300 s включительно и future skew до 30 s;
  parser пропускает неизвестную envelope metadata. В `test_protocol.c` уже есть
  воспроизведение: первая доставка ON через 60 s приводит к GPIO.
- Worker сериализует обработку, очередь ограничена четырьмя сообщениями, проверяет
  connection generation. MQTT 3.1.1, clean session, QoS 1; `set` не retained.
  Эти меры не устраняют задержки в сети/очереди и повторную доставку.
- NVS journal: 128 slots, pending до GPIO, terminal после; pending при reboot
  становится failed без повторного GPIO. Дедупликация 600 s; проверка дубликата
  предшествует freshness. Сейчас fingerprint не включает срок, которого ещё нет.
- ESP32 ждёт SNTP, использует monotonic anchor, journal clock floor и блокирует
  скачок wall time >2 s. Это защита от части сбоев, **не доказательство** погрешности
  относительно Core; SNTP event сам по себе не подтверждает источник/точность.

## 2. Рекомендуемый контракт

Добавить явно согласуемое расширение v1 `deadline-v1`. Namespace остаётся
`kzhome/v1/{house_id}/{device_id}/{set,ack,state,status}`; directional ACL/TLS сохраняются.
Это wire-addition, но **не** безопасная прозрачная совместимость со старым actuator.

Для исполняемой команды обязательны:

```json
{
  "protocol_version": "v1",
  "command_id": "<неизменяемый ID>",
  "correlation_id": "<неизменяемый ID>",
  "timestamp": "2026-11-02T10:00:00.000Z",
  "expires_at": "2026-11-02T10:00:30.000Z",
  "command_policy": "deadline-v1",
  "time_epoch": "<эпоха authority Core>",
  "state": {"on": true}
}
```

`timestamp` — исходное время создания, `E=expires_at` — исключительная граница:
при времени `>= E` новый GPIO запрещён. Для ledger API обе величины берутся из
одной сохранённой записи; `0 < E-timestamp <= 30 s`, штатно ровно 30 s.
Задержка до publish расходует этот бюджет. Retry/reconnect не обновляет timestamp,
E, epoch, IDs или intent. Нет автоматического resend и нового ID ради retry.
Legacy/automation dispatch получает такой же бюджет на центральном transport-пути;
его ответ остаётся dispatch-only, без выдуманного receipt `applied`.

Срок ограничивает **новую запись GPIO**, а не длительность ON и не время доставки ACK.
Истечение не означает OFF/отмену ранее выполненного ON. Механическое срабатывание
контактов после записи GPIO и безопасность нагрузки требуют отдельной модели.

### Подтверждение времени без доверия к wall clock ESP32

Рекомендуется bounded round-trip к той же authority Core. Нужны два новых явно
типизированных control frame, не являющихся relay-командой или lifecycle ACK:

- устройство → свой `ack`: `message_type=time_probe`, версия расширения, свежий
  случайный nonce, boot/session ID; локально сохраняются monotonic `m0` и generation;
- Core → его `set`: `message_type=time_reply`, те же nonce/session, `time_epoch`,
  `core_time=C`, снятый после получения probe и до публикации ответа.

У reply нет `state` и command IDs: старый firmware не должен принять его за intent.
Core отделяет probe до обычной ACK validation; firmware отделяет reply до command
parser. Оба frame non-retained, QoS 1, ограничены по размеру/частоте. Проверяются
существующие house/device binding, active lifecycle и точный topic. Никакой регистрации,
активации, смены профиля или предоставления прав через probe. Запросы до activation запрещены.
Nonce принимается один раз только для текущего boot/session/generation и короткого
ожидания; retained, unsolicited, stale/replayed/foreign replies отбрасываются.
Broker остаётся доверенной стороной: TLS с проверкой CA/hostname и строгие ACL
обеспечивают источник ответа; компрометация broker/Core вне этой гарантии.

Пусть ответ получен при `m1`, а текущий monotonic tick — `m`. Если измеренная
скорость monotonic отклоняется от authority не более чем на `rho < 1`, то интервал:

```text
L(m) = C + (m-m1)/(1+rho) - epsilon
U(m) = C + (m-m0)/(1-rho) + epsilon
```

`epsilon` покрывает разрешение/округление и погрешность authority, без двойного
округления в опасную сторону. Сетевая задержка включена в round-trip, симметрия
маршрута не предполагается. Долгий RTT ухудшает доступность, но не продлевает срок.
Начальные policy limits для реализации: RTT <=2 s, anchor lease <=60 s;
`rho`, `epsilon` и запас GPIO `B` должны иметь обоснованные верхние границы для
обеих плат/сборок. Произвольная константа «часы точны до 2 секунд» неприемлема.
Если границы не подтверждены, управление остаётся запрещённым — это release blocker.

Firmware разрешает новый GPIO только если корректны policy/epoch/anchor, journal,
intent и **`U(m)+B < E`**. Проверка выполняется после durable pending commit,
непосредственно перед bounded GPIO primitive. Между последней проверкой и GPIO
нельзя иметь неограниченное ожидание/планирование. Проверка только при получении
MQTT или перед NVS недостаточна. Задержка NVS, очередь или вытеснение расходуют бюджет.
При неопределённом времени нет ожидания «пока синхронизируемся и выполним старое»:
команда отклоняется/сбрасывается без GPIO; повтор доставки не получает новый срок.

Boot/reconnect/смена generation инвалидируют anchor; повторный обмен обязателен.
SNTP остаётся локальным bootstrap для TLS/date checks; один SNTP event не разрешает
GPIO. Проверку сертификатов не отключать ради получения time_reply. Интернет не нужен:
Core и локальный time source для TLS могут находиться в LAN.

### Authority Core и границы гарантии

Нужен единый clock provider для создания, публикации, evidence и timeout:
непрерывная monotonic-anchored шкала с UTC-представлением. Нельзя слать E по одной
шкале, а истекать по независимо скачнувшему `datetime.now()`.
Core переводит pending в `timed_out` только когда его достоверная нижняя граница
времени достигла E. При этом firmware уже не может разрешить новый GPIO через U.

Clock discontinuity, потеря достоверности и Core restart требуют отдельной recovery
policy: блокировать новые команды/time replies, не объявлять старые команды timed_out
раньше доказанного истечения в исходной шкале; новая authority получает новую epoch.
Восстановление из UTC возможно только с подтверждённой границей ошибки. Иначе нужен
консервативный monotonic recovery fence, покрывающий максимальный оставшийся срок
всех ранее отправленных команд (30 s плюс погрешности), прежде чем фиксировать timeout.
Нельзя просто применить скачок wall time вперёд к pending ledger. В degraded recovery
GET может временно возвращать недоступность; Web покажет ошибку, не ложный успех.
Для нескольких процессов/инстансов нужна одна authority/epoch; независимые часы
workers без согласования не соответствуют контракту.

Гарантия условна на корректных Core/firmware/broker и доказанных bounds времени/GPIO.
При отсутствии этих предпосылок безопасный результат — отказ управления. Произвольный
OS stall внутри GPIO critical section или компрометированный actuator нельзя исправить JSON-полем.

## 3. Дубликаты, reboot и подтверждения

| Ситуация | Обязательное поведение |
|---|---|
| Первая доставка с достаточным бюджетом | Проверка, durable pending, повторная проверка времени, один GPIO, terminal journal, ACK/state |
| Доставка при E или позже / слишком близко с учётом U+B | Ноль GPIO; durable rejected + `error_code=timeout`, если journal доступен |
| Повтор той же команды, в том числе после E | Ноль нового GPIO; прежний terminal ACK допустим как исторический исход |
| Тот же ID, иной E/epoch/timestamp/correlation/state | Конфликт, ноль GPIO; не наследовать успешный ACK другого intent |
| ESP32 reboot | Anchor заново; pending journal → failed, без replay; terminal запись не восстанавливает ON |
| Нет достоверного anchor, испорчен/полон journal | Ноль GPIO; отклонение, либо отсутствие ACK, если нельзя гарантировать terminal запись |
| Reconnect / задержка worker / повтор брокера | Исходный E неизменен; старая generation отбрасывается, новая проверяется заново |
| GPIO до E, ACK или state пришли после E | Core остаётся timed_out/unknown execution; late evidence не делает applied |

Journal fingerprint включает новые поля. Сохранить 600 s retention, запрет auto-erase
и повторного выполнения pending. Reclaim должен учитывать исходный E и подтверждённое
время; eviction никогда не делает просроченный пакет исполнимым. Нужна явная версия
NVS record и проверенный upgrade v1→новый формат: старые terminal/pending сохраняются
как запрет исполнения соответствующего ID. Не менять размер raw struct и не стирать
раздел для обхода ошибки. Повреждённая/непонятная версия блокирует GPIO.

Core сохраняет нынешнее правило ACK+state до E и неизменность terminal receipt.
Дубликат applied ACK после reboot — исторический исход, не утверждение текущего ON;
state публикуется фактический. `failed/publish_unconfirmed` по-прежнему unknown execution:
publish timeout 5 s не отменяет ещё действующую 30-секундную команду.

## 4. Совместимость и порядок внедрения

| Пара | Policy |
|---|---|
| Core v0.22 + проверенный Firmware v0.12 | `deadline-v1`, ручное разрешение конкретного physical identity, успешный probe |
| Core v0.22 + старый/неизвестный physical firmware | Запрет control до обновления и проверки; read/state/status сохраняются |
| Старый Core + Firmware v0.12 | Команды без полного контракта отклоняются; никакого fallback на 300 s |
| Core v0.20 у наставника + прежний demo relay | Оставить установленную пару как есть; гарантия нового контракта к ней не относится |
| Web v0.19 + Core v0.22 | HTTP paths/status/receipt/failure_code остаются прежними; blocked control → существующая HTTP ошибка |

Не определять поддержку по версии из произвольной MQTT metadata. Использовать
доверенную per-device operator policy плюс live probe; `hardware_model` остаётся
идентичностью платы, не capability negotiation. Unknown/unclassified MQTT actuator
не получает legacy exemption лишь потому, что отсутствует в physical inventory.
Все пути выдачи команд (новый API, legacy on/off/state, automations/scenes) проходят
один gate; human RBAC, house isolation, active lifecycle и ACL не ослабляются.
Нет новых HTTP status/failure_code enum: Web v0.19 отвергает неизвестные failure_code.
Причины отсутствия policy/anchor возвращать существующим 409/503 до publish;
device timeout ACK отображается как существующий `failed/device_rejected`.

Последовательность реализации и отдельного обновления:

1. **Core v0.22:** зафиксировать расширение/clock invariants; добавить immutable
   timestamp/E/epoch через service→Transport→Gateway и единый control gate, probe/reply,
   rate limits, clock recovery, тесты. Новым ledger rows нужна сохраняемая epoch
   (nullable колонка для исторических записей, явная Alembic migration); старые rows
   не публиковать повторно и не выдумывать им policy. Operator mapping хранить явно,
   без секретов и автоматического доверия к status. Physical control по умолчанию denied.
2. **Firmware v0.12:** strict parser, probe/interval/lease, финальная проверка GPIO,
   journal upgrade/fingerprint, worker/reboot tests; сборки classic ESP32 и C6.
   Совместный fixture использует Core v0.22; standalone old Core не включает управление.
3. Обновить software relay в новом Core candidate: strict E check и demo regression
   apply/fail/timeout/restart. Его volatile dedup не считать физической гарантией.
   Если нужен прежний demo mode, разрешать только явно помеченный trusted software ID
   в изолированной demo-конфигурации, не глобальный legacy fallback для устройств.
4. В отдельном тестовом окружении доказать полный контракт; затем предложить оператору
   согласованное окно обновления Core→Firmware с временно заблокированным control.
   По одному устройству проверить плату/версию/NVS/clock bounds, activation/ACL и разрешить policy.
   До смены firmware физический actuator изолировать по согласованной operator-процедуре:
   запрет новых publish не отзывает уже отправленные старые пакеты. Учесть broker sessions,
   очереди/outbox и retained set; один sleep 300 s при недостоверных старых часах недостаточен.
   После обновления v0.12 такие legacy пакеты отклоняются. Старые ledger rows и переходное
   окно не получают задним числом новую гарантию; она начинается после строгого commissioning.
5. Наставнику немедленное обновление не требуется. Его volumes, secrets, compose,
   broker и запущенные images не трогать. Будущее обновление — отдельная согласованная
   процедура safe upgrade/backup; rollback старого Core при оставленном v0.12 означает
   отказ команд, не автоматический downgrade защиты firmware.

В реализации потребуется актуализировать DEVICE_PROTOCOL (в том числе устаревшее
общее утверждение о retry), lifecycle docs, commissioning и mentor guide. Не включать
автоматический retry/cancellation, LWT fixes, OTA или новый broker credential authority.

## 5. Альтернативы и риски

- **Только сократить firmware age 300→30 s:** недостаточно; timestamp Gateway отличается
  от creation, есть clock skew, время NVS/очереди, legacy bypass и повторный publish.
- **Только expires_at + обычный SNTP:** меньше изменений, но безопасно лишь с независимо
  подтверждённой границей clock error и доверенным локальным источником. Текущая реализация
  этого не обеспечивает. Это допустимая будущая замена probe при доказанной эквивалентности.
- **TTL от получения:** опасно, даёт задержанной команде новый бюджет. Не выбирать.
- **MQTT 5 Message Expiry:** полезная дополнительная фильтрация брокером, но не заменяет
  проверку перед GPIO и журнал; сейчас firmware MQTT 3.1.1. Не требовать для v0.22/v0.12.
- **Cancel или online permission на каждое действие:** cancel может опоздать, а permission
  сам задержаться; остаётся задача срока действия разрешения. Больше состояний без решения
  clock bounds. Отдельный v2 namespace сам по себе также не решает время.

Цена рекомендуемого решения — дополнительный короткий обмен, меньшая доступность
при плохой сети/clock fault и временный запрет старых физических устройств. Это явно
видимые отказы вместо потенциально позднего выполнения. Нельзя обещать строгую
гарантию, оставив активным любой незащищённый путь публикации или legacy firmware.

## 6. Приёмка

1. Fake-clock Core проверяет неизменные timestamp/E/epoch в БД и wire, задержку
   commit→publish, idempotency, restart/no replay, равенство now=E и оба порядка ACK/state.
   Старые API/automation paths не обходят gate. Expiry не обновляется даже при QoS duplicate.
2. Parser тестирует отсутствие/null/type/duplicate-key/неверный UTC E, E<=timestamp,
   lifetime>30 s, неизвестную policy/epoch; каждый такой intent даёт ноль GPIO.
3. Interval tests: asymmetric RTT, stale/foreign/replayed nonce, retained replies,
   RTT>2 s, lease expiry, drift обоих знаков, rounding/overflow, SNTP без probe,
   wall-clock jumps Core/ESP32, authority restart и recovery fence. Неподтверждённые
   bounds не включают control. Time frames никогда не меняют relay или lifecycle receipt.
4. Firmware boundary tests: U+B=E и U+B>E → ноль GPIO; уверенно до E → ровно один.
   Инъекции задержек очереди/NVS/планировщика между проверками не допускают поздний GPIO.
   Зафиксировать доказанные rho/epsilon/B для обеих сборок, а не только средние измерения.
5. Journal tests: QoS duplicate до/после E, изменённый E с тем же ID, reboot до/после
   GPIO/commit, upgrade старых записей, заполнение, corruption и eviction. Ни одного replay;
   downgrade firmware не является разрешённым способом снять защиту.
6. Core tests: late ACK/state не меняют terminal receipt, observed state остаётся
   честным; rejected timeout не становится applied. Сохранены RBAC/house isolation,
   physical lifecycle, directional ACL/TLS и отсутствие выдачи credentials.
7. Изолированный broker fixture удерживает **первую** доставку ON/OFF до E, E+1 ms
   и 60 s, включая reconnect, перезапуск Core/ESP32 и задержку ACK. Проверяется счётчик
   реального вызова GPIO/его аппаратный trace, не только статус HTTP. Вовремя выполненная
   команда с late ACK остаётся timed_out; задержанная ни разу не меняет GPIO.
8. Полные Core API/MQTT/lifecycle/schema/demo suites, firmware host suites и обе IDF
   builds; Web v0.19 contract regression без изменения UI. Live/hardware acceptance
   отдельно разрешается в изолированном окружении, никогда на установке наставника.

В этой работе выполнены только чтение/анализ и проверка diff документа. Runtime-тесты,
сборки, миграции, flashing и hardware acceptance **не запускались**. Документ готов
к техническому review; реализация и доказательство timing bounds ещё впереди.
