# Explicit durable waits (implementation candidate)

Only `{ "type": "durable_wait", "seconds": N }` activates this runtime. Legacy `delay` remains unsupported by confirmed execution and is not migrated or silently enabled.

A plan has 3–16 total actions, starts/ends with Boolean relay actions, has no adjacent waits, each wait is a strict integer 1–3600 seconds, and total wait time is at most 3600 seconds. Scenes and automations use the same SceneExecutor and the existing lifecycle ticker. Manual/client-dispatched scene execution refuses waits.

## Evidence and bounded recovery

A wait is parked only after its predecessor has both applied ACK and matching current state. Its due time is the persisted command completion time plus the explicit interval. The ownership token is durably released before parking. Only that parked checkpoint with a never-claimed, never-sent successor may be resumed, including after Core process restart. The resume grace is 30 seconds, strictly exclusive at the boundary. Claim commits before scheduling the executor: a crash after claim intentionally loses the continuation, never replays it. Already claimed/sent/unknown commands never advance automatically.

`dispatch_deadline` remains immutable and is also exposed as `plan_deadline` for these plans: creation time + total waits + 30 seconds per relay action (bounded below 68 minutes). Each individual command still has at most 30 seconds and its original expiry is never extended. Initial dispatch is bounded to creation + 30 seconds, or the scheduled occurrence + 30 seconds, whichever is earlier. A resumed successor is bounded to wait due time + 30 seconds. The existing live database deadline and monotonic/wall clock fail-closed guards apply at command commit and before publish. Backward checkpoint time cancels continuation as expired; downtime beyond grace expires it. Restart cannot prove a trustworthy external clock; physical execution remains outside this software acceptance.

Disable, definition revision changes, revoked authorization, moved devices, and (for scheduled automation only) house timezone revision changes invalidate remaining steps. Waiting status is not a promise of later success: offline/stale/pending controls and changed conditions can refuse continuation. Accepted in-flight commands cannot be recalled or undone.

## Wire and cancellation

Relay action snapshots and legacy JSON remain unchanged. Wait snapshots have `type:durable_wait`, `seconds`, null device_id/requested_on/receipt, and due_at/resume_deadline null until parked. Completed waits retain timestamps. Status is ready/waiting/completed/cancelled/expired. Run status adds waiting/cancelled; optional plan_deadline, wait_due_at, resume_deadline and cancelled_at are omitted when absent. Refused automation preflight has no wait snapshot and no plan_deadline.

POST `/v1/houses/{house}/scene-runs/{run}/cancel` requires the creator and scene.run/device.control. POST `/automations/{automation}/runs/{run}/cancel` requires automation.manage/device.control. Only a parked waiting run can be cancelled; response 200 is the current run DTO, repeated cancellation is idempotent. Claimed/active/sent or otherwise terminal runs return 409 and must be re-read. Cancellation does not undo confirmed earlier steps.

## Storage and tests

Migration 0016 is additive: independent wait checkpoint rows leave historical relay snapshots and receipts untouched. Downgrade refuses any explicit wait definition or wait history, even disabled drafts. Restore a verified prior backup instead. Existing profile tooling still targets the previous qualified schedule runtime; a separately reviewed supported-transfer integration must qualify this schema before any profile upgrade.

Local tests use migrated SQLite and fake MQTT; PostgreSQL locking and real TLS software relay checks run only in disposable CI. No physical-device validation is claimed. Separate process tests distinguish parked/claim/publish crash boundaries, and cancellation/revision admission races. The CI wait fixture verifies A → wait → restart → B ACK/state, waiting cancel, and real overdue restart without replay.
