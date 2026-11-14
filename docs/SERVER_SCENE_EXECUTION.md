# One-click software scene execution

New explicit `POST /v1/houses/{house_id}/scene-runs/execute`, JSON
`{"scene_id":"...","expected_revision":1}`, `Idempotency-Key` (16–128 ASCII
letters/digits/underscore/hyphen), returns 201 and the durable run. The old
`POST /scene-runs` is deliberately unchanged and creates a manual run.
Manual dispatch endpoints reject server runs, never bypassing the executor.

`GET /scene-runs/{run_id}` reads results. `GET /scene-runs/by-key/{key}` is
caller-scoped, read-only recovery after a lost create response. 404 means no
matching committed run is currently visible, not proof the original request
cannot arrive later. A repeated same-key POST returns the same intent and never
schedules another worker. Reusing a key with a different scene/revision/mode is
409. A live server run for the same house/scene blocks a different key (house
row lock serializes PostgreSQL admission).

The previous mechanism intentionally required each action to be dispatched
by the client: its ledger protected at-most-once attempts but it had no server
executor. Merely removing UI confirmations would have bypassed that ownership
boundary. Server execution now owns an immutable action snapshot and a random
executor token. It sequentially advances only when both correlated ACK and state
prove `applied`. Creation checks the entire snapshot, revision, house, Boolean
relay capability, online state, no pending command, physical exclusion, MQTT
availability/software allowlist, and caller permissions before publication.
Revision is rechecked under a lock after asynchronous preflight. Authorization,
location, device contract, command deadline and executor lease are rechecked at
actual dispatch. A permission change cannot authorize a subsequent action; a
command already published cannot be recalled.

Each claim is committed before network-capable code and each command/receipt link
is committed before publish. Server ownership has a 30-second renewable lease,
within the existing five-minute total dispatch deadline. Renewal cannot revive
an expired lease. No startup scan resumes runs. Process cancellation marks an
interrupted outcome; abrupt process loss becomes unknown after lease expiry.
Unknown attempts are never blindly replayed. This is not a distributed transaction,
rollback, or exactly-once physical delivery guarantee.

`execution_mode` is `manual|server`; `execution_error` provides a bounded reason.
For server runs, status is `running`, `succeeded`, `partial`, `refused`, `unknown`,
or `expired`. Explicit rejection yields partial if earlier actions applied,
otherwise refused. Publish-unconfirmed, receipt timeout, or executor loss is
unknown even when earlier actions applied. Action statuses/receipts preserve the
individual evidence; a timeout is never reported as confirmed device failure.
Unsent later actions become skipped after a stop. Reads can reconcile late
receipts but never initiate a command.

Migration `0012_server_scene_execution` adds ownership metadata. Existing runs
remain manual. Downgrade refuses to discard server history. Use a **new isolated
software test profile**; do not run this against the owner's old WSL profile or
change its directory binding. The old profile recovery task is postponed.

Tests use software relay only. No physical ESP32/relais/mains control is supported
or tested by this change. Use the pinned Web checkout in unified-software.yml and
its existing isolated launcher; production deploy and merge are not authorized.
