# Core v0.22: command expiry foundation

**Следующий этап:** [control authority](CONTROL_AUTHORITY_CORE_V0.22_RU.md)
добавляет experimental control topics и отдельную migration `0007_control_authority`.
Описанное ниже отсутствие миграций относилось только к первоначальному foundation;
physical gate по-прежнему закрыт, HTTP-контракт software demo сохранён.

Первая часть контракта. Сквозная гарантия «ESP32 не запишет GPIO после E»
**не реализована и не заявляется**. Нужен согласованный Firmware v0.12.
Исходный [проектный документ](COMMAND_EXPIRY_V0.22_DESIGN_RU.md) сохранён;
решения этого review имеют приоритет над его предложениями.

## Критический review time-proof

Текущие v1 `ack`/`set` имеют конкретные ACK/CommandEnvelope schemas.
`time_probe` без command IDs/status не является ACK; `time_reply` без intent
не является командой. Без согласованного discriminator и firmware parser
перегружать их нельзя. Core продолжает отвергать такие пакеты, не отвечает на
них и не выдаёт разрешений. Namespace/ACL не меняются.

Nonce связывает ответ с запросом, но не доказывает точность часов, oscillator
drift, lifetime lease или время последующего GPIO. RTT interval требует
подтверждённых monotonic bounds и непрерывной authority Core. Предложенные в
черновике RTT=2 s/lease=60 s — policy guesses, не доказанные bounds. Fake-clock
тест не доказывает максимальную задержку NVS/preemption/реального GPIO.

Безопасная альтернатива для совместного этапа: отдельный типизированный control
подпротокол, например `time/request` и `time/reply` под тем же
`kzhome/v1/{house}/{device}`. До реализации нужны review envelope, topic parser,
directional ACL, session/nonce replay protection, rate limits и TLS bootstrap.
Это предложение, **не новые разрешённые topics**. Для сохранения ack/set нужен
эквивалентный review расширения обеих схем. Пока ни один вариант не реализован.

Restart Core обрывает прежнюю monotonic шкалу. Сохранённый epoch/nonce сам по
себе не восстанавливает доверие. Pending rows, неверный boot UTC, несколько Core
workers, recovery fence и invalidation device leases требуют совместного проекта.
Старые уже отправленные команды нельзя отозвать DB timeout. Поэтому в этом этапе
вообще нет способа разрешить физическую отправку — даже через metadata или restart.

## Реализованное поведение

- Ledger API сохраняет creation/E до I/O и передаёт **эти же даты** через Transport
  в Gateway. Timestamp публикации больше не подменяет creation. Максимум lifetime
  один: 30 s. Gateway требует обе даты в UTC, `0 < E-created <=30 s` и проверяет
  `created <= now < E` непосредственно перед publish. Fallback E=now+30 отсутствует.
- Same-key POST возвращает прежний receipt без publish; reconnect не пересчитывает
  payload. Явный повтор Gateway требует исходные даты. Автоматического resend/replay нет.
  Задержку внутри уже начатого publish Core не контролирует.
- DeviceService и CommandService запрещают physical inventory. Gateway независимо
  выполняет DB-backed проверку binding/active lifecycle и physical gate. API, legacy
  on/off/state, scenes и automations не обходят запрет. Чтение и ingress сохраняются.
- Все physical profiles, включая active classic/C6, пока unsupported для control.
  Нет config/API-флага enablement. Metadata версии/policy и предполагаемое time-proof
  недоверенные и не снимают запрет.
- Неизвестный MQTT record не становится software из-за отсутствия inventory.
  Нужна явная operator-настройка `MQTT_SOFTWARE_DEVICES`: JSON-массив точных
  `[house_id,device_id]`, по умолчанию `[]`, без wildcard. Physical inventory имеет
  приоритет даже при ошибочном включении его ID в список. Обычная Device metadata
  не меняет этот список. Это доверенная классификация, не аппаратная аттестация.
- Программный legacy dispatch создаёт один непостоянный 30 s window в DeviceService.
  Он остаётся dispatch-only без выдуманного ledger receipt/retry. Это исключительно
  software exception. Для ledger API единственный источник E — сохранённый
  `device_commands.expires_at`; Gateway никогда его не вычисляет.
- Software relay проверяет имеющийся E до изменения software state. Исторические
  сообщения без E декодируются только для software fixtures; volatile dedup и часы
  симулятора не объявляются физической гарантией.

Общий `CommandClock` для creation/evidence/timeout/dispatch использует UTC anchor
и monotonic elapsed. Naive/non-UTC sample, nonfinite/reversed monotonic или
wall/monotonic divergence >1 s защёлкивают отказ до restart процесса (HTTP 503).
Порог 1 s — диагностика, **не доказанная погрешность ESP32/UTC**. Неверный UTC
при старте может остаться незамеченным. Restart создаёт новый anchor и не включает
physical control. Этот provider не является завершённой time authority.
Старые ledger rows сохраняют прежнюю семантику unknown execution, а не получают
задним числом гарантию безопасного физического timeout.

## Совместимость и эксплуатация

HTTP paths/receipt/status/failure_code enums Web v0.19 сохранены. Unsupported
physical/unclassified destination даёт существующий 409 до новой ledger row/publish;
clock fault — 503. Automations сохраняют прежний `automation_failed` при отказе
действия. Scenes могут выполнить более ранние software actions до отказа physical;
атомарность сцен не добавляется. Physical action при этом не публикуется.

Новых migrations нет: существующих created_at/expires_at достаточно; head остаётся
`0006_command_lifecycle`. Epoch/verified flags не добавляются: постоянный флаг
без живой доказанной гарантии был бы опасным разрешением.

Tracked demo overlay явно разрешает только software `e2e_house/e2e_relay`.
Images/containers/private inputs/volumes наставника v0.20 не менялись, обновление
ему не требуется. Будущее обновление отдельно проходит safe upgrade/backup/review.
Software list не заменяет ACL, credentials, house RBAC или trusted inventory;
не подключать физическую плату под software ID. Перед будущим firmware update
отдельно изолировать прежние сессии/очереди: новый gate не отзывает старые пакеты.

## Проверки и оставшиеся блокеры

Тесты покрывают persisted E = wire E, publish delay, повтор/reconnect, отсутствие/
невалидность E, now=E, late first delivery software relay, late ACK/state,
idempotency/no replay, restart, clock fault, control paths, ложные metadata/probe
и ошибочную software classification physical ID. Lifecycle regression работает на
**отдельной** software identity; physical fixture не переименована ради обхода gate.

До physical enablement обязательны совместный parser/journal upgrade Firmware,
strict missing-E rejection, bounded authority/session protocol, restart/multiworker
recovery, доказанные drift/rounding/GPIO bounds и isolated delayed-delivery/hardware
acceptance обеих плат. Пока единственная заявленная физическая защита этого этапа —
отсутствие новых physical publish; receipt не гарантирует физический исход.

## Файлы и результаты проверки

Runtime: `app/services/command_clock.py` (новый),
`app/services/{command_service,device_service,onboarding_service}.py`,
`app/transports/{base,mqtt,mqtt_models}.py`, `app/core/settings.py`, `app/main.py`.
Software demo: `simulator/mqtt_relay.py`, `compose.demo.yaml`, `.env.example`.
Тесты: `tests/test_command_expiry.py` (новый),
`tests/test_{command_lifecycle,e2e_automation,mqtt,onboarding,physical_acceptance,settings}.py`.
Документы: этот файл (новый), сохранённый `docs/COMMAND_EXPIRY_V0.22_DESIGN_RU.md`,
`DEVICE_PROTOCOL.md`, `COMMAND_LIFECYCLE_V0.15.md`, `deploy/MENTOR_DEMO_RU.md`.

Полный `pytest -q --maxfail=4`: **526 passed, 14 skipped**, 273.48 s.
После финального объединения lifetime constant и удаления недостижимой ветки
повторены expiry/lifecycle suites: **51 passed**, 57.90 s.
Пропуски — opt-in integration checks; live broker/PostgreSQL/hardware acceptance
не запускались. Ruff `check app simulator tests alembic` пройден;
`alembic heads` показывает только `0006_command_lifecycle`; whitespace проверен.
Сохраняются известные Starlette/AnyIO и Alembic path_separator warnings.
Никаких deploy, изменений приватных inputs, Firmware/Web, коммитов или PR.
