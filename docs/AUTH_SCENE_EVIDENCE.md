# Auth race investigation and scene execution boundary

Baseline: Core #36 `6783ce0347004d101ec2ffa5eb60c48946901786`.
This branch changes neither JWT policy, MQTT envelopes, database schema nor physical gates.

## Reported 401/401

The reported natural-clock failure has **not been independently reproduced**.
The original failing log and clock telemetry are unavailable in this workspace.
A valid initial GET followed by two 401 PATCH responses happens before scene/automation
validation or the atomic revision update; it does not prove a lost-update defect.
`AuthenticationService.access_user` returns the same public error for rejected JWT,
missing user and inactive user. `TokenCodec` requires signature, sub/type/iat/exp/jti,
checks access type and uses PyJWT 2.10.1 without clock leeway.

New deterministic cases use actual login/auth and concurrent HTTP requests:

* Same valid clock: both authenticated reads succeed.
* Move verification time two seconds before signed iat after successful GET:
  both requests fail 401 (`ImmatureSignatureError`).
* Move verification time exactly to signed exp: both fail 401 (`ExpiredSignatureError`).
* PostgreSQL repeats the two rejection cases for scene and automation PATCH and
  verifies revision/name remain unchanged. The existing natural-clock race still
  requires exactly one 200 and one 409 after both requests validate revision 1.

These are controlled possible causes, not proof of the historical cause. No auth fix
is justified without that evidence. The race assertion now reports response status/body
and only JWT exception class names, never tokens, claims, identifiers or exception text.
A future failure with no JWT class points investigation toward user/session/database
state rather than token time. No retry, xfail, new skip, auth bypass or leeway is added.
The first local targeted collection also exposed the pre-existing requirement to set
DATABASE_URL before importing app.main; commands below supply an in-memory test URL.

## Reproduced scene defect and minimal fix

Previously SceneService.run checked all house references, then executed actions one by
one. With valid action A followed by unsupported action B, A already changed local DB
state or published a command before B was rejected. Both cases fail the new regression
on baseline. Admission now validates every action using the existing physical binding,
active/command-contract gates and the correct local/transport state validator before
starting A. Each actual update still independently rechecks its guards. Tests cover
active/inactive synthetic physical inventory, invalid later fields, and capability
change after the first publish. No hardware is contacted or gate replaced.

This is preflight, **not atomic execution**. Changes/failures after admission can still
leave a partially dispatched scene. There is no compensating OFF or automatic replay.
Current HTTP response shapes and accepted valid scenes remain compatible.

## What would constitute a confirmed scene run

Current route: authenticated SCENE_RUN permission -> scene house validation -> preflight
-> sequential DeviceService.update_state -> scene_started -> SceneRead HTTP 200.
With MQTT, update_state returns the old observed state after send_command; it does not
create a DeviceCommand receipt. A scene_started event or HTTP 200 is not success evidence.
The regression explicitly sends two commands without ACK/state and proves the curtain
remains at its old observed position. Local no-transport DB updates are emulation only.

Existing software relay command API is stronger: POST /v1/devices/{id}/commands with
Idempotency-Key persists intent before publishing; GET /v1/houses/{house}/commands/{id}
returns applied only after matching, non-retained applied ACK and matching Boolean state
within the original deadline. accepted ACK, publish completion, retained state and a
snapshot which happens to equal desired are insufficient. Failed/timed_out is not proof
of physical OFF. This ledger currently supports relay on_off, not arbitrary scene actions.

A separate versioned scene-run API should snapshot scene revision/actions, persist run
and ordered action identities before network I/O, authorize every target, use stable
per-action idempotency and reference existing command receipts. Aggregate success requires
ALL receipts applied; waiting, partial/failed, and unknown outcomes must stay distinct.
GET must be house scoped and cannot infer completion from desired/current equality.
Cancellation must stop only undispatched actions, not claim reversal of delivered ones.
Required tests include lost response/retry, duplicate target intents, negative ACK,
wrong/retained correlation, timeout, restart mid-run, revocation and concurrent edits.

**Deferred design decisions:** behavior after partial failure (stop/continue), repeated
actions on one device, restart recovery and authorization of a persisted/background run.
Existing scene API has no caller/idempotency/run records and generic actions exceed the
relay ledger. Silently routing scenes into it or adding compensation would change control
semantics without an agreed contract. No confirmed-run endpoint is introduced here.
Physical implementation additionally remains blocked by unqualified expiry/permit and
firmware gates; software tests cannot resolve that blocker.

## Automation limits

AutomationService uses the same legacy update_state path; automation_completed currently
means the action loop returned, not ACK/state-confirmed execution. manual_run returns a
rule even when _execute catches an exception and emits automation_failed. Manual run does
not evaluate enabled/conditions as event-triggered execution does. Delays use in-process
asyncio.sleep, no durable queue, run identity, restart recovery or per-action receipt.
MAX_AUTOMATION_DEPTH=8 bounds local event recursion; it is not a general broker loop or
rate-limit guarantee. Conditions read observed snapshots and local server time, not an
atomic condition/actuation transaction. Do not label these runs verified in Web.
Automation has no initiating authenticated principal for event-driven dispatch; selecting
an execution identity/authorization policy and durable failure semantics is a separate
reviewable design dependency. This branch does not expand it or physical permissions.

## Compatibility and checks

Core dependencies #32/#33/#35 and test-only #34 are inherited from #36. #14 is excluded.
Web launcher remains pinned to `fac23c233a1c86dad1661a9e2970cfca9450d00d` (#14/#15).
Newer Web #16 is not covered by that pin. Firmware #12
`4a3bde0fb14c81e5c9b0230bab44a95c099ffa12` still pins baseline Core #36;
its prior successful CI is not evidence of a Firmware rerun against this branch.
No protocol or dependency changes are needed by this preflight fix.

Local software tests (no broker/hardware):

```sh
APP_ENV=test DATABASE_URL=sqlite:///:memory: python -m pytest -q -ra
python -m ruff check --no-cache .
```

Real PostgreSQL tests require a dedicated disposable database (CI supplies it):

```sh
APP_ENV=test DATABASE_URL="$TEST_POSTGRESQL_URL" python -m pytest -q -ra \
  tests/test_schema.py tests/test_onboarding_postgresql.py \
  tests/test_provisioning_postgresql.py tests/test_definition_postgresql.py \
  tests/test_definition_migration.py
```

Existing Core CI builds pinned Mosquitto and runs eight actual SUBACK/TLS cases.
Unified software CI runs the unchanged pinned Web fixture, real PostgreSQL/Mosquitto TLS,
software Relay ACK/state receipts and Chromium scenarios. New scene regression transport
is a recording mock; it is not claimed as real broker scene completion. Exact commit CI
results belong in the draft PR after the jobs finish, not guessed here.
