# KZ Home Core — v0.11 operator commissioning bridge

Core now supports trusted physical inventory, one-time claim into a house, and
explicit activation/deactivation/revocation through existing house RBAC. Read
[onboarding lifecycle, API and operator boundaries](DEVICE_ONBOARDING.md) before
commissioning. Explicit migration `0005_device_onboarding` is required. MQTT
credentials/ACLs and firmware/network provisioning remain operator-managed;
activation confirms external preparation and does not perform it.

The [one-relay commissioning procedure](DEVICE_COMMISSIONING.md) now provides
deterministic local ACL preparation, interactive operator password updates,
rotation/revocation checks and optional LAN TLS publication. Base Compose remains
internal-only for MQTT. No credential API, schema or firmware changes are added.

v0.8 operator acceptance has passed on the production Compose stack: authenticated
MQTT over verified TLS; Core reconnect and subscription restoration after a
Mosquitto restart; HTTP ON/OFF -> MQTT relay -> matching ACK/state -> PostgreSQL
persistence; and MQTT motion -> existing AutomationService -> MQTT relay ->
correlated ACK/state -> persistence. These are operator-reported live results,
not physical ESP32/hardware verification or complete protocol conformance.

Both acceptance runners are opt-in and separate from production startup and normal
pytest. See [operator procedures](simulator/E2E_ACCEPTANCE.md) and
[current status/limitations](PROJECT_STATUS.md). The runtime API version remains
`0.6.0b1`; the Compose Core image label remains `v0.7-local`. These historical labels
were not retagged or deployed during v0.8 acceptance work.

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

Routes проверяют аутентификацию и права дома; используют repositories для scoped
reads и services для бизнес-операций. Состояние, capabilities, metadata и правила хранятся в PostgreSQL
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
single role matrix. Authentication was introduced in v0.6a; the existing
house/device/scene APIs now enforce house-scoped RBAC as described below.

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

Domain HTTP operations require an access token. Health/readiness, login/refresh,
and the default API documentation endpoints are public.
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

The image can also be used by the Compose foundation below. Reverse proxy,
certificate issuance, and backup automation remain separate work.
The base tag receives updates and transitive dependencies are not fully locked;
record the tested image digest for a deployment and rebuild deliberately.

### Single-server Compose foundation

For the separate v0.8 relay and motion automation acceptance processes, see
[E2E acceptance](simulator/E2E_ACCEPTANCE.md) and
[standalone simulators](simulator/README.md). They use verified TLS and dedicated
device ACLs on the existing private backend network; they do not enable Core's
in-process demo simulator. Bootstrap is explicit, atomic and limited to dedicated
E2E records; it is not a general production user-recovery tool.

`compose.production.yaml` runs Core, official PostgreSQL 17, and Eclipse Mosquitto
2. Use Linux containers and Docker Compose v2 supporting long bind mounts and
health dependencies. PostgreSQL and MQTT have **no published host ports**; Core
publishes only `127.0.0.1:8000`. All services share an internal backend bridge.
Core alone also joins a non-internal frontend bridge so Docker can activate the
loopback port publication. This permits Core outbound connectivity; PostgreSQL
and Mosquitto remain attached only to the internal backend network.
After changing this topology, recreate only Core (a restart is insufficient):

```powershell
docker compose --env-file deploy/local/production.env -f compose.production.yaml up -d --no-deps --force-recreate core
docker compose --env-file deploy/local/production.env -f compose.production.yaml port core 8000
curl.exe --fail http://127.0.0.1:8000/health
curl.exe --fail -i http://127.0.0.1:8000/ready
```

The port command must report `127.0.0.1:8000`. No migrations or data-volume changes
are required for this network correction.
This base topology does not permit external ESP32 connections or public API access.
For explicit MQTT LAN access without broadening HTTP/PostgreSQL exposure, use
[the optional commissioning override and TLS SAN procedure](DEVICE_COMMISSIONING.md).

Prepare `deploy/local/production.env` from `deploy/compose.env.example`. Set a
separate database administrator password, a non-superuser Core database URL using
`postgres:5432/kzhome`, a strong JWT secret, and the Core MQTT password. Supply
literal values (single-quote values containing `$` in the Compose env file), and
URL-encode database password characters in `DATABASE_URL`. Set `TZ` deliberately.
These variables are injected only into services that need them. Core's existing
production/MQTT TLS guards stay enabled. Do not print expanded Compose config with
real secrets; use `config --quiet`. Protect the env file from other host users.

Supply these files yourself; the repository generates no certificates or keys:

| Host file | Mosquitto read-only input -> private runtime copy | Core read-only mount |
|---|---|---|
| `deploy/local/mqtt/ca.crt` | `/bootstrap/ca.crt` -> `/run/mosquitto/ca.crt` | `/run/mqtt/ca.crt` |
| `deploy/local/mqtt/server.crt` | `/bootstrap/server.crt` -> `/run/mosquitto/server.crt` | Not mounted |
| `deploy/local/mqtt/server.key` | `/bootstrap/server.key` -> `/run/mosquitto/server.key` | Not mounted |
| `deploy/local/mqtt/passwords` | `/bootstrap/passwords` -> `/run/mosquitto/passwords` | Not mounted |

The repository ACL is likewise copied from `/bootstrap/acl` to
`/run/mosquitto/acl`. Only these individual inputs are mounted, never the entire
local TLS directory. `ca.key`, `server.csr`, `server.ext`, and `ca.srl` are issuance
material and are **not mounted**. Keep the CA private key offline/protected.

Certificates must be PEM; `server.crt` includes the server/intermediate chain and
must have **DNS SAN `mosquitto`**, matching Core's fixed Compose `MQTT_HOST`.
Supply the matching server private key suitable for unattended broker startup.
Do not put the CA private key on this server. Core sets the standard OpenSSL
`SSL_CERT_FILE=/run/mqtt/ca.crt`; its existing `ssl.create_default_context()` loads
that CA, retains certificate-chain and hostname verification, and connects on
8883. No insecure verification flags or application TLS changes are used.
The broker uses TLS 1.2 or newer plus username/password authentication, not mTLS.
See [Python SSL](https://docs.python.org/3.12/library/ssl.html) and
[Mosquitto TLS/authentication settings](https://mosquitto.org/man/mosquitto-conf-5.html).

On the Linux deployment host, create the directories and generate the **password
hash file** interactively, entering the same password as `MQTT_PASSWORD`:

```sh
mkdir -p deploy/local/mqtt
docker run --rm -it --user 0:0 --entrypoint mosquitto_passwd -v "$(pwd)/deploy/local/mqtt:/bootstrap" eclipse-mosquitto:2 -c /bootstrap/passwords kzhome-core
```

Use `-c` only when creating a new password file; omit it for updates. This is a
bootstrap container, not ordinary broker startup. The checked-in ACL allows this
Core identity to read inbound v1 topics and publish only commands. Anonymous access
is disabled. Prepare a physical relay's unique account and exact ACL using
[operator commissioning](DEVICE_COMMISSIONING.md); passwords remain external to Core.

Windows bind mounts do not reliably provide Linux ownership/mode semantics.
`deploy/mosquitto/start.sh` starts as container root, validates required inputs,
and copies them with umask 077 into a private 1 MiB tmpfs. It sets every runtime
file to mode 0600, owned by the image's `mosquitto` user/group, and the directory
to 0700. It also assigns the data-volume directory to that user. It then execs
Mosquitto, whose explicit `user mosquitto` configuration drops privileges.
Missing/unreadable files fail startup; no insecure fallback is used. The script
has enforced LF line endings for Windows checkouts. Copies disappear on container
removal and are restaged on each start; no secret volume or baked-in secrets exist.

Protect source private files with Windows ACLs or Linux mode 0600 and appropriate
host ownership. They must be readable by the container bootstrap root; do not
make private keys world-readable. Core UID 10001 must still read the public CA
bind mount (0644 is appropriate for that public certificate). This does not alter
TLS verification. Rotation requires replacing source files and recreating Mosquitto
to refresh copies/bind mounts; restart Core as well after changing CA trust.
All `deploy/local/` contents and common certificate/key extensions are excluded
from Git and Docker build context. No log volume is needed: broker logs go to
stdout, with bounded Docker log rotation for all services.

To retry the broker on Windows PowerShell after this permission fix, from the
repository root (this does not touch PostgreSQL or run migrations):

```powershell
docker compose --env-file deploy/local/production.env -f compose.production.yaml config --quiet
docker compose --env-file deploy/local/production.env -f compose.production.yaml up -d --no-deps --force-recreate mosquitto
docker compose --env-file deploy/local/production.env -f compose.production.yaml logs --tail 50 mosquitto
docker compose --env-file deploy/local/production.env -f compose.production.yaml exec mosquitto ls -ln /run/mosquitto
```

Expect private files to show `-rw-------`, owned by the broker UID/GID, and no
key/ACL/password permission warnings. Do not display their contents. Once the
broker is stable, start/restart Core using the already documented explicit
migration workflow. A healthy `/ready` alone does not verify MQTT authentication.

The commands below use a shell helper from the repository root:

```sh
dc() { docker compose --env-file deploy/local/production.env -f compose.production.yaml "$@"; }
dc config --quiet
dc build core
dc up -d postgres mosquitto
dc ps
dc logs --tail 50 mosquitto
```

Wait for PostgreSQL to report healthy and confirm Mosquitto starts without TLS or
credential errors. Core depends on PostgreSQL health and broker process startup;
there is deliberately no claim that process startup proves authenticated MQTT
readiness. Core reconnects with exponential backoff from 1 to 30 seconds and
restores all four inbound subscriptions on each connection. Backoff resets after
inbound traffic, so repeated immediate disconnects or subscription failures also
back off. `MQTT connected; inbound subscriptions restored` confirms subscription
setup, not device availability. An already reported broker disconnect is not
reported again as a cleanup failure. Shutdown cancels backoff; an in-flight
connection attempt is allowed to settle so a late connection can be closed.
Its duration still depends on the MQTT library/socket timeouts. PostgreSQL's
healthcheck checks server availability, **not migrations**.

Application INFO logs are configured on stderr when no application/root logging
handler has been supplied; Uvicorn's default server logging alone does not enable
application INFO logs. Operator-provided logging configuration takes precedence.

On a fresh database volume, provision the application role once using the local
administrator connection. Do not use the image's bootstrap superuser in Core:

```sh
dc exec postgres psql -U postgres -d kzhome
```

Inside psql (enter the application password interactively at `\password`):

```text
CREATE ROLE kzhome LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
\password kzhome
ALTER DATABASE kzhome OWNER TO kzhome;
\q
```

Set `DATABASE_URL` to this role and password. The role owns this one database and
can run migrations; splitting runtime and migration privileges is later hardening.
The official PostgreSQL image's environment initializes only an empty volume;
changing `POSTGRES_PASSWORD` later is not password rotation. See the
[official PostgreSQL image documentation](https://hub.docker.com/_/postgres).

Apply migrations explicitly and start Core only after success:

```sh
dc run --rm --no-deps core python -m alembic upgrade head
dc up -d core
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
dc logs --tail 50 core
```

The same complete production settings are needed for migrations, but that command
does not start MQTT. `/health` and the image healthcheck are liveness only;
`/ready` checks initialization/database/Alembic head, not broker connectivity.
Confirm broker authentication/TLS separately from these HTTP probes. To upgrade,
stop Core, build the intended image, explicitly migrate, and start Core again;
never run old/new Core instances concurrently against this broker.

```sh
dc stop core
dc up -d core
dc stop
dc up -d
```

Use the last command only for an already provisioned and migrated stack. `dc down`
removes containers/network but retains named volumes `postgres_data` and
`mosquitto_data` (prefixed by Compose project `kzhome`). Never use `down -v` unless
intentionally deleting installation state. Retained MQTT data and PostgreSQL data
survive container recreation; certificates/passwords/env remain operator-managed
host files. Named volumes are not backups. API files need no persistent volume.

HTTPS, reverse proxy, public exposure, certificate automation, physical-device
hardware acceptance, backup/restore automation, and MQTT readiness remain deferred.
LAN MQTT is an explicit optional deployment mode, requiring operator DNS/SAN,
firewall and device-VLAN verification; it is not enabled by base Compose.
Image tags are major-version pinned, not immutable digests; record tested digests
for releases. The operator-verified v0.8 paths above do not establish general
production readiness, backup recoverability or physical-device compatibility.
