# KZ Home — Provisioning v0.1.0

Статус: контракт и чистая модель попытки, база Core v0.22 `3d9e2d1`.
Это не работающий enrollment API. Нет новых маршрутов, таблиц, миграций,
выдачи credentials, сетевых вызовов или изменений existing onboarding.
Web v0.19 и Firmware v0.12 не меняются. Deploy и установка наставника
не затрагиваются. **Physical gate остаётся закрытым.**

## 1. Переиспользование модели

House → Floor → Room → Device остаётся существующей структурой. Доверенная
аппаратная identity хранится в PhysicalDevice, не в Device.metadata.
Capability — существующий серверный профиль, не заявление обнаруженной платы.
Новая модель состояния описывает попытку добавления, а не второе устройство.

`ProvisioningStart` наследует существующий `DeviceClaim`: hardware_id, SecretStr
claim_code, room_id, name. House берётся из будущего авторизованного маршрута.
device_id определяется доверенным inventory, а не выбирается клиентом.
Типы идентификаторов переиспользуют onboarding TopicID. Будущий transport обязан
дополнительно соблюдать общий Firmware/Core subset: 1–64 ASCII буквы, цифры,
`_` и `-`, первый символ буквенно-цифровой. Текущий TopicID шире этого subset.

Существующий claim проверяет код, дом комнаты, профиль и атомарно создаёт Device,
привязку и audit. Код потребляется один раз. Это доказательство знания кода,
а не криптографическая проверка присутствия платы. Существующие API не меняются;
новая модель сама не закрывает ограничения старого ручного процесса.

## 2. Архитектура и границы доверия

Цель — локальная установка без обязательного облака и ручного ввода MQTT.
Первый целевой transport: SoftAP + Espressif protocomm Security 2 (SRP6a/AES-GCM),
уникальный WPA2 пароль AP и доверенный локальный помощник на Windows.
Security 0 и автоматический downgrade запрещены. BLE — следующий transport
того же enrollment-контракта, не обязательная часть v0.1.

Web ведёт пользователя через выбор дома/комнаты и подтверждение. Браузер/PWA
не обеспечивает одинаковый BLE-доступ на iOS/Android/Windows и универсальное
управление Wi-Fi. Смена сети может прервать связь с Core. Полноценная мобильная
поддержка, captive portal и browser-only сценарий этой версией не заявляются.
Не загружать криптографическую точку доверия с непроверенной HTTP-страницы платы.

Границы:

- Пользователь проходит существующую Core authentication и проверку
  `device.onboard` в выбранном доме. Сейчас это owner/installer.
- Inventory импортирует доверенный оператор; discovery не создаёт inventory
  и не меняет ключи, профиль, дом или capabilities.
- Помощник переносит Wi-Fi и временные bootstrap-параметры; не имеет broker-admin
  доступа и не получает постоянные MQTT credentials.
- Устройство обращается в Core через проверенный HTTPS. CA/pin Core переносится
  по аутентифицированному setup-каналу; TOFU по ответу неизвестной LAN-службы нет.
- Core принимает владение; отдельный будущий broker adapter применяет точные ACL.
  БД и брокер не образуют общую транзакцию.
- Компрометация Core, брокера или доверенного помощника выходит за пределы
  защиты от соседнего устройства. Открытая flash допускает извлечение ключей;
  аппаратная аттестация этой схемой не предоставляется.

Ни helper, ни новый transport, ни public HTTP adapter в v0.1.0 не реализованы.

## 3. Будущее подтверждение владения

1. Доверенная подготовка конкретной платы создаёт уникальный setup secret и
   ключ подписи. Public key закрепляется в inventory, private key остаётся
   на плате. Закрытая карточка содержит setup-данные и отдельный claim-код.
2. Авторизованный пользователь выбирает дом/комнату. Core проверяет inventory,
   код и права, но до proof+confirm не потребляет claim.
3. Физическое действие открывает короткое setup-окно. Подтверждение не включает
   реле. Конкретные кнопка, индикация и GPIO требуют проверки board profile.
4. Помощник аутентифицирует Security 2, передаёт Wi-Fi, identity/CA Core,
   session ID, challenge и ограниченный enrollment token.
5. Устройство подключается к LAN и подписывает transcript для Core. В него входят
   protocol version, session ID, challenge, inventory identity, целевой house,
   identity Core и nonce текущего физического окна. Каноническое кодирование,
   алгоритм подписи и формат ключей должны быть определены до v0.1.1 integration.
6. Core проверяет подпись по заранее закреплённому ключу, контекст, nonce и TTL.
   Только доверенный verifier создаёт внутреннее событие `proof_verified`.
7. Пользователь подтверждает проверенную identity и расположение. Будущая
   транзакция повторно проверяет права/комнату, выполняет existing claim,
   сохраняет attempt revision и audit. Затем готовится broker access.
8. Credentials передаются напрямую устройству через HTTPS enrollment.
   Завершение связи требует свежего challenge/response через новый MQTT principal
   или проверяемого broker event, связанного с текущим credential generation.
   Retained online, HTTP 200 и заявление helper не доказывают подключение.

SSID, MAC, serial, близость по RSSI и знание одного публичного ID недостаточны.
Claim-код и setup secret различны. Утечка кода без ключа платы не должна позволять
завершить новый enrollment. Физический reset не отменяет серверное владение.

## 4. Challenge, время, replay и конкуренция

- Challenge и enrollment token генерируются из не менее 32 CSPRNG байт,
  уникальны для каждой попытки. 43 URL-safe символа — только формат, не
  доказательство энтропии. В snapshot хранится SHA-256 challenge.
- TTL конечен, задаётся сервером. Рекомендуемые начальные 5 минут — проектное
  значение, пока не policy/API. Клиент не продлевает TTL. `now >= expires_at`
  означает истечение, включая точное равенство.
- Все часы timezone-aware. `now` поступает от доверенного серверного clock;
  реальная защита от отката часов и restart требует v0.1.1 clock policy.
- Проверенный proof потребляет challenge один раз. Повторный proof/confirm
  отклоняется, даже с текущей revision. Новая попытка требует нового challenge.
- Enrollment token ограничен session/device и TTL; сам по себе не даёт claim,
  user API или broker-admin доступ. Не передавать token через URL/logs.
- `expected_revision` — строгий неотрицательный int, bool не допускается.
  Успешный переход увеличивает revision ровно на один; ошибка не меняет snapshot.
- Чистая модель не защищает два процесса с копиями snapshot. В v0.1.1 нужны
  durable session, DB CAS/row lock, одна активная попытка на identity и atomic
  consumption вместе с claim/audit. Конфликт возвращается без раскрытия inventory.
- Идемпотентная HTTP доставка и replay proof различаются. Повтор запроса с тем же
  ключом/телом может вернуть сохранённый результат, но не исполнить confirm снова;
  другой digest тела с тем же ключом — конфликт. В v0.1.0 HTTP idempotency нет.

## 5. Чистая модель состояний

| Из | Событие | В |
|---|---|---|
| discovered | proof_verified + совпадающий challenge | awaiting_confirmation |
| awaiting_confirmation | confirm | registered |
| registered | connected, доверенное свежее evidence | connected |
| любое нетерминальное | cancel | cancelled |
| любое нетерминальное | fail + фиксированный error code | error |
| любое нетерминальное, TTL истёк | expire | expired |

Connected, error, expired, cancelled терминальны для **попытки**. Все остальные
переходы запрещены. При TTL expiry обычная операция выбрасывает
`ProvisioningExpired`; caller должен отдельно материализовать `expire` и
сохранить его в будущем CAS, не продолжать исходную операцию.
Терминальный snapshot не меняется из-за последующего истечения TTL.

`confirm` здесь моделирует результат будущей атомарной регистрации; он НЕ вызывает
OnboardingService и НЕ регистрирует устройство. `proof_verified` НЕ проверяет
подпись. Public Confirm/Cancel принимают только expected_revision; клиентский
флаг proof_verified или physical_gate запрещён. Эти схемы пока не являются API.

Public status — явный allowlist session/house/device/room IDs, status, revision,
expires_at и фиксированный error_code. Нет raw proof, challenge/hash, token,
claim-кода, private key, Wi-Fi или MQTT credentials. Нельзя сериализовать внутренний
snapshot как HTTP response. SecretStr маскирует вывод, но future routes обязаны
редактировать Pydantic validation errors по образцу PrivateValidationRoute:
исходный rejected input может содержать секрет. Body/debug logging запрещён.

Lifecycle PhysicalDevice, состояние попытки и runtime online независимы.
Потеря связи после connected означает offline, не повторную регистрацию.

## 6. Будущая выдача и отзыв MQTT-доступа

Никаких общих постоянных секретов в Firmware/Web. Для каждого устройства — новый
случайный пароль из 32 байт, отдельные username/client ID и credential generation.
Пароль не совпадает с Wi-Fi/setup/claim секретами. Для первой версии достаточно
server-authenticated TLS + уникального пароля; mTLS не обязателен.

Выдача допускается после проверенного proof, confirm и согласованной permanent
binding. Broker adapter сначала создаёт disabled principal и точные ACL,
проверяет применение, затем включает доступ. Durable desired/applied state и
повторяемое reconciliation нужны до автоматической выдачи. Сбой не должен
оставлять активный principal без известной binding. Не держать DB lock через сеть.

Постоянные credentials идут устройству напрямую, не через Web/helper. Broker
хранит password hash. При необходимости повторной доставки Core хранит только
короткоживущий encrypted envelope с ключом вне БД; bounded retry отдаёт тот же
пакет тому же проверенному устройству. Потерянный ответ не создаёт новый пароль.
Отдельный NVS config store не затрагивает журнал дедупликации команд.

Отзыв: Core блокирует identity немедленно; broker adapter отключает principal,
действующее соединение и reconnect, очищает queued/persistent session traffic.
До broker ACK показывается незавершённый отзыв. Существующий Core revoke сам
по себе этого не выполняет. Удаление в v0.1 — revoke с сохранением identity,
не hard delete. Revoked не возвращается в claim обычным retry.

Смена Wi-Fi/recovery — отдельная авторизованная попытка с новым challenge и
физическим окном. Factory reset очищает пользовательскую конфигурацию, сохраняет
factory identity и не возрождает старый claim-код. Междомовой transfer отложен.

## 7. ACL Firmware v0.12 и закрытый physical gate

Старый DEVICE_COMMISSIONING.md и deploy/commission_device.py описывают чтение
legacy `/set`. **Этот ACL несовместим со startup/control Firmware v0.12**:
прошивка ожидает SUBACK собственных control/command и control/reply, публикует
control/request. Она не подписывается на legacy set.

Целевой ACL после отдельного согласования control transport:

```text
READ  kzhome/v1/{house}/{device}/control/command
READ  kzhome/v1/{house}/{device}/control/reply
WRITE kzhome/v1/{house}/{device}/control/request
WRITE kzhome/v1/{house}/{device}/ack
WRITE kzhome/v1/{house}/{device}/state
WRITE kzhome/v1/{house}/{device}/status
```

Нет wildcard, legacy set, чужих topics, записи command/reply, telemetry без
профильного основания или `$CONTROL` для устройства. Control packets non-retained.
Этот документ НЕ устанавливает ACL. Минимальный publish-only diagnostic profile
потребовал бы отдельного firmware startup режима, которого сейчас нет.

`active`, successful provisioning и ACL не квалифицируют clock/bounds и не
разрешают физическое управление. Core closed_gate и PhysicalBindingGuard,
Firmware unqualified budget остаются обязательны. Новые модули не импортируют
control authority, ORM/DeviceCommand, MQTT, repository или сеть, не выпускают
permit/credential и не вызывают GPIO. Нельзя использовать demo identity для платы.

## 8. Готовность v0.1.0 и вопросы v0.1.1

Готовность: offline tests переходов, boundary expiry, cancel, повторных confirm,
challenge replay и revision conflict; schema/redaction проверки; профильные
onboarding/control regressions; Ruff и formatting новых Python файлов.
Проверки импорта ограничивают operational dependencies чистой модели.

До v0.1.1 определить durable session schema/CAS и порядок блокировок, clock/TTL,
канонический signed transcript и ключи, импорт trust anchor, enrollment token
scope, rate limits, idempotency, redacted errors и совместимость старого manual
claim. Затем отдельно реализуются broker adapter/reconciliation, LAN HTTPS/DNS,
helper и Firmware secure config storage. Нужны patched ESP-IDF/protocomm,
проверка размеров обеих сборок и board-specific физического подтверждения.
Ни мобильная совместимость, ни hardware acceptance здесь не подтверждены.

Опорные контракты: [onboarding](../DEVICE_ONBOARDING.md),
[commissioning](../DEVICE_COMMISSIONING.md),
[control authority](CONTROL_AUTHORITY_CORE_V0.22_RU.md),
[mentor demo](../deploy/MENTOR_DEMO_RU.md).
