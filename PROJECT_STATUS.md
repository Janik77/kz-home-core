# KZ Home — Project Status

Canonical development handoff. This is a repository snapshot, not certification
of a deployed installation. Update it when capabilities or validation change.

## Latest block — v0.12 software physical identity acceptance (2026-10-04)

Branch: `feature/software-device-e2e-v0.12`, starting clean at merged v0.11.
[Operator commands and exact acceptance boundaries](simulator/PHYSICAL_ACCEPTANCE.md).
The new opt-in `simulator.physical_acceptance --run` uses the existing commissioned
identity, v1 Relay and API/service/gateway/event persistence path; no Core runtime,
schema, production Compose, firmware or local installation inputs were changed.
The v0.8 runner's shared HTTP/evidence/scenario helpers now accept explicit paths
and house/device IDs while preserving its fixed-identity CLI and default behavior.

The runner requires ready Core, authenticated device/event access and an active
physical relay with permanent binding. It rejects enabled rules referencing that
relay. ON/OFF requires actual MQTT receipt, unique IDs, matching applied ACK and
correlated state events plus resulting GET. A saved ON receipt replay after OFF
uses the relay's existing cache and sends only its original ACK; unchanged OFF
reports must not add state events. Offline subscription removal/status checks
prove HTTP dispatch does not become false ACK/state confirmation during a bounded
window. Ordinary GET/list/events are checked for private fields and known secrets.
Success leaves the existing active binding OFF/offline and is repeatable. No
lifecycle, credential, broker configuration or production seed is performed.

`tests/test_physical_acceptance.py` exercises migrated SQLite and actual API/RBAC,
services, gateway, repositories and EventLog with only MQTT network I/O replaced.
The opt-in `tests/test_physical_acceptance_live.py` derives an exclusively owned
fixture from effective production Compose: non-superuser PostgreSQL, explicit
migrations/inventory registration, human API claim/activation, real TLS Mosquitto
and exact v0.11 ACL. It tests negative broker/RBAC access, two complete runner
passes, independent PostgreSQL state/ACK/event/lifecycle/security-audit reads,
and survival of Core recreation/PostgreSQL restart. Random resource names and
temporary test inputs preserve `deploy/local` and installed volumes; no PostgreSQL
port is published and fixture HTTP/MQTT ports bind random loopback ports only.

**Verification boundary:** offline integration and the real isolated Docker
acceptance now pass. On 2026-10-04, after Docker Desktop became available, the
prepared test completed **1 passed in 93.08 s** without a code fix. This verifies
the complete PostgreSQL/Core/Mosquitto TLS/physical-identity software relay path,
two ON/OFF cycles, correlated ACK/state, replay/offline checks, negative broker
ACL/authentication and human RBAC/IDOR checks, independent database evidence and
state/event survival after PostgreSQL restart and Core recreation. It does not
verify physical hardware, LAN deployment or the existing installation. Nothing
was deployed or changed in that installation; its original container/network IDs
and volumes were preserved.

The first resumed attempt stopped at the local-image prerequisite because the
existing Dockerfile's `python:3.12-slim-bookworm` base image was absent. Fetching
that test dependency resolved setup; no application or test code changed. The
fixture removed its containers, networks, volumes, image and private inputs.
Both newly created workspace test directories and the newly fetched base-image
tag were also removed; no labeled acceptance resources remained.

Final checks for this block:

- Focused physical/v0.8 E2E/onboarding/commissioning/relay suite: **190 passed in
  27.43 s**.
- Full normal pytest: **311 passed, 7 skipped in 43.34 s**. Four skips require a
  dedicated PostgreSQL test URL; three are opt-in Docker checks. Five deprecation
  warnings: the existing Starlette/AnyIO warning and four Alembic legacy
  `prepend_sys_path` configuration warnings during explicit fixture migrations.
- Effective production Compose/default LAN override validation, with dummy
  process inputs and no daemon required: **1 passed in 2.05 s**.
- Opt-in full-stack validation: **1 passed in 93.08 s** with Docker Client/Linux
  Server 29.8.0. The earlier daemon-unavailable attempt and resumed missing-image
  setup failure are resolved. No code changed during resumed validation, so the
  full regression result above remains the latest; it was not rerun for doc-only
  updates. Windows fixture temp directories were new and explicitly cleaned.
- Ruff over app/simulator/tests/Alembic/commissioning tool and compileall: passed.
  Tracked and new-file whitespace checks: passed. Sole Alembic head remains
  `0005_device_onboarding`; no revision was added or startup migration introduced.
- PyCharm MCP inspections: no errors in changed Python files. IDE build reports
  success with limited build diagnostics. No secret/build artifacts appear in
  normal Git status; generated fixture inputs and local installation files are
  ignored. Initial clean tree now has six modified and five new requested files;
  no commit, push, merge, PR or deployment was performed.

Core's existing command-outcome tracking, retries, heartbeat/freshness and durable
simulator replay gaps remain. Duplicate ACKs are stored; unchanged-state reports
do not create state events. The runner deliberately replays a saved receipt in
the handler; it does not prove broker retransmission. HTTP 200 for an offline
command means dispatch, not physical success. GPIO/electrical behavior, firmware
durability/clock/reconnect and actual LAN DNS/SAN/VLAN installation remain unverified.
The next smallest block is operator acceptance on the already commissioned
installation using the documented one-off runner, followed by physical acceptance
when hardware is available. Hardware is not required for that operator software run.

## 1. Project goal

KZ Home is a smart-home platform for organizing houses and controlling devices,
scenes, and automations. `kz-home-core` is its backend: HTTP/WebSocket access,
persistence, authorization, automation execution, and device transport integration.
There is no frontend in this repository. ESP32 firmware is maintained separately
in `kz-home-firmware` (project context); its current implementation and hardware
compatibility have not been verified here. Project context confirms separate
ESP32-C6 firmware v0.9 is implemented and merged; physical acceptance is deferred.

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
require broker authentication. Production Compose supplies the required Core
credentials and Mosquitto disables anonymous access. The two E2E identities have
exact directional device ACLs: relay reads its own set and writes state/ACK/status;
motion writes only its own state/status and has no subscriptions. EventLog
redaction is key-based, not a guarantee that arbitrary strings or exception
traces are secret-free.

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
  -> 0005_device_onboarding
```

The sole current head is `0005_device_onboarding`. It adds private physical
inventory and permanent house binding; downgrade refuses nonempty inventory.
The previous revision `0004_event_log_correlation` widens the indexed,
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
`DATABASE_URL`. Without explicit configuration they skip. The optional PostgreSQL
migration/length-enforcement tests remain unverified in this audit. Separately,
live operator E2E acceptance has verified PostgreSQL-backed state and event
persistence on the production Compose stack; that does not prove every migration
or restore path.

Validation commands used by the project:

```text
python -m pytest
python -m ruff check app simulator tests alembic
python -m compileall -q app simulator tests alembic
git diff --check
python -m alembic heads
```

Final audit results (2026-09-30): focused MQTT/simulator/E2E/bootstrap/logging
checks **74 passed**; full normal suite **156 passed, 2 skipped**, with one
Starlette/AnyIO deprecation warning. Both skips are optional PostgreSQL migration
tests. Ruff, compileall and `git diff --check` passed; Alembic reports the sole
head `0004_event_log_correlation`. Pytest ran with its cache provider disabled
because the workspace cache is inaccessible; no tests were excluded by that flag.
Normal pytest uses offline transport/SQLite; neither broker-backed acceptance
runner nor E2E bootstrap is invoked by application startup or test collection.
Live evidence below is operator-reported; this audit does not access Docker.
The audit fixed bootstrap's echoed-password fallback and strengthened rollback,
unchanged-password and exact ACL regression checks. Both passing E2E runners
and simulator runtime implementations were left unchanged during the final audit.

v0.10 foundation validation (2026-10-01): pre-change baseline **156 passed,
2 skipped**; focused onboarding/schema/auth/RBAC/MQTT/E2E/bootstrap suite
**143 passed, 4 skipped**; full suite **233 passed, 4 skipped**. The four skips
are opt-in dedicated PostgreSQL migration/length and concurrent-claim tests;
`TEST_POSTGRESQL_URL` was not supplied. The existing Starlette/AnyIO deprecation
warning remains. Ruff and compileall passed; `git diff --check` passed and Alembic
reports the sole `0005_device_onboarding` head. IDE inspections report no errors
in changed Python files; existing type/IDE-configuration warnings remain. IDE
build reports success but cannot collect full build diagnostics. No migration
was applied to an installation; no broker-backed acceptance rerun or secret-file,
deployment or firmware changes were performed by this block.

v0.11 commissioning validation (2026-10-02): focused commissioning/onboarding/
auth/RBAC/MQTT/simulator/E2E/bootstrap checks **218 passed** in 33.24 seconds;
full normal suite **287 passed, 6 skipped** in 39.63 seconds. Four skips remain
dedicated PostgreSQL cases; two are opt-in isolated Docker checks. The latter
passed separately: **2 passed** in 10.43 seconds, using Compose 5.5.1 and Docker
29.8.0 with only temporary fixture TLS/password inputs and a uniquely named broker
on a random loopback port. Checks covered effective Compose defaults/LAN merge,
missing bind-address refusal, real TLS/hostname/untrusted-CA checks, exact ACL
delivery/write isolation, deliberate ACL broadening detection, existing relay v1
command/ACK/state, credential rotation with old live-session termination, and
revocation preserving the Core account. All test containers were removed.

Ruff (`app simulator tests alembic deploy/commission_device.py`), compileall for
those paths and whitespace checks passed; sole Alembic head remains 0005.
Pytest cache was disabled without excluding tests. The existing Starlette/AnyIO
deprecation warning remains. IDE Python inspections found no errors; warnings
remain for dev requirements recognition and the Windows asyncio policy's future
deprecation. IDE build reports success with limited diagnostics. The Compose IDE
IP validator flags the unresolved `host_ip` environment expression; real Compose
interpolation/configuration checks passed. No production image was built/applied,
no installation migrations ran, and no `deploy/local`, real secrets/certificates,
firmware or installed services were changed. Installed LAN/Core/PostgreSQL and
physical hardware acceptance remain operator-run.

## 7. Production/deployment readiness

**Already implemented:** configuration guards above; external Alembic migrations;
PostgreSQL-compatible model types; lifecycle-managed MQTT reconnect; shutdown task
cancellation and engine disposal; disabled FastAPI debug responses; domain error
handlers; authentication, RBAC, and scene house preflight. `/ready` checks lifespan
initialization, database access, and exact agreement with the packaged Alembic head;
failures return 503 with fixed safe reasons. `/health` remains lightweight liveness.

**Operator-verified v0.8 scope:** broker TLS/authenticated connectivity, reconnect
and subscription restoration after broker restart, and both relay and automation
round trips with persisted ACK/state history (details in §9).

**Still deferred:** MQTT/device readiness checks; bounded and resilient shutdown;
general production user recovery; HTTPS/reverse proxy and authentication rate
limits; DB timeouts; broad secret-safe logging/retention; backups with restore
rehearsal; dedicated PostgreSQL migration tests and physical ESP32 acceptance.
A non-root, single-worker Core Dockerfile and `.dockerignore` are present; README
documents environment injection and explicit migrations.
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
reverse proxy and certificate automation remain deferred. Base Compose is
internal-only for device access; optional LAN commissioning is described below.
For a physical relay, v0.11 adds [explicit operator commissioning](DEVICE_COMMISSIONING.md):
generated local ACL selected through `MQTT_ACL_FILE`, native interactive password
administration, and an optional `compose.mqtt-lan.yaml` override publishing only
TLS 8883 on required `MQTT_LAN_BIND_IP`. Mosquitto retains backend and joins a
separate publishing bridge. PostgreSQL/Core exposure stays unchanged. LAN DNS/SAN,
firewall and device-VLAN checks remain operator-run; base MQTT stays internal-only.
Container build/runtime validation must be checked separately from host pytest.

Plan for one Core instance/worker: EventBus, WebSockets, MQTT consumption, and
automation tasks are process-local. Multi-worker coordination is not implemented.

## 8. Known limitations / technical debt

- `/ready` checks database migration history on request, not manual schema drift
  or MQTT/device availability. No automatic migrations run at startup. MQTT
  `connected` is set only after all inbound subscriptions complete. MQTT retries
  use 1–30 second exponential backoff, reset on inbound traffic; already reported
  broker disconnect errors are not logged again during adapter cleanup.
- Core cleanup has no application-level deadline or independent failure protection.
  Cancellation during aiomqtt connection waits for entry to settle before closing
  a late connection; DNS/executor/socket behavior can delay shutdown. E2E coroutine
  phases and I/O have timeouts, but these are not a hard process-exit guarantee
  during OS-level I/O stalls or cleanup. No new retry/queue subsystem is present.
  Synchronous database work occurs in async paths. Delayed automations are not
  durable, task concurrency is unbounded, and time conditions use host-local time.
- MQTT retained flags/state timestamps are not used for freshness decisions;
  heartbeat expiry is absent. MQTT state reports restart automation depth at zero,
  so the existing in-process depth guard does not cover physical round trips.
- Command IDs are returned by the gateway but discarded by DeviceService; ACKs
  become events without pending-command tracking, outcome progression, or retries.
- Legacy device management CRUD can still write observed `state`/`online`; physical
  records reject these management writes. Numeric state
  validation is incomplete for some sensor fields. API IDs are less restrictive
  than MQTT topic IDs. These need review before commissioning hardware.
- WebSocket sends are sequential without deadlines; access-token expiration is
  checked at handshake, while active-user/membership checks occur for events.
- Refresh revocation and replacement issuance use separate commits. EventLog and
  refresh-session cleanup policies are absent; telemetry is persisted per message.
- `app.bootstrap_e2e` explicitly provisions dedicated E2E ownership/records using
  existing services and hashing. It reuses compatible records without resetting
  passwords/state, rejects conflicts and rolls back the whole transaction. It
  requires hidden terminal password input and is never called at startup. General
  production user recovery/public registration remains absent. Block 2 instead
  provisions through separate RBAC-protected API transactions; a partial failure
  may leave compatible records for a safe rerun, not a single atomic rollback.
- Runtime version remains `0.6.0b1` and the Compose Core image label `v0.7-local`;
  v0.8 is the current development milestone, not a retagged/deployed image claim.
- Legacy `app/automation.py`, `app/models.py`, and `StructureService` are not the
  active application path. Avoid confusing them with current service/package code.
- Frontend, AI control, OTA/provisioning platform, BLE Mesh, Zigbee, and Matter are
  not implemented here and are not required for this deployment foundation.

## 9. Current development milestone

v0.8 broker-backed device/automation acceptance is complete at the simulator level
and merged (local history includes PR #17). v0.10 onboarding is merged (PR #18).
This block works on `feature/device-commissioning-v0.11`;
no commit/push/merge/PR is performed.

v0.10 adds trusted inventory registration, a consumed hashed claim code, immutable
house binding, approved relay profile, atomic claim/lifecycle audit, and Core
activation/inactive/revoked admission. Owners/installers use the existing RBAC
matrix's `device.onboard` permission. Ordinary reads expose no onboarding proof
or broker credentials. Physical transfers/deletion and CRUD profile/state/online/
metadata overwrites are rejected; legacy/E2E records keep existing behavior.
See [implementation and exact external credential boundary](DEVICE_ONBOARDING.md).
Mosquitto administration and credential issuance/rotation/revocation remain
operator-managed, and physical hardware E2E is not a blocker for this foundation.

v0.11 provides one-device host-side ACL grant/check/revoke with immutable-binding
conflict checks, an opt-in live broker probe and exact operator password/rotation/
revocation steps. The device principal `kzdevice-{device_id}` reads only its own
set and writes ack/state/status; no telemetry or wildcard permission is granted.
Core/E2E principals, existing RBAC, lifecycle, TLS guards and revision 0005 remain
intact. A broker-admin subsystem, plaintext Core credential storage, firmware,
Wi-Fi delivery and production CA issuance are not added. The optional LAN bridge
requires explicit private IP preflight, firewall policy, DNS and certificate SANs
`mosquitto` plus `mqtt.kzhome.home.arpa` (or a consistently selected local DNS name).
See [exact commissioning steps and acceptance boundary](DEVICE_COMMISSIONING.md).

The operator reports these live checks passed on the production Compose stack:

- Verified broker TLS and username/password authentication.
- Core reconnect and inbound subscription restoration after Mosquitto restart.
- Persisted relay=false -> HTTP ON -> Core MQTT -> standalone relay -> matching
  ACK/state -> persisted true -> HTTP OFF -> matching ACK/state -> persisted false.
- Fresh MQTT motion=true -> Core `report_state` -> existing AutomationService ->
  MQTT relay set -> simulator apply -> correlated ACK/state -> persisted relay=true.

The two opt-in runners use existing Protocol v1 and public APIs; no command
tracking, retries, queue, new automation engine or production seeding was added.
See [operator procedures and evidence requirements](simulator/E2E_ACCEPTANCE.md).
No physical ESP32/hardware, firmware compatibility or general production-readiness
claim follows from these simulator results.

## 10. Next planned steps

Proposed order; these are pending work, not implemented capabilities:

Next smallest block: operator-run v0.11 commissioning on the intended stack,
including LAN DNS/SAN/firewall isolation and a physical-identity software relay
ON/OFF -> correlated persisted ACK/state check. Hardware need not block that
software check; real ESP32 acceptance follows when hardware is available.

1. [ ] Extend readiness to MQTT subscription state; database/schema readiness and
       separate liveness are implemented.
2. [ ] Bound shutdown and WebSocket sends; ensure cleanup survives individual failures.
3. [ ] Address real-device freshness/heartbeat, command outcomes, and MQTT loop safety.
4. [ ] Add general operator user recovery and broad safe logging/retention procedures;
       dedicated E2E bootstrap and application INFO logging are implemented.
5. [ ] Complete HTTPS, secret rotation and operational storage procedures around the
       existing single-worker Compose deployment and verified secure broker.
6. [ ] Run dedicated PostgreSQL migration tests, backup/restore rehearsal, and real ESP32
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

Last updated: 2026-10-02
Current milestone: v0.11 one-relay operator commissioning bridge; hardware acceptance deferred
