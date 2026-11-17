# Durable device-state automations (software pilot)

Base: Core #47, migration `0013_device_automations`. This is **not** electrical or
physical-device acceptance. Only destinations explicitly on the existing software
MQTT allowlist can receive commands.

## Supported path

Create a disabled rule, validate with `POST /automations/{id}/dry-run`, save edits
with `expected_revision`, enable, then receive a fresh non-retained MQTT state
change. Boolean relay `on` with `eq`/`neq`, AND device-state conditions and 1–16
Boolean relay actions are supported. UI/device capabilities are not a substitute
for server authorization. The rule executes as the user who last saved/enabled it.
All actions are preflighted before admission and rechecked through the existing
command admission mechanism before each dispatch and immediately before publish.

Dry-run does not submit commands, claim actions, or create history. It reports
conditions independently of `enabled`; a disabled rule has `rule_disabled` in
`issues`, hence `eligible=false`. Freshness requires a non-retained state report
with sender timestamp within two minutes (at most five seconds future skew).
A status heartbeat alone cannot make old state current. Retained/old reports do
not trigger automation and invalidate freshness rather than pretending to be live.

Existing general definitions remain stored and editable. Motion/temperature,
time conditions, delays and non-relay actions are not executable in this slice.
Old migrated rules have no `authorized_by` and remain fail-closed until an
explicit authorized edit/enable. The legacy `/automations/{id}/run` returns 409;
it must not bypass durable command receipts by calling `update_state` directly.
HTTP/demo state PATCH is not accepted as an automation trigger. Legacy API tests
now assert these explicit refusal paths; the new suite exercises real ASGI and
MQTT gateway with a software-only wire, not electrical hardware.

## Results and API

- `GET /automations/{id}/runs`: latest 20 durable runs, newest first.
- `GET /automations/{id}/runs/{run_id}`: one result.
- Returned run uses `automation_id`, `automation_revision`, `house_id`, `status`,
  `execution_error`, `created_at`, and the existing scene action/command receipt
  fields. Reads require automation and device read permission in that house.
- `running` is not success. Every action must have both applied ACK and correlated
  state before `applied`; the complete run then becomes `succeeded`.
- Rejection produces `refused`, partially applied run `partial`; lost evidence or
  dispatch ownership produces `unknown`. Never automatically retry an unknown run.
- Preflight refusals and cooldown are durable history entries with reason codes.
  No command was submitted for a refused preflight; its actions list is empty.

## Duplicate, concurrency, loops, interruption

The per-rule/event-correlation digest is persisted with a database unique partial
index, independent of the author. House/definition admission locks serialize
PostgreSQL concurrency. Duplicate delivery after cooldown is still a read/no-op.
A new distinct event within 30 seconds is refused as `rate_limited`; refusal also
counts toward the quiet period. Normal unrelated external state changes after
that period may run again. Event correlation must be unique for each new external
change; reusing a correlation ID intentionally counts as the same event.

For this first slice, **automation chaining is disabled**: a state report whose
correlation belongs to any automation-issued command cannot trigger any rule.
The durable command-to-run link exists before MQTT publication, including after
restart. This prevents same-rule and inter-rule feedback cycles without depending
on an in-memory depth counter. Manual commands with unrelated correlation are
not suppressed by this rule.

Disable, delete, edit, author deactivation or permission revocation stops future
actions at admission. Commands already accepted/in flight cannot be recalled;
their later ACK/state evidence remains visible. No rollback commands are sent.
Changing a definition invalidates its remaining snapshot even when only its name
changed. Re-enable does not resume an old run.

On shutdown the existing SceneExecutor stops ownership. Following a crash a lost
lease makes unfinished dispatch `unknown`. Startup never resumes dispatch or
replays claims. New external events are independent intents, subject to cooldown
and pending-command checks. Reads are side-effect-free with respect to command
publication, including after restart.

## Migration and data preservation

Run the repository's supported backed-up upgrade workflow; never seed an existing
profile or edit its identity metadata. Migration adds nullable authority/state
freshness fields and an additive source discriminator/index to existing scene
history. Existing scene rows default to `source_kind=scene`. Downgrade refuses
when automation run history exists rather than destroying evidence. Database
backup/restore and migration behavior need PostgreSQL integration acceptance in
addition to the migrated SQLite tests. No changes to the old user profile are
required to use an isolated stand.
