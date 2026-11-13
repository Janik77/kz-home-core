# Conditional edits for scenes and automations

This feature is stacked on Core PR #32 (`159eb98f2349b1aa2b8afd313c3a3f1fbf9b6bd2`)
for the existing isolated CI workflow and migration-test corrections. It does
not depend on the SUBACK runtime change in #33 or the test-only #34 integration.
Their transport files are untouched. Apply Alembic `0009_definition_revisions`
after `0008_provisioning_sessions` before running this version. Existing rows
are preserved and start at revision 1; no device state or provisioning data is
changed. Migration execution is an operator step, not performed on production.

## Web contract

Every scene/automation read (including lists, create and update responses) now
includes a positive integer `revision`. When opening an editor, retain that
revision alongside the form's original values. Submit a minimal PATCH:

```
PATCH /scenes/leave_home
{"name":"New name","expected_revision":3}
```

The same field is supported on PATCH /automations/{id}. A successful PATCH
returns the updated full definition and revision 4. Actions, trigger, conditions
and enabled are preserved when omitted. Saving a definition does not execute it.
Every successful PATCH, including an empty/no-op PATCH, increments revision.
Automation enable/disable also increments revision, invalidating older drafts.

If another edit wins, the response is HTTP 409:

```
{"detail":"Definition changed; reload before saving","code":"definition_revision_conflict"}
```

Keep the user's draft and offer an explicit reload/reconciliation. Do not
blindly retry using a newly fetched revision: that would overwrite the competing
edit. After an uncertain network outcome, read the record and reconcile; PATCH
is not an idempotency-key endpoint. A repeated old revision fails, even if its
name equals the current name. Missing/unauthorized resource semantics remain
404/403. Invalid expected_revision (boolean, string, fractional or <1) is 422.

For backward compatibility, omitted/null expected_revision is accepted. Such
clients cannot detect an edit that completed before their request's read; their
writes still increment revision and invalidate conditional editors. In-flight
changes after service validation are rejected even for those clients. Web #12
must adopt expected_revision; its GET/name comparison alone is not atomic.
An old Core ignores unknown JSON fields, so Web must also require a revision in
server responses before claiming conflict protection. Do not silently fall back
to unconditional PATCH when revision is missing.

## Atomicity and permissions

DefinitionRepository executes a single SQL UPDATE with id, revision, and the
source house checked during request authorization in its WHERE clause. It
increments revision in that same statement and returns the row. A zero-row
result rolls back and becomes the typed 409. Concurrent updates to the same
revision cannot both match on PostgreSQL READ COMMITTED: the losing UPDATE
waits/rechecks the committed row. The service's early stale-version check is an
optimization, not the atomicity boundary. A validated candidate's revision is
always carried into that SQL statement. No application mutex or GET-then-check
substitute is used.

Existing source and destination manage permissions are still required. The SQL
house predicate additionally closes a move-between-authorization-and-write gap.
No role, JWT, MQTT, physical command guard, control qualification or execution
behavior is changed. The enable/disable endpoint request shape is unchanged;
this feature adds conditional PATCH, not conditional execution/deletion.

Counters track one record lifetime. Deleting/recreating the same client-chosen
ID or downgrading/restoring the database invalidates stored drafts; counters
are not globally unique generation tokens. Out-of-band SQL writes and old Core
processes do not participate in this protocol. Upgrade Core writers together;
do not advertise protection during a mixed-version rollout. No DB trigger or
claim of protection against external writers is included.

## Verification

- HTTP/SQLite: conditional name edits, preserved definition fields, stale/no-op
  conflicts, legacy edits, enable/disable invalidation and strict input types.
- Existing RBAC suite remains required. Definition edits emit no device commands.
- Real PostgreSQL: two independent sessions racing one revision; conditional vs
  legacy races; old drafts after legacy writes; source-house and enable guards.
- Real HTTP + PostgreSQL: authenticated request-owned sessions synchronize after
  both candidates validate; exactly one response is 200 and one 409, and the
  winner persists with all unrelated definition fields preserved.
- Migration tests on SQLite and PostgreSQL: upgrade from 0008 preserves rows,
  initializes counters, matches ORM metadata, and supports downgrade/re-upgrade.

CI runs PostgreSQL tests only against its disposable service, each test in a
unique schema. TEST_POSTGRESQL_URL is explicit; no fallback to DATABASE_URL or
.env. The offline suite skips unavailable PostgreSQL tests. These tests validate
definition persistence, not broker delivery, firmware or hardware actuation.
