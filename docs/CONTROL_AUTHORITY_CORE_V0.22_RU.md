# Core v0.22 — серверная control authority

Статус: серверный компонент реализован для review и изолированных совместных тестов.
**Physical gate закрыт. Firmware v0.12, аппаратные bounds и production enablement
не реализованы и не подтверждены.** Ветка `feature/command-expiry-core-v0.22`,
база `6464bd8`; предыдущие незакоммиченные изменения и проектные документы сохранены.

## Протокол и admission

Реализованы точные шестикомпонентные topics под прежним `kzhome/v1`:
`{house}/{device}/control/command`, `control/request`, `control/reply`.
Все QoS 1/non-retained. Core принимает только request; inbound command/reply,
retained requests, extra path components и wildcard identity отвергаются.
Legacy set/ACK/status schemas и HTTP receipt/status/failure_code не меняются.

`PermitRequest` имеет strict types, canonical UUID, lowercase hex boot/session/nonce
по 128 бит, digest SHA-256, literal version/profile/type. Limit 4 KiB; duplicate
JSON keys, неизвестные поля и недопустимый UTF-8 запрещены. Transport limiter
работает перед DB: один request/identity/секунду, 100 frames/секунду всего,
bounded identity map. В authority есть отдельные issuance/capacity limits.

DB-backed identity admission проверяет точный house/device, physical inventory,
active lifecycle и согласованность location. Дополнительно обязателен отдельный
gate. **Штатный gate всегда отказывает**; clock не qualified и utc-health=false.
Нет HTTP/env/config-флага, который включает выдачу permit. Software allowlist,
hardware model, версия из status или содержимое control packet gate не открывают.
API, legacy, scenes и automations сохраняют прежний запрет physical control.

`ControlAuthority.prepare(session, transient_row)` — trusted internal integration
point для нового durable intent: ledger + точные байты candidate/profile/epoch/hash
сохраняются одной транзакцией. Ledger flush выполняется до FK child insert, но
commit общий. Старую уже сохранённую ledger row принять этим методом нельзя.
Действующий HTTP API этот метод для physical не вызывает.

`MQTTGateway.send_control_command` публикует только заранее подготовленный candidate
текущей authority, в исходном validity interval. До I/O фиксируется dispatch_started;
после отмены/ошибки публикации явный повтор кандидата запрещён. Авто-replay нет.

Request сверяется с command ID, exact-byte digest, immutable RAM snapshot ledger,
epoch и original monotonic deadline. Срок не выводится заново из момента request.
Grant содержит echo binding fields, core_time, исходный expires_at и remaining_us:
`min(original_remaining, 2_000_000)-1`, с консервативным запасом одного timer tick.
Это вычислительная часть контракта, **не доказательство реальных clock/GPIO bounds**.

Один command ID получает максимум один durable permit binding. SHA binding
связывает house/device/epoch/boot/session/nonce; связь с command/hash задаёт проверенная
immutable control row. UNIQUE constraint запрещает повтор того же nonce-context
для другой команды. CAS update защищает одновременные попытки даже без FOR UPDATE
в SQLite. Binding commit происходит до reply publication.

Дубликат получает только прежние байты grant, пока тот действителен. Нет cache —
отказ, а не новый R. Замена boot/session/nonce/digest после выдачи запрещена.
Перед publication вновь проверяются timing/epoch/transport и срок grant; задержка
DB/reply не продлевает permit. Из-за уже начатого сетевого I/O сообщение всё ещё
может прийти поздно: будущий Firmware обязан считать полный elapsed с исходного m0.
Отказы возвращают фиксированный `permit_denied/reason=unavailable`, без различения
существования приватных rows. Непрошедшая topic identity/validation может быть отброшена.

## Recovery и lifecycle

При старте authority берётся bounded read-only snapshot старых pending control IDs.
Recovery fence начинается **после** snapshot. При условно подтверждённых rate bounds
он покрывает 30 s lifetime и recovery error с округлением вверх. Неизвестные bounds,
неподтверждённый UTC, незавершённый fence или timer fault не допускают новых grants.

Старая epoch никогда не получает новый permit. До fence её receipt не считается
достоверно expired. После fence можно timed_out только IDs, входившие в startup
snapshot. Чужая команда, появившаяся позже, остаётся unavailable: её нельзя
преждевременно просрочить просто из-за отличающейся epoch. Новый restart создаёт
новую epoch/snapshot/fence, ничего не переиздаёт и не восстанавливает RAM grant.

Wall divergence защёлкивает admission fault. Original monotonic counter остаётся
доступным для expiry/evidence; существующий deadline не переносится на новый wall.
Timer rollback/невалидный counter защёлкивает отказ timing целиком. Health/fence
сами по себе physical gate не открывают. Startup snapshot также не является
механизмом leader election: production multiworker/HA и qualified suspend-aware
clock остаются отдельными блокерами перед enablement.

Для control rows ACK applied и correlated state не принимаются как completion
evidence до durable permit binding. Accepted/negative ACK сохраняют прежний смысл.
Дальше нужны те же ACK applied + live matching boolean state до исходного deadline;
late evidence не изменяет terminal receipt. Timestamps control evidence берутся
из original authority clock, не software clock нового процесса. Wire/nonce/digest
поля не добавлены в HTTP CommandRead. Software command lifecycle не меняется.

Timing windows (4096) не теряют unresolved pending evidence; при capacity
освобождаются только terminal rows, иначе admission отказывает. Reply cache (1024)
может очищаться по expiry: durable bindings всё равно исключают повторную выдачу.

## Persistence и миграция

`0007_control_authority` следует за `0006_command_lifecycle` и создаёт только
`command_control`. В существующие таблицы/строки ничего не дописывается:

| Поле | Назначение |
|---|---|
| command_id | PK + RESTRICT FK на существующий ledger |
| execution_profile, authority_epoch | Explicit profile/current owner epoch |
| wire_payload, wire_sha256 | Exact immutable candidate bytes и fingerprint |
| permit_binding_sha256 | Nullable до выдачи; затем single-use binding, UNIQUE |

Есть constraints profile, размеров hash/payload/epoch. Историческим командам control
metadata не выдумываются. Grant reply остаётся RAM-only: после потери cache безопасен
отказ. Downgrade требует online data check и запрещён при любой control history.
Startup не применяет migration и не создаёт inventory/credentials.

Проверены upgrade/downgrade пустой control table с полным сравнением старого ledger,
metadata alignment, rollback ledger при ошибке child insert и отказ downgrade при
сохранённых bindings. Это временная SQLite DB; существующие установки/volumes не
затрагивались. Реальные PostgreSQL/live broker/hardware проверки здесь не заявляются.

## ACL, software demo и ограничения

Tracked broker ACL добавляет **только Core** read `+/+/control/request` и write
`+/+/control/command`, `+/+/control/reply`. Simulator/motion/device rights не расширены.
Новые physical accounts/ACL не provisioned; существующий commissioning helper для
legacy set не становится helper нового профиля. Перед Firmware acceptance нужен
отдельный isolated operator ACL с точными physical topics и без legacy set.

Если ещё старый broker ACL отказывает experimental subscription, Gateway сохраняет
legacy subscriptions. Control publication без подтверждённой subscription/connection
недоступна. `/ready` остаётся готовностью приложения/schema, не доказательством
physical time safety. Web v0.19 и установленное software demo v0.20 не менялись.
Upgrade-check распознаёт новый schema head, но никакое обновление не запускалось.

Для совместного Firmware fixture нужны strict новый parser/state machine, session
invalidation, NVS format upgrade/dedup и final bounded GPIO check. До аппаратной
проверки rho/epsilon/G, UTC health и single-authority deployment никакого production
enablement. Nonce не заменяет broker authentication или firmware attestation.
Firmware должен считать digest исходных MQTT bytes (без добавленного NUL) **до**
in-place JSON parsing; повторная сериализация кандидата не эквивалентна exact-byte hash.

## Изменённые файлы этого этапа

Новые: `app/models/control_command.py`, `app/services/control_authority.py`,
`app/services/control_clock.py`, `app/transports/control_models.py`,
`alembic/versions/0007_control_authority.py`, `tests/test_control_authority.py`,
этот документ.

Обновлены: `app/models/__init__.py`, `app/main.py`, `app/api/commands.py`,
`app/services/command_service.py`, `app/transports/{mqtt,mqtt_topics}.py`,
`deploy/mosquitto/acl`, `deploy/demo_upgrade.py`,
`tests/test_{mqtt,schema,command_schema,e2e_automation}.py` и ссылки в документации.
Изменения предыдущего foundation сохранены; Firmware/Web/private inputs не менялись.

## Результаты проверки

- Полный итоговый `pytest -q`: **561 passed, 14 skipped**, 221.79 s.
- Authority/schema/readiness: **47 passed**; authority содержит 35 cases.
- Ruff: `check app simulator tests alembic deploy/demo_upgrade.py` — passed.
- Alembic: единственный head `0007_control_authority`; schema alignment и
  сохранность исторического ledger проверены на временных SQLite DB.
- Проверены strict parsing/направления topics, CAS/replay/nonce uniqueness,
  задержанный reply, cache loss, missing proof, разные boot/session/epoch/digest,
  restart fence/cohort, скачки времени, late ACK, закрытая production wiring,
  static ACL directions и legacy MQTT при отказе control subscription.
- Live broker/ACL enforcement, dedicated PostgreSQL и hardware/IDF acceptance
  не запускались; их не заменяют static ACL и fake-clock tests. Сохраняются
  известные Starlette/AnyIO и Alembic path_separator deprecation warnings.

Готово к code review и будущей изолированной интеграции Firmware. Не готово к
physical enablement: нужны firmware/journal/GPIO implementation, квалификация
clock/UTC/bounds и совместная приёмка. Коммитов, PR и deploy нет.
