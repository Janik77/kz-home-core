# KZ Home — Project Status

Canonical development handoff. This is a repository snapshot, not certification
of a deployed installation. Update it when capabilities or validation change.

## 1. Project goal

KZ Home is a smart-home platform for organizing houses and controlling devices,
scenes, and automations. `kz-home-core` is its backend: HTTP/WebSocket access,
persistence, authorization, automation execution, and device transport integration.
There is no frontend in this repository. ESP32 firmware is maintained separately
in `kz-home-firmware` (project context); its current implementation and hardware
compatibility have not been verified here.

## 2. Current architecture

- **API** (`app/api`): FastAPI routes, Pydantic input, authentication dependencies,
  resource-house resolution and permission checks. Routes also use repositories
  for scoped reads/authorization; business operations primarily use services.
- **Services** (`app/services`): device validation/state, scene execution,
  declarative automations, authentication/membership operations, event history.
- **Repositories** (`app/repositories`): SQLAlchemy queries, persistence, and
  house-scoped lookups. Preserve these boundaries when extending the backend.
- **Models/database** (`app/models/`, `app/db/`, `alembic/`): synchronous SQLAlchemy
  sessions, PostgreSQL storage and migrations; JSON uses JSONB on PostgreSQL.
- **EventBus** (`app/events`): sequential, in-process delivery to automation
  scheduling, EventLog persistence, and WebSocket broadcasting.
- **Transport/MQTT** (`app/transports`): normalized command interface and aiomqtt
  adapter; incoming messages reach DeviceService through application callbacks.
- **Automation Engine**: active implementation is `AutomationService`, wired in
  `app/main.py`; stored rules execute through DeviceService and the transport.
- **Authentication/RBAC**: `app/security.py`, auth services/repositories, and API
  dependencies separate human identity from house-scoped permission checks.

## 3. Current backend capabilities

- CRUD for houses, floors, rooms, devices, scenes, and automations; device filters.
- Device capability validation, state updates, on/off shortcuts, status reports.
  With MQTT enabled, commands leave current state unchanged until device reports
  arrive. Without a transport, development/test services support virtual updates.
- Scenes validate all action-device house associations on create/update and
  preflight ownership before executing any action.
- Automations support device-state triggers, AND-combined state/time conditions,
  ordered state/delay actions, enable/disable/manual run, and failure events.
- EventBus, persisted EventLog, house-scoped event queries, and authenticated `/ws`.
- MQTT v1 topics: `kzhome/v1/{house_id}/{device_id}/{state|set|ack|telemetry|status}`;
  envelopes, QoS/retention policies, size/depth limits, registered house/device
  checks, background connection/reconnection, and separate telemetry/ACK events.
- [DEVICE_PROTOCOL.md](DEVICE_PROTOCOL.md) defines Device Protocol v1. Its contract
  is broader than the implemented runtime; see limitations below.
- Login, refresh, logout, current-user endpoint, Argon2id password hashing, access
  JWTs, and server-side refresh sessions with hashed identifiers and rotation.
- House memberships: owner, installer, technician, resident; owner-managed members,
  final-owner protection, atomic house creation with initial owner membership.
- Optional virtual simulator and idempotent demo seed outside production.

## 4. Security model

Human access tokens establish identity; resource-house membership and the central
permission matrix authorize domain operations. Foreign resource access is hidden
as 404; insufficient permissions within a member's house return 403. Superuser
metadata grants no house bypass. Scene action references are checked separately
and cross-house references are rejected. Preflight does not lock ownership against
concurrent moves or make physical execution transactional.

Human JWTs and device credentials are separate identities. MQTT checks registered
topic ownership, but broker authentication and directional per-device ACLs remain
essential: Core does not receive authenticated publisher identity with messages.

Production configuration guards in `app/core/settings.py` enforce:

- Case-insensitive environment allowlist: development, test, local, production.
- Required database URL; production requires PostgreSQL, MQTT enabled, and MQTT TLS.
- Existing signing-secret checks and positive token lifetimes.
- Strict boolean parsing for debug, simulator, MQTT enablement, and TLS flags.
- Explicit process production mode skips local `.env` loading. Set it explicitly;
  environment defaults to development when absent.

Production suppresses the simulator and refuses demo seeding before database
access. `.env.production.example` documents required configuration without real
secrets. MQTT credentials must be paired if supplied; configuration does not itself
require broker authentication. EventLog redaction is key-based, not a guarantee
that arbitrary strings or exception traces are secret-free.

## 5. Database and migrations

SQLite remains supported for development/tests. Production requires PostgreSQL;
the declared driver is psycopg. Sessions are scoped and the engine uses
`pool_pre_ping=True`. Application startup neither creates tables nor applies or
verifies migrations.

Current linear chain, verified from migration revision declarations:

```text
0001_initial
  -> 0002_automation_engine
  -> 0003_auth_foundation
  -> 0004_event_log_correlation
```

The sole current head is `0004_event_log_correlation`. It widens the indexed,
non-null `event_logs.correlation_id` from VARCHAR(36) to VARCHAR(128), matching
the MQTT envelope limit. Downgrade requires an online data check and refuses
while IDs longer than 36 exist. SQLite does not enforce VARCHAR length like
PostgreSQL; SQLite success alone does not prove PostgreSQL compatibility.

Run `alembic upgrade head` explicitly against the intended database before
starting Core. Normal Alembic execution loads application Settings and therefore
requires valid configuration. `alembic/env.py` also accepts a supplied connection
for isolated migration tests. Existing migrations must not be rewritten.

## 6. Testing and validation

Tests cover API/CRUD, authentication, house RBAC/IDOR, scene isolation, automation,
fake-client MQTT, configuration guards, and schema/migration behavior. Most API
tests create SQLite schemas from ORM metadata; `tests/test_schema.py` additionally
executes migrations for fresh installation, upgrade, downgrade, and re-upgrade.

PostgreSQL tests are opt-in via `TEST_POSTGRESQL_URL` pointing to a dedicated test
database. They create a random schema in a transaction and roll it back, test
128-character storage and rejection of 129 characters, and never fall back to
`DATABASE_URL`. Without explicit configuration they skip. Real PostgreSQL results
are pending; tests existing in source do not establish that they have passed there.

Validation commands used by the project:

```text
python -m pytest
python -m ruff check app simulator tests alembic
python -m compileall -q app simulator tests alembic
git diff --check
python -m alembic heads
```

This documentation-only snapshot did not rerun tests or compilation, avoiding
generated file changes. No current test-count or deployment-pass claim is made.

## 7. Production/deployment readiness

**Already implemented:** configuration guards above; external Alembic migrations;
PostgreSQL-compatible model types; lifecycle-managed MQTT reconnect; shutdown task
cancellation and engine disposal; disabled FastAPI debug responses; domain error
handlers; authentication, RBAC, and scene house preflight. `/ready` checks lifespan
initialization, database access, and exact agreement with the packaged Alembic head;
failures return 503 with fixed safe reasons. `/health` remains lightweight liveness.

**Remaining before first real deployment:** MQTT/device readiness checks;
bounded and resilient shutdown; production user bootstrap/recovery; secure broker
configuration and certificates; HTTPS/reverse proxy and authentication rate limits;
deployment orchestration; database roles/storage/timeouts; logging and
retention; backups with restore rehearsal; real PostgreSQL and ESP32 acceptance
tests. None of these operational procedures is demonstrated by a live deployment
in this repository. A non-root, single-worker Core Dockerfile and `.dockerignore`
are present; README documents environment injection and explicit migrations.
`compose.production.yaml` adds PostgreSQL 17 and Mosquitto 2 on an internal network,
with named data volumes and only loopback API port 8000 published. Core additionally
joins a non-internal frontend bridge to activate Docker port publishing and allow
outbound connectivity; PostgreSQL/Mosquitto stay exclusively on the internal
backend. Recreate Core to apply this topology; restarting is insufficient. Operator-supplied
MQTT certificates (SAN `mosquitto`), password hashes, and ACL use individual
read-only input mounts. A root bootstrap stages broker-owned 0600 copies in
private tmpfs, then Mosquitto drops to its configured non-root user. CA private
keys/CSR/issuance files are not mounted. Host inputs still need OS access controls;
Core uses `SSL_CERT_FILE` with existing verified TLS. No secrets are supplied.
PostgreSQL health gates Core startup; migrations and initial database-role setup
remain explicit. Broker startup is not authenticated MQTT readiness. HTTPS,
reverse proxy, device network access, and certificate automation remain deferred.
Container build/runtime validation must be checked separately from host pytest.

Plan for one Core instance/worker: EventBus, WebSockets, MQTT consumption, and
automation tasks are process-local. Multi-worker coordination is not implemented.

## 8. Known limitations / technical debt

- `/ready` checks database migration history on request, not manual schema drift
  or MQTT/device availability. No automatic migrations run at startup. MQTT
  `connected` is set before subscriptions complete.
- Cleanup has no application-level deadline or independent failure protection.
  Synchronous database work occurs in async paths. Delayed automations are not
  durable, task concurrency is unbounded, and time conditions use host-local time.
- MQTT retained flags/state timestamps are not used for freshness decisions;
  heartbeat expiry is absent. MQTT state reports restart automation depth at zero,
  so the existing in-process depth guard does not cover physical round trips.
- Command IDs are returned by the gateway but discarded by DeviceService; ACKs
  become events without pending-command tracking, outcome progression, or retries.
- Device management CRUD can still write observed `state`/`online`. Numeric state
  validation is incomplete for some sensor fields. API IDs are less restrictive
  than MQTT topic IDs. These need review before commissioning hardware.
- WebSocket sends are sequential without deadlines; access-token expiration is
  checked at handshake, while active-user/membership checks occur for events.
- Refresh revocation and replacement issuance use separate commits. EventLog and
  refresh-session cleanup policies are absent; telemetry is persisted per message.
- No production user-creation CLI/public registration exists. Runtime bootstrapping
  must not substitute demo data or weaken owner/membership boundaries.
- README's version heading, architecture's older v0.6b wording, and protocol §19
  contain historical descriptions. Runtime version remains `0.6.0b1`.
- Legacy `app/automation.py`, `app/models.py`, and `StructureService` are not the
  active application path. Avoid confusing them with current service/package code.
- Frontend, AI control, OTA/provisioning platform, BLE Mesh, Zigbee, and Matter are
  not implemented here and are not required for this deployment foundation.

## 9. Current development milestone

v0.7 production/deployment preparation is in progress, not deployment-complete.
Snapshot branch: `feature/production-deployment-v0.7`; inspected HEAD: `c98cd88`.
Working tree was clean before this status document was created.

Completed work visible in branch history:

- `6cae771`: scene device house isolation and regression tests.
- `7fcac56`: production configuration guards and configuration examples.
- `c98cd88`: PostgreSQL correlation-ID schema correction and migration verification.

The branch builds on merged v0.6b house-scoped RBAC and v0.6a authentication.
Remote deployment state and firmware behavior are unverified.

## 10. Next planned steps

Proposed order; these are pending work, not implemented capabilities:

1. [ ] Extend readiness to MQTT subscription state; database/schema readiness and
       separate liveness are implemented.
2. [ ] Bound shutdown and WebSocket sends; ensure cleanup survives individual failures.
3. [ ] Address real-device freshness/heartbeat, command outcomes, and MQTT loop safety.
4. [ ] Add local operator user bootstrap/recovery and safe logging/retention procedures.
5. [ ] Define a single-worker deployment with secure broker, HTTPS, secrets, and storage.
6. [ ] Run PostgreSQL integration tests, backup/restore rehearsal, and real ESP32
       commissioning/reconnect tests before declaring the first installation ready.

## 11. Repository workflow rules

- Work in feature branches and keep changes scoped.
- Run tests and validation before commits; state skipped/unverified checks clearly.
- No direct unreviewed production changes.
- New migrations must preserve a single Alembic head; do not rewrite applied history.
- Never commit secrets or private infrastructure details.
- AI agents must not commit, push, merge, or create PRs unless explicitly instructed.

## 12. Handoff instructions

First read:

1. [PROJECT_STATUS.md](PROJECT_STATUS.md)
2. [ARCHITECTURE.md](ARCHITECTURE.md)
3. [DEVICE_PROTOCOL.md](DEVICE_PROTOCOL.md)
4. [README.md](README.md)

Then inspect `git status`, `git log`, and the current branch before making changes.
Recheck relevant implementation and tests; this snapshot may become stale. Preserve
existing work, service boundaries, house isolation, and separate human/device
identities. Do not treat protocol requirements or planned steps as implemented.

Last updated: 2026-09-25
Current milestone: v0.7 production/deployment preparation
