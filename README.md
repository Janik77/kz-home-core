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

### Protected Core API (v0.6b)

Except for health, login, and refresh, HTTP operations require an access token.
The server resolves each resource's real house and checks its membership permission
before invoking the existing service. List queries are scoped in the database to
authorized house IDs. Known IDs in another house deliberately return `404`; a user
who belongs to the house but lacks the required capability receives `403`.

House creators atomically receive an owner membership. Owners can manage members at
`/houses/{house_id}/members`; the final owner cannot be removed or downgraded.
Superusers do not bypass house membership in v0.6b. WebSocket clients authenticate
the `/ws` upgrade with `Authorization: Bearer <access-token>` and receive only
events whose `house_id` is in their authorized memberships. Proxies must redact
the Authorization header from logs.

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

### Schema verification

The normal pytest suite checks the linear Alembic chain and SQLite migrations
without a PostgreSQL server. To also test fresh PostgreSQL installation, upgrades,
and correlation-ID length enforcement, explicitly set `TEST_POSTGRESQL_URL` to a
dedicated test database using `postgresql+psycopg://...`, then run
`pytest tests/test_schema.py`. These tests create a unique schema inside a
transaction and roll it back; the test role needs permission to create schemas.
They never fall back to `DATABASE_URL`. Without this variable they are skipped.

Revision `0004_event_log_correlation` widens event correlation IDs to 128
characters. Its downgrade requires an online connection and refuses to proceed
while IDs longer than 36 characters exist; it never truncates them.
Application startup still does not apply or verify migrations: run
`alembic upgrade head` explicitly before starting Core.

### Liveness and readiness

`GET /health` remains a lightweight public liveness check with
`{"status":"ok","version":"0.6.0b1"}`. It does not query dependencies.
`GET /ready` is a public, non-cached probe: 200 with `{"status":"ready"}` only
after application lifespan initialization and a successful database query whose
Alembic revision matches the single packaged head. Otherwise it returns 503 with
`status: not_ready` and a safe reason: `application_not_initialized`,
`database_unavailable`, or `schema_revision_mismatch`.

Readiness borrows the application engine and closes each connection. PostgreSQL
and SQLite use the same Alembic revision check; ORM `create_all` without migration
history is not sufficient. Missing, behind, unknown, or multiple database revisions
are not ready. This checks migration history, not manual schema drift.
Ship the `alembic/` directory with the application. No migrations, schema repair,
seeding, or retries run during startup/probes. Run `alembic upgrade head` as an
explicit deployment step. MQTT connectivity/device readiness is not included in
this endpoint yet; its startup/reconnect behavior is unchanged.

### Production Core container

The Dockerfile uses the official `python:3.12-slim-bookworm` image, matching the
Python 3.12 local validation environment. Python 3.12 remains supported; see the
[Python support schedule](https://devguide.python.org/versions/) and
[official image tags](https://hub.docker.com/_/python).
Only runtime requirements and application/simulator/migration files are copied.
The simulator module is required by imports but remains disabled in production.
Application files are root-owned; runtime runs as UID/GID 10001 in `/app`.

Build from the repository root with Docker configured for Linux containers:

```sh
docker build --pull -t kz-home-core:v0.7-local .
```

Provide configuration through environment variables only. Prepare a protected
environment file **outside the repository/build context**, using
`.env.production.example` as the variable reference; replace its placeholders.
The following `/secure/kzhome.env` is only a path placeholder, not a supplied file.
Docker `--env-file` injects process variables; Core does not read that file itself.
Never pass secrets as build arguments or copy them into the image.

Production requires a PostgreSQL URL, a strong JWT signing secret, MQTT enabled,
MQTT host/client ID, and TLS. Supply broker credentials together and configure
broker authentication/ACLs externally. PostgreSQL and the broker must be reachable
from the container: `localhost` refers to the container, not the host.
Choose the installation timezone explicitly through `TZ` for local-time rules.
Private broker CAs must be trusted by the container runtime; do not disable TLS
verification. Certificates, database, broker, and network provisioning are external.

Before starting Core, run migrations explicitly against the intended database
using the same image and environment. This command validates production settings
but does not start the API or connect MQTT:

```sh
docker run --rm --env-file /secure/kzhome.env kz-home-core:v0.7-local python -m alembic upgrade head
```

Only after migrations succeed, start the single Core process:

```sh
docker run -d --name kzhome-core --env-file /secure/kzhome.env -p 127.0.0.1:8000:8000 kz-home-core:v0.7-local
```

This binds host access to loopback; HTTPS/reverse-proxy deployment is separate work.
Do not scale instances or override the worker count. The exec-form command runs
Uvicorn directly, without reload, seeding, migration hooks, or a shell wrapper.
Use `docker stop --time 30 kzhome-core` for a graceful-stop window.

The image healthcheck uses Python's standard library against `/health`; no curl
or extra package is installed. It reports process liveness, not database readiness,
and does not automatically restart an unhealthy container. Check `/ready` before
sending traffic: it requires initialized application, database access, and the
expected Alembic head. It does not check MQTT/device availability.

This is the Core image foundation only: no Compose, PostgreSQL/Mosquitto
containers, reverse proxy, certificates, or backup automation are supplied.
The base tag receives updates and transitive dependencies are not fully locked;
record the tested image digest for a deployment and rebuild deliberately.
