# Durable software scene runs (caller-driven v1)

This additive API is separate from legacy `POST /scenes/{id}/run`. The legacy route
and automation engine are unchanged. No new automation/background execution identity,
physical qualification, replay worker or compensating OFF command is introduced.

## Supported scope and policy

A run snapshots 1–16 ordered actions from one exact scene revision. Each must be a
software relay with Boolean `on` and no other fields. Repeated devices are supported
sequentially (e.g. ON then OFF). Physical inventory is rejected by the existing guards,
even if mislabelled as software. Transport allowlist, connectivity, online status, house
binding and command gates are checked again by CommandService at actual dispatch.

Creation persists intent without sending anything. Each action requires an explicit
caller request; only the original creator with current SCENE_RUN and DEVICE_CONTROL
permissions may dispatch. A following action is admissible only after every preceding
receipt is `applied`. Negative/unknown outcomes stop progression. This conservative
policy avoids choosing background authorization or automatic recovery semantics.

The original run admits new actions for five minutes; retrying its creation never extends
this window. Each scene command expires at the EARLIER of its original 30-second limit
and the persisted scene deadline; retries never extend either limit.
The run window is admission policy, not a guaranteed actuator deadline. Read operations
continue after expiry and can recognize valid late-arriving receipt evidence only under
the existing command lifetime rules. Timeout is not proof of OFF or non-execution.

## HTTP contract

All endpoints are under `/v1/houses/{house_id}/scene-runs`:

* `POST` with `Idempotency-Key` (16–128 ASCII alphanumeric/underscore/hyphen characters)
  and `{"scene_id":"evening","expected_revision":3}` returns 201 with the persisted
  run. Idempotency is scoped to house/user/key; same intent returns the same run,
  different scene/revision gives 409. A stale definition revision gives the existing
  `definition_revision_conflict` 409. A replay still works after definition deletion.
* `GET /{run_id}` requires SCENE_READ and DEVICE_READ in that house. It derives results
  from persisted claims and exact command receipts, expiring pending receipts via the
  existing CommandService. It never publishes a command.
* `POST /{run_id}/actions/{position}/dispatch` returns 202 with current run evidence.
  It attempts only that zero-based action. Retrying an already claimed position returns
  its evidence and never sends it again. A not-ready position returns 409. 202 is not
  verified execution; inspect the action receipt and aggregate status.

Response includes snapshot scene ID/revision, creation/admission-deadline timestamps,
ordered device/Boolean intent, per-action status/failure code and full `CommandRead`
receipt when one exists. It never includes user tokens, passwords or idempotency hashes.

Action statuses: `ready`, `waiting`, `queued`, `dispatched`, `applied`, `failed`,
`timed_out`, `blocked`, `dispatch_unknown`, `skipped`. `dispatch_unknown` includes the
transient interval while another request owns a claim but has not yet persisted its
command; it can later resolve to a receipt. Otherwise it stays unknown indefinitely.
`blocked` with `dispatch_blocked` means admission failed with no linked receipt, not
that an arbitrary external actor could not have changed the device. Later skipped
positions are not attempted. The aggregate is `ready`, `running`, `succeeded`, `stopped`,
`unknown` or `expired`. Only ALL actions with receipts `applied` yield `succeeded`.

Applied requires the existing non-retained, correctly correlated applied ACK AND matching
Boolean state within deadline. An accepted ACK, equal observed snapshot, desired intent,
HTTP success or broker PUBACK does not qualify. This proves a software protocol result;
it does not qualify physical hardware or prove the state remains unchanged forever.

## Persistence, concurrency and restart

Migration `0010_scene_runs` adds immutable run snapshots, per-position claims and exact
receipt links. House/user idempotency uniqueness handles concurrent creation. A SQL
compare-and-set on `claimed=false` admits only one dispatcher for each position.
The claim is committed before entering network-capable code. CommandService persists
command intent AND the action-to-command link in one transaction before MQTT publish;
the existing single-pending-command-per-device guard remains in force. A direct command
with a guessed stable key cannot masquerade as a scene receipt.

Crash windows are explicit:

1. Before claim: the caller can request that position normally.
2. After claim, before atomic command/link commit: no receipt; `dispatch_unknown`, no
   retry/replay. Some attempts can therefore remain unresolved even when nothing was sent.
3. After command/link commit, before/during publish: GET recovers the same receipt. No
   automatic republish; a lost delivery eventually times out. A command could have arrived.
4. After valid ACK/state: terminal receipt persists; after a new Core process starts,
   the creator may explicitly dispatch the next ready position within the original window.

GET/POST recovery needs the run ID or original creation key. There is no scheduler,
list/pagination UI, automatic resume or cancellation API in this stage. No distributed
atomic scene, exactly-once delivery, rollback, hardware success or bounded physical OFF
is promised. A new user-authorized run after an unknown outcome is a new intent and must
not be silently created as an automatic retry. Scene edits/deletion do not rewrite a
persisted run; permissions and device guards are checked again at every dispatch.

Downgrade refuses nonempty scene-run history so evidence cannot silently be discarded.
Existing endpoints/schema fields and MQTT envelopes are preserved; database migration
is required before using this branch. The pinned Web #20 exposes this lifecycle in the integrated software branch.
Automation remains on its legacy unconfirmed path and is not implicitly migrated.

## Verification and dependencies

Stacked on Core #37 `a2bc83aa2451590f71ac34f9aad1c7c328bccb19`, inheriting #36/#32/#33/#35
and #34 broker coverage. Web fixture is pinned to
`c2c4fa89a0da3652e8e0cca3629300ecfd7e658f` (Web #20 including #17) in this integration.
Persistent profile tooling comes from Core #38; integrated runtime is exactly
Core #39 `f946a07f67f9ccb1e8f62c99860a0842bf3a062b`.
Firmware protocol and gates are unchanged; Firmware #12 still pins older Core #36 and
must be independently tested before claiming this exact new pair is validated.

Software tests use actual API/auth/migrated SQLite and explicit recording MQTT doubles:

```sh
APP_ENV=test DATABASE_URL=sqlite:///:memory: python -m pytest tests/test_scene_runs.py -q
python -m ruff check .
```

Real PostgreSQL race/atomic-visibility/downgrade checks use the existing isolated-schema
fixture, only with explicit `TEST_POSTGRESQL_URL` pointing to a disposable database:

```sh
APP_ENV=test DATABASE_URL="$TEST_POSTGRESQL_URL" python -m pytest tests/test_scene_runs_postgresql.py -v
```

Existing Core CI includes those tests, full regression, migration round trip and actual
Mosquitto SUBACK/TLS. The unified CI additionally runs:

```sh
python -B tools/scene_run_live.py --web-root ../web
```

That command reuses the exact pinned Web fixture's owned PostgreSQL/Mosquitto TLS services,
creates ephemeral credentials/certificates, runs Core in a separate subprocess, races
same-action dispatch, checks actual SET/ACK/state plus SQL links, stops/restarts Core
between ON and OFF, proves recovery without duplicate SET, and checks zero physical
identities. It removes only its generated resources and compares Docker inventory before
and after. It requires Docker already available; no privilege or production configuration
changes are performed. The existing launcher/browser regression still runs afterward.
Exact head and actual CI results are recorded in the draft PR after execution.

## Deadline TOCTOU correction

The initial implementation checked the five-minute window in GET before claiming an
action. Review reproduced a late command after an SQL/scheduler stall. That earlier
head is not considered deadline-safe. The corrected path:

1. Locks the action row BEFORE the admission update, so a row-lock wait cannot preserve
   an earlier eligibility result. The CAS predicate includes the persisted scene deadline
   against both current application time and PostgreSQL `clock_timestamp()` evaluated
   in the update. Transaction-start `CURRENT_TIMESTAMP` is deliberately not used.
2. Rechecks live database/application admission after device locks and asynchronous
   authorization, before creating command intent.
3. Persists an immutable wire/receipt expiry capped to the scene deadline.
4. Rechecks admission inside the publication task, after command/link commit and task
   scheduling. The existing MQTT gateway then rechecks the capped expiry after its own
   asynchronous authorization immediately before handing bytes to the MQTT client.

Expired-before-reservation actions stay unclaimed. If expiration follows reservation
but precedes command persistence, the attempt is blocked without a receipt. If it follows
atomic command/link persistence, the receipt remains authoritative and can become timed_out;
there is no retry, compensation or inferred OFF. Committing a claim can itself finish
late, but that does not authorize subsequent command construction/publication.

SQLite test admission conservatively rejects the boundary millisecond because its database
clock has millisecond precision. PostgreSQL lock-wait tests freeze application time and
use actual database time to demonstrate the second clock is not an old SQL parameter.
The real TLS E2E adds an owned PostgreSQL BEFORE INSERT trigger that holds command/link
persistence until the original deadline +165ms, then verifies zero SET messages, one
bounded timed_out receipt and no replay. The trigger is confined to the disposable fixture
and removed afterward; it is not a runtime migration or production trigger.

These checks do not promise a physically atomic clock-check/network-write operation.
A scheduler, client buffer or network can delay bytes after the last application check;
MQTT publication cannot be unsent. The existing software relay rejects an expired wire
command, while physical gates remain closed. Independent host/broker/device clock faults,
hardware timing and physical OFF are not certified by these software tests.


## Web recovery integration review status

This branch is stacked on unchanged Core #41 `1bb981551d451c31935088dc60ca32f84a3e4ae3`,
with unmodified Web #20 `c2c4fa89a0da3652e8e0cca3629300ecfd7e658f`.
Independent review of Web #20 is complete. Its acknowledgement correction is
integrated here; this does not authorize merge, deploy or physical operation.
New preparation and recent run-ID history are read-only with respect to MQTT:
only a separately authorized action dispatch may publish. The history belongs to
the current browser context, is not browser-persisted, and does not replace durable
Core receipts. EVERY separate new preparation requires explicit acknowledgement that local
history is incomplete, regardless of the selected run. Selecting successful run B
after unknown/timed-out A must not remove this requirement. Local recent IDs are
not a journal of all runs and never prove safe/OFF. Acknowledgement does not prove
that an earlier action did not execute. A real revision conflict still requires
separately reviewing fresh data.
Use a new profile (for example `acknowledgement-profile`); existing earlier Web profile pins
are not migrated or edited. Core runtime and migration remain #39 / `0010_scene_runs`.
