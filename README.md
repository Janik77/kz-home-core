# KZ Home Core v0.5

KZ Home Core — компактное ядро умного дома на FastAPI, Pydantic и SQLAlchemy 2.
PostgreSQL хранит структуру дома, устройства, сцены и автоматизации; EventBus,
WebSocket, transport abstraction и virtual demo mode сохранены.

## Архитектура

```text
HTTP / WebSocket → FastAPI API → Services → Repositories → SQLAlchemy → PostgreSQL
                                  ↓
                              EventBus → Automation / WebSocket
                                  ↓
                         Virtual / MQTT transports
```

API не обращается к SQLAlchemy напрямую. Routes вызывают services, services —
repositories. Состояние, capabilities, metadata и правила хранятся в PostgreSQL
JSONB. Для локальных тестов используется отдельная SQLite database.

## Установка и конфигурация

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

В development/test файл `.env` загружается автоматически. Уже установленные
переменные процесса имеют приоритет над значениями из файла. При явном
`APP_ENV=production` локальный `.env` не загружается. `DATABASE_URL` остаётся
обязательным, а реальные пароли нельзя коммитить.

```bash
export DATABASE_URL='postgresql+psycopg://kzhome:password@localhost:5432/kzhome'
export APP_ENV=development
export APP_DEBUG=false
export KZHOME_SIMULATOR_ENABLED=false
```

### MQTT Device Gateway

MQTT по умолчанию отключён (`MQTT_ENABLED=false`), поэтому development и HTTP
API не требуют доступного broker. Для локального broker включите gateway явно:

```bash
export MQTT_ENABLED=true
export MQTT_HOST=localhost
export MQTT_PORT=1883
export MQTT_CLIENT_ID=kzhome-core-development
export MQTT_KEEPALIVE=60
export MQTT_TLS_ENABLED=false
# MQTT_USERNAME и MQTT_PASSWORD задаются вместе, если broker их требует.
uvicorn app.main:app --reload
```

При включении обязательны `MQTT_HOST` и уникальный `MQTT_CLIENT_ID`. В
`APP_ENV=production` gateway не запустится без `MQTT_TLS_ENABLED=true`;
credentials должны поступать только из environment/secret manager и не должны
попадать в MQTT payload, EventLog или logs. Broker ACL должен изолировать house
и разрешать физическому device доступ только к его собственным topics.

Device Protocol v1 использует namespace
`kzhome/v1/{house_id}/{device_id}/{state|set|ack|telemetry|status}`. Полный
контракт: [DEVICE_PROTOCOL.md](DEVICE_PROTOCOL.md). Gateway подключается и
переподключается в background, поэтому недоступный broker не блокирует запуск
HTTP API.

Command (`.../set`, QoS 1, non-retained):

```json
{"protocol_version":"v1","command_id":"550e8400-e29b-41d4-a716-446655440000","correlation_id":"c8ec0c3c-2204-46f5-bddb-6076ec2f45d0","timestamp":"2026-09-11T12:00:00Z","state":{"brightness":65}}
```

Current state (`.../state`, QoS 1, retained):

```json
{"protocol_version":"v1","timestamp":"2026-09-11T12:00:01Z","correlation_id":"c8ec0c3c-2204-46f5-bddb-6076ec2f45d0","state":{"on":true,"brightness":65}}
```

ACK (`.../ack`, QoS 1, non-retained):

```json
{"protocol_version":"v1","command_id":"550e8400-e29b-41d4-a716-446655440000","correlation_id":"c8ec0c3c-2204-46f5-bddb-6076ec2f45d0","status":"applied"}
```

Status (`.../status`, QoS 1, retained) and telemetry (`.../telemetry`, QoS 0,
non-retained) remain separate from authoritative state:

```json
{"protocol_version":"v1","status":"online","last_seen":"2026-09-11T12:03:00Z","heartbeat_interval_seconds":60}
```

```json
{"protocol_version":"v1","timestamp":"2026-09-11T12:03:10Z","metrics":{"rssi":-61,"uptime":86420,"free_heap":118240,"temperature":42.5,"firmware_version":"1.4.2"}}
```

## Миграции, demo data и запуск

Таблицы при запуске приложения автоматически не создаются. Сначала примените
миграции и заполните демонстрационные данные:

```bash
alembic upgrade head
python -m app.seed
python -m app.seed  # безопасный повторный запуск, дубликатов не будет
uvicorn app.main:app --reload
```

Swagger UI: http://127.0.0.1:8000/docs

Новая миграция после изменения ORM-моделей:

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

Virtual simulator по умолчанию отключён и никогда не запускается автоматически
при `APP_ENV=production`. Для локального demo mode включите его явно:

```bash
export KZHOME_SIMULATOR_ENABLED=true
export KZHOME_SIMULATOR_INTERVAL=5
uvicorn app.main:app --reload
```

Simulator работает фоновой задачей и последовательно изменяет `hall_motion`,
`bedroom_temperature` и `main_leak_sensor`. Каждый шаг открывает отдельную
короткоживущую database session. События доступны на `/ws`.

## API

Сохранены `/health`, `/devices`, `/devices/{id}/state`, shortcuts `/on` и `/off`,
`/scenes`, `/scenes/{id}/run` и `/ws`. Для houses, floors, rooms, devices,
scenes и automations доступен CRUD. `GET /devices` принимает фильтры `house_id`,
`room_id`, `type` и `online`.

Automation Engine принимает только декларативные JSON-правила. Он не выполняет
Python-код, shell-команды, `eval` или `exec`. Подробности: [ARCHITECTURE.md](ARCHITECTURE.md).

## Authentication foundation (v0.6a)

Human identities are stored as normalized users with Argon2id password hashes.
A user receives house-scoped authority through one membership (`owner`,
`installer`, `technician`, or `resident`) per house. Permissions are defined in a
single role matrix. This release secures the new authentication endpoints; broad
RBAC enforcement on the existing house/device/scene APIs is intentionally deferred
to v0.6b.

Set `AUTH_JWT_SECRET` to a unique random value of at least 32 characters in
production. `AUTH_JWT_ALGORITHM` defaults to `HS256`, access lifetime to 15
minutes, and refresh lifetime to 30 days. The checked-in secret is only a local
development placeholder and is rejected in production. Optional `AUTH_DEMO_EMAIL`
and `AUTH_DEMO_PASSWORD` create a user during seed only outside production and
only when both values are explicitly supplied. Production gateways/proxies must
rate-limit `/auth/login` and `/auth/refresh`; the application caps credential and
token input sizes but does not claim distributed rate limiting.

Example flow (replace all placeholder values):

```bash
curl -X POST http://127.0.0.1:8000/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"user@example.invalid","password":"<development-password>"}'
curl -X POST http://127.0.0.1:8000/auth/refresh \
  -H 'content-type: application/json' -d '{"refresh_token":"<refresh-token>"}'
curl -X POST http://127.0.0.1:8000/auth/logout \
  -H 'content-type: application/json' -d '{"refresh_token":"<refresh-token>"}'
curl http://127.0.0.1:8000/auth/me -H 'Authorization: Bearer <access-token>'
```

Refresh tokens rotate on use and their hashed identifiers are tracked server-side
for revocation. API responses never include password hashes. Owners have all
house permissions; installers manage installation devices/scenes/automations;
technicians manage and diagnose devices; residents read/control devices and
read/run scenes. Member and house administration remain owner-only.

### Motion → light

```json
{
  "trigger": {"type": "device_state", "device_id": "hall_motion", "field": "motion", "operator": "eq", "value": true},
  "conditions": [],
  "actions": [{"type": "device_state", "device_id": "living_room_light", "state": {"on": true}}]
}
```

### Night motion → brightness 15%

```json
{
  "trigger": {"type": "device_state", "device_id": "hall_motion", "field": "motion", "operator": "eq", "value": true},
  "conditions": [{"type": "time", "after": "23:00", "before": "07:00"}],
  "actions": [{"type": "device_state", "device_id": "living_room_light", "state": {"on": true, "brightness": 15}}]
}
```

### Delay → light off

```json
{
  "trigger": {"type": "device_state", "device_id": "hall_motion", "field": "motion", "operator": "eq", "value": false},
  "conditions": [],
  "actions": [
    {"type": "delay", "seconds": 60},
    {"type": "device_state", "device_id": "living_room_light", "state": {"on": false}}
  ]
}
```

Состояние automation меняется через `/automations/{id}/enable` и `/disable`, а
`/automations/{id}/run` запускает правило вручную. История важных событий доступна
через `GET /events` с фильтрами `house_id`, `event_type` и `limit`.

## Тесты

Тесты явно используют отдельную SQLite database и не подключаются к production:

```bash
pip install -r requirements-dev.txt
pytest
ruff check app simulator tests
```
