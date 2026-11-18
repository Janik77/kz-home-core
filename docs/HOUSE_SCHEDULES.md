# Explicit house-zone schedules (software candidate)

This stage extends AutomationRunService and SceneExecutor, not the legacy executor.
No physical-device claim, automatic deployment, or installed profile mutation.

## Wire contract

House GET returns `timezone: string | null` (IANA identifier) and
`timezone_revision: integer`. Existing houses are NULL, never inferred from server,
browser, UTC offset or location. POST house may explicitly choose timezone.
PATCH `/houses/{id}` with `timezone` (including NULL) requires
`expected_timezone_revision`. Unknown zones/missing CAS return 422; stale CAS 409
`definition_revision_conflict`. Existing rename-only PATCH remains compatible.
Requires existing `house.manage`; read requires `house.read`.

Trigger: `{"type":"schedule","time":"21:30","weekdays":[0,1,2,3,4]}`.
Time is strict 24-hour HH:MM. Days are Monday=0 through Sunday=6, unique nonempty
integers (not booleans); response sorted. Unknown fields are rejected. No per-rule
zone, catch-up setting, cron strings, interval or durable delay in this stage.
Enabled schedule create/edit/enable needs house timezone, otherwise 422
`House timezone must be explicitly configured`. Disabled drafts can be saved
without timezone. Definitions retain existing revision CAS and permissions.

Dry-run returns 200 and readiness issues even for disabled drafts; missing timezone
is `house_timezone_required`. It tests availability/conditions, NOT whether the
scheduled minute is now; it never sends a command. Valid actions remain Boolean
software relay commands, supported conditions Boolean/numeric AND comparisons.
Legacy time conditions/delays are NOT silently converted to active schedules.

## Time, edits and downtime

One lifecycle ticker wakes approximately once per second. Only a continuously
healthy tick crossing a minute boundary (gap <=5 seconds) can claim that minute.
A per-house persisted high-water cursor and row lock serialize concurrent workers.
A newly starting worker may conservatively advance the shared cursor before another
healthy worker claims the boundary: availability is deliberately secondary to no replay.
Process startup always establishes a baseline and skips the current minute; no
startup replay even when restarted within that minute. A missed/stalled tick skips,
not queues, work. No historical days are enumerated or backfilled.

Every successful rule edit/enable/disable (including no-op PATCH) and timezone save
becomes effective strictly after its current minute. An old rule revision or zone
revision cannot dispatch. A house timezone edit invalidates already admitted but
unsent schedule actions through the same pre-command and pre-publish guards used
by existing durable commands. Existing command clock wall/monotonic drift detection,
DB `clock_timestamp()` deadlines and leases remain unchanged.

DST gap: nonexistent local minute is skipped. DST fold: only first occurrence
(fold=0) is eligible; the second is skipped even if the first was missed/offline.
Weekdays and HH:MM are interpreted solely in the saved house zone.

## Durable outcome and limits

Occurrence consumption is committed before rights/condition/transport admission.
Existing history records refusals (`conditions_not_met`, `authorization_revoked`,
`state_unavailable`, `command_pending`, `transport_unavailable`, `rate_limited`,
`admission_refused`) without retry on later recovery. Disabled rules and missed
planning windows are skipped without infinite refusal history. There is no catchup.
A crash between cursor/occurrence consumption and admission can lose one occurrence;
this deliberately favors no unexpected actuation over at-least-once execution.

Runs reuse existing status/actions/receipt wire. Success requires ACK AND matching
state. Missing/uncertain receipt remains unknown, never guessed successful.
Dispatch is bounded to 30 seconds after the occurrence; an unsent expired action is
blocked. No startup resumption or automation chaining was added.

Migration `0015_house_schedules` is additive and preserves numeric freshness/latches.
Downgrade refuses saved schedule data/history; restore a verified original backup.
Existing numeric integration profile pins are intentionally not rewritten by this
feature. A reviewed compatible integration and backup/restore upgrade is separate.

## Verification

`python -m pytest tests/test_house_schedules.py tests/test_schedule_process_restart.py`
`TEST_POSTGRESQL_URL` selects dedicated CI database for `test_schedule_postgresql.py`.
CI-only actual-clock PG/TLS/software-relay acceptance reuses the existing fixture:
`python -B tools/scene_run_live.py --web-root <pinned-clean-web> --schedule-only`.
Do not run this fixture against the owner's local Docker/profile. Browser/video
acceptance is coordinated with the matching Web schedule branch separately.
