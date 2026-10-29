# Core v0.15 — Command Lifecycle API

## Discovery and compatible extension

Before implementation both worktrees were clean. Core was already on
`feature/command-lifecycle-v0.15` at `d3fe1eeb4d31cb79efde69670f462eb50203266f`;
Web is a read-only reference on `main`. Inspected PROJECT_STATUS, ARCHITECTURE,
DEVICE_PROTOCOL §§5–9/19, Web v0.6's blocker report, routes, schemas, DeviceService,
MQTTGateway, EventLog, repositories, RBAC/memberships, migrations, simulator and
physical acceptance tests. The mentor deployment guide was read, not changed.

Existing flow: authenticated house-scoped `/devices/{id}/on`, `/off`, `/state`
delegates to DeviceService. With MQTT enabled it validates and dispatches desired
state, then returns the **old observed DeviceRead**, discarding the transport's
command ID. MQTT creates IDs; ACK is audit history, and valid changed state updates
the device separately. Equal state produces no state-change event. Resident has
device control but no event-read access. Browser-native `/ws` authentication remains
unavailable because it requires an Authorization upgrade header.

The extension is deliberately separate: new versioned relay-only routes, a durable
attempt row before publication, direct gateway evidence callbacks and existing
house permissions. Legacy routes, virtual/no-transport behavior, scenes, automation,
wire envelopes, EventLog semantics and deployment defaults remain unchanged.

## Actual HTTP API

### Submit

```http
POST /v1/devices/relay-01/commands
Authorization: Bearer <in-memory-access-token>
Content-Type: application/json
Idempotency-Key: 3cd20ebe-2d7d-49d9-8905-d3312206f971

{"on":true}
```

- Only a JSON boolean `on` is accepted; missing/extra fields, `1`, strings,
  dimming, curtain positions and arbitrary state are rejected with 422.
- Device must be `type=relay` and advertise `on_off`. Physical identities must be
  active under the existing binding guard. Reported offline rejects with 409;
  disabled/disconnected MQTT rejects with 503. These preflight rejections create
  no command and do not publish. Device online is only Core's reported flag:
  heartbeat/freshness guarantees are not added.
- Bearer identity plus **device.control** in the device's actual house is required.
  There is no client-supplied house/command/correlation ID or permission override.
- Required key: 16–128 ASCII letters/digits/underscore/hyphen; UUIDs are suitable.
  Store one key per absolute desired-state intent before issuing the request.

HTTP **202** returns the current persisted receipt. Typical newly published result:

```json
{
  "command_id": "0c699eb2-3dd0-4271-96af-5eff74c4c9dc",
  "correlation_id": "bc168893-9b68-4bb8-b1e7-3a9c8311e69b",
  "house_id": "house-01",
  "device_id": "relay-01",
  "requested_on": true,
  "status": "dispatched",
  "ack_status": null,
  "reported_on": null,
  "failure_code": null,
  "created_at": "2026-10-29T10:00:00Z",
  "updated_at": "2026-10-29T10:00:00.020Z",
  "expires_at": "2026-10-29T10:00:30Z",
  "dispatch_started_at": "2026-10-29T10:00:00Z",
  "dispatched_at": "2026-10-29T10:00:00.020Z",
  "ack_received_at": null,
  "state_received_at": null,
  "completed_at": null
}
```

IDs are independently server-generated UUIDs. **Initial durable status is queued**
before network I/O; 202 is not completion. POST may return queued on a concurrent
idempotent replay, dispatched after publication, failed after publish uncertainty,
or even applied if both verified evidence callbacks completed before the response.
Its status always comes from the durable row, never from desired state alone.

### Read result

```http
GET /v1/houses/house-01/commands/0c699eb2-3dd0-4271-96af-5eff74c4c9dc
Authorization: Bearer <in-memory-access-token>
Cache-Control: no-cache
```

HTTP **200**, same `CommandRead` DTO. Use fresh reads (`fetch` cache `no-store` in
Web's existing transport). **device.read** in the URL house is required; the record
must also belong to that house. It is not an issuance permission or audit grant.
Resident can read results without getting `/events` access. House members with read
permission may inspect house command results, not only attempts they submitted.

DTO contains only the fields shown above. `requested_on` is intent; `reported_on`
is the most recent eligible correlated boolean observed during this attempt, not
a promise of the device's current value. No user IDs, idempotency hash, credentials,
raw MQTT diagnostic message, device metadata or arbitrary broker exception is returned.
All timestamps serialize in UTC. `ack_status` is null or accepted/applied/rejected/failed.

Errors retain FastAPI's `detail` conventions: 401 invalid/missing auth; 404 missing
or foreign resource; 403 insufficient permission in a member's house; 409 offline,
active physical admission failure, busy device or key conflict; 422 invalid/unsupported
intent; 503 unavailable MQTT. Invalid bodies/headers use normal validation DTOs.
Superuser metadata grants no house bypass. Authorization occurs before replay.

## Lifecycle and verified completion

| Status | Meaning / transition |
| --- | --- |
| queued | IDs, actor/house/device, desired boolean and deadline committed before one publish attempt. Not evidence of delivery. |
| dispatched | Publish await returned, or a positive device ACK was observed. Broker acknowledgement alone is not device acceptance. Remains pending even with ACK accepted or only one completion proof. |
| applied | Before deadline, both **ACK applied with exact command_id + correlation_id + house + device** and validated **non-retained live state with the same correlation + house + device and desired boolean** have been processed. Either arrival order works. |
| failed | Matching rejected/failed ACK, or publication could not be confirmed. Inspect failure_code; publish failure is unknown execution, not proof that nothing happened. |
| timed_out | Deadline passed before both proofs. Execution is unknown; no cancellation or OFF is implied. |

`failure_code`: null, `device_rejected`, `device_failed`, `publish_unconfirmed`,
`deadline_exceeded`. ACK's rejected/failed maps to failed while preserving
`ack_status`; no arbitrary error string or code is copied into the response.

Callbacks operate after existing topic-house/device admission and state validation.
State evidence is recorded even when DeviceService's current boolean does not
change, without fabricating another state-change event. EventLog remains history;
its best-effort persistence is not used as the completion oracle.

Unknown IDs, wrong command/correlation/house/device, retained ACK/state bootstrap
and events after deadline cannot confirm. ACK accepted cannot regress applied ACK
evidence. Duplicate ACKs do not advance deadlines. A negative ACK before complete
verification fails closed. Once applied/failed/timed_out, the lifecycle record is
immutable, including after contradictory or duplicate late reports. Existing audit
history may still record those ACKs; valid late state may still update DeviceRead.

Timestamps from devices are **not** trusted for authorization or ordering. Unpredictable,
unique persisted IDs, admitted topic identity, non-retained receipt and a server-clock
deadline anchor evidence. Same-correlation state reports received before completion
replace that attempt's reported boolean in processing order; a nonmatching boolean
cannot satisfy completion. This is verified **protocol-reported execution**, not a
GPIO/electrical attestation or guarantee against a compromised authorized device.

Applied is a historical outcome. A later automation, legacy command or physical
change may alter the current device state. Read the ordinary device endpoint separately.

## Idempotency, conflict and delivery semantics

The durable unique key is `(house_id, requested_by, SHA256(Idempotency-Key))`.
Same authorized caller/key and exact device/desired boolean returns the same record
without republishing, including terminal records. Reusing a key for a different
intent in that house gives 409. Different users/houses have independent key scopes.
Physical admission is still checked for POST replays; GET remains available with
read permission if a physical identity is subsequently inactive.

A device row lock serializes creation and a database partial unique index permits
only one queued/dispatched **new-API** command per device. Another key while pending
gets 409, even for the same desired value. Expired attempts are materialized before
a new one is inserted. Concurrent same-key calls publish once; conflicting calls
are not silently reported as successful. These locks are committed/released before
MQTT network I/O. Legacy routes and automations remain separate dispatch-only callers;
this is not an exclusive lock across every controller.

- Fixed command deadline: **30 s from creation**. Fixed publish await bound: **5 s**.
- No MQTT/HTTP retry worker, automatic resend, command cancellation or outbox.
  QoS 1 may deliver duplicates; existing device deduplication remains responsible.
- MQTT disconnected/no transport: reject before insertion. Disconnect after insertion
  or publish exception/timeout: failed + publish_unconfirmed if no verified terminal
  result already exists. The device may still execute; never automatically retry.
- Lost/cancelled HTTP/process after commit: record survives, possibly queued; a
  same-key POST does not resend. Before commit no command is published.
- Restart/reconnect: restore subscriptions and persisted evidence; no replay.
  Retained bootstrap is not fresh completion evidence. Fresh evidence can complete
  an existing attempt only within its original deadline.
- Missing ACK/state: logical deadline survives restart. GET, matching ingress and
  subsequent submissions check stored `expires_at` and commit timed_out. No in-memory
  timer is required. A silent unobserved row can retain its pending status in storage
  until that check; it is never reported pending by a GET after deadline.
- Core must process evidence before the deadline; delayed handling can conservatively
  time out. Accurate server time/NTP is required. `completed_at` for timeout is when
  it was materialized, while `expires_at` is the effective deadline.
- A timed-out command can still execute later (v1 has no cancellation/deadline field
  tied to this API's 30 s). Its lifecycle stays timed_out while ordinary state updates.
  A new key is a new explicit intent, not a guaranteed safe replay of the old attempt.

## Database / migration / operational boundary

Explicit Alembic head: **0006_command_lifecycle**, after 0005_device_onboarding.
Adds `device_commands`, bounded status/ACK/deadline checks, unique correlation IDs,
actor/house-scoped idempotency, house/time lookup and a unique partial pending-device
index. Timestamps and observed evidence live in PostgreSQL, not process memory.
FK RESTRICT retains referenced house/device/user history; explicit deletion of a
referenced entity now conflicts until an independently designed retention/archive
procedure exists. There is no automatic history deletion. Downgrade refuses a
populated ledger; no destructive reset or startup migration is added.

Migration was applied to disposable test databases only: fresh real PostgreSQL and
fresh/incremental SQLite (including preservation of existing records/model comparison).
Existing production/local databases were **not** migrated or restarted. Their images,
volumes, credentials, compose files and defaults were not changed. Deploying this
code separately requires an operator backup, explicit migration in the managed
environment and readiness verification; the new packaged head will otherwise make
`/ready` report schema_revision_mismatch. Do not combine this development work with
an unsolicited installed-stack update.

## Tests actually performed

| Check | Result |
| --- | --- |
| Full normal pytest | **350 passed, 13 skipped**, 106.14 s. Skips are opt-in/environment-gated cases, not claims of execution. |
| New lifecycle/schema tests | **27 passed**: actual FastAPI/RBAC/services/gateway and migrated SQLite, only MQTT network fake. |
| Real isolated Docker acceptance | **1 passed, 55.72 s** using PostgreSQL 17, Core, Mosquitto TLS and existing software Relay. |
| Ruff app/simulator/tests/Alembic | Passed |
| Alembic heads | Sole head 0006_command_lifecycle |
| Compose config | Local, production, production + ARM64 overlay passed with explicit dummy process inputs and an empty env file; no installed secrets read. |
| Whitespace | git diff --check passed; new files checked separately. |

Unit/integration cases cover both evidence orders, equal-state OFF, exact IDs,
accepted/negative/duplicate/late/unknown/retained ACKs, mismatching state, missing
ACK, durable timeout, cancelled publish, bounded publish failure, offline and broker
unavailable, idempotency/rapid conflicts, invalid/unsupported bodies and device types,
resident result read without audit/control escalation, cross-house IDs, restart,
schema constraints and populated-downgrade refusal. Legacy regression remains green.

Live test reuses the existing v0.12 **exclusively owned** production-component fixture,
with random project/image names, isolated volumes, non-superuser app DB role, ephemeral
loopback ports and TLS credentials. It proves actual HTTP concurrent idempotency,
MQTT receipt IDs, relay ACK/state, unchanged OFF, resident/IDOR, Core + PostgreSQL
restart, missing evidence timeout, no resend and late ACK/state without false applied.
Independent fixture PostgreSQL reads found **4 applied + 1 timed_out**, zero applied
rows lacking matching ACK/state evidence. The relay ended OFF. No intercepted browser
fixtures or frontend implementation are involved.

Before/after inventory matched: **4 pre-existing containers, 17 volumes, 3 running**;
no extra fixture container/volume remained. Only the fixture's unique resources were
removed. A previously absent `python:3.12-slim-bookworm` test-build dependency was
fetched and remains in the image cache. Existing images were not deleted.

Initial validation found outdated migration-head assertions and an Alembic fileConfig
logger-disable leak in the reused test helper; assertions/test isolation were corrected.
The first live attempt reached the restart phase, then the harness used an old dynamic
host port. It was corrected to rediscover the owned port; the full live rerun passed.
No runtime workaround or installed deployment change was used. Normal warnings:
Starlette/AnyIO and Alembic's existing legacy path_separator deprecations (30 total).

Reproduce only in the development checkout / isolated fixture:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app simulator tests alembic
$env:RUN_COMMAND_LIFECYCLE_DOCKER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_command_lifecycle_live.py -q
git diff --check
```

The live fixture needs Docker Desktop/Linux containers and the existing images
listed in `tests/test_physical_acceptance_live.py`; it refuses implicit pulls.
No physical hardware, ARM64 runtime, LAN routing or installed stack acceptance is claimed.
Full end-to-end delivery guarantees, device durable deduplication, heartbeat freshness,
arbitrary capabilities, scene lifecycle and history retention remain outside v0.15.

## Next minimal Web integration (not implemented)

Keep Signature/mock behavior. Add a separate relay receipt/result domain path;
do **not** simply set the existing snapshot-returning repository to writable.
Submit only relay on_off with one UUID key per absolute intent; keep observed state
unchanged while pending. Poll the actual house-scoped GET with fresh authenticated,
abortable reads until its status is terminal. ACK accepted is pending; applied needs
the server's verified status. Publish-unconfirmed/timed_out mean unknown execution.
An aborted browser request cancels observation, not the physical command. Bind all
receipts/keys/results to user/house/device and ignore responses after navigation,
house change or session expiry. Do not automatically replay commands or enable
brightness/curtains/scenes. Existing 401/403/404 handling remains authoritative;
read permission alone must not be treated as issuance permission.

## Exact files / Git

Modified: `README.md`, `PROJECT_STATUS.md`, `ARCHITECTURE.md`, `DEVICE_PROTOCOL.md`,
`app/api/routes.py`, `app/core/errors.py`, `app/main.py`, `app/models/__init__.py`,
`app/transports/base.py`, `app/transports/mqtt.py`, `tests/test_schema.py`,
`tests/test_onboarding_schema.py`, `tests/test_physical_acceptance_live.py`.

Created: `COMMAND_LIFECYCLE_V0.15.md`, `alembic/versions/0006_command_lifecycle.py`,
`app/api/commands.py`, `app/models/device_command.py`,
`app/repositories/command_repository.py`, `app/schemas/commands.py`,
`app/services/command_service.py`, `tests/test_command_lifecycle.py`,
`tests/test_command_lifecycle_live.py`, `tests/test_command_schema.py`.

Core: `feature/command-lifecycle-v0.15`, 13 modified + 10 untracked, nothing staged.
Web: `main` at `f9a97d37951cfb835b682e67eef0e6d916741c81`, clean and unchanged.
No branch changes, commits, pushes, merges or PRs.
