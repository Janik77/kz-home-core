# Numeric sensors and threshold automations (software only)

Base Core #50 c03aa044. Additive migration `0014_numeric_thresholds`.
No physical sensor, GPIO or electrical acceptance is claimed. Do not point this
branch at an existing persistent profile using old compatibility pins. Preserve
and back up the supported old profile; integration tooling must explicitly
support this schema before using its upgrade path.

## Contract

Existing Device `capabilities` advertise `temperature`, `humidity`, `illuminance`.
Read-only numeric state uses the same field names. Units/ranges: Celsius -100..150,
relative humidity percent 0..100, illuminance lux 0..200000. Only JSON numbers,
not booleans/strings/null/NaN/infinity. Added `illuminance_sensor` DeviceType.
No numeric command support is introduced.

DeviceRead `numeric_observed_at` maps each field to an ISO UTC sender timestamp
or null. Missing/null is unknown, not zero. `updated_at` is NOT measurement age.
Fresh requires online + valid field + timestamp within 120 seconds and no more
than 5 seconds future skew. Another field/status heartbeat never refreshes it.
Invalid packets are rejected. Retained/old packets invalidate field freshness
without overwriting latest accepted numeric readings; duplicate/out-of-order
valid packets do not update readings or freshness. Watermarks survive restart.
An offline status clears field freshness. HTTP inventory edits invalidate it.

POST/PATCH `/automations` uses existing revision, permissions and history APIs:

```json
{"trigger":{"type":"numeric_threshold","device_id":"e2e_motion","field":"illuminance","operator":"lt","value":100,"hysteresis":20},"conditions":[{"type":"numeric_state","device_id":"e2e_motion","field":"temperature","operator":"gte","value":18}],"actions":[{"type":"device_state","device_id":"e2e_relay","state":{"on":true}}]}
```

Include normal id/name/house_id/enabled on create; `expected_revision` on edit.
`numeric_threshold` allows strict lt/gt only; positive finite hysteresis and its
reset point must fit field bounds. `numeric_state` AND conditions allow
 eq/neq/gt/gte/lt/lte. Only existing 1..16 Boolean software-relay actions execute.
Old numeric `device_state` rules remain unsupported, never silently converted.

## Initialization, hysteresis and interrupted work

Each new revision starts unarmed. First fresh sample initializes WITHOUT firing:
lt arms at value >= threshold+hysteresis; gt arms at value <= threshold-hysteresis.
An armed rule fires once when strictly below/above the threshold, respectively.
The edge is consumed even if conditions/preflight refuse it; there is no deferred
execution once conditions improve. A complete hysteresis recovery is needed.
Samples within the dead band retain the latch. Equality at trigger does not fire;
equality at reset re-arms. Disable excludes new work; edit/re-enable starts a new
revision and initialization. A stale/retained/offline gap or >120-second gap
reinitializes rather than inferring an unobserved crossing. Equal-value fresh
samples still initialize/re-arm; no invented changes are needed.

House/definition locks serialize edge consumption. The persisted latch is
committed BEFORE command admission (at-most-once, not guaranteed delivery).
A crash in that narrow gap can lose an edge, never replay it. Restart does not
start work from saved states. Existing durable command/run receipts, ACK+state
confirmation, 30-second cooldown, no chaining and no unknown-command replay remain.
Events superseded by newer accepted values are ignored instead of acting on old
conditions. Rapid sensor bursts may therefore coalesce; this is fail-closed.
Downgrade refuses nonempty threshold checkpoints; use a verified backup.

## Software probe

Reuses existing motion-simulator TLS identity and its e2e_house/e2e_motion ACL;
no secret/permission provisioning and no commands. On an isolated test inventory,
explicitly advertise the numeric capabilities on this software sensor. Existing
physical device gates and legacy motion rule are unchanged.

```sh
# Existing MOTION_MQTT_HOST/PORT/USERNAME/PASSWORD/CLIENT_ID and MOTION_CA_FILE
python -m simulator.mqtt_numeric --run --house e2e_house --device e2e_motion --field illuminance --values 120 100 99 105 99 --interval 1
# Add --duplicate or --stale to test repeated/old packets.
```

Probe publishes online, bounded samples then offline; success text means published,
NOT proof of an automation. Inspect `/automations/{id}/runs` for applied command
receipts. Sensor CLI has no API token and creates/edits no rules or inventory.

Tests: tests/test_numeric_thresholds.py (gateway, migrated SQLite),
tests/test_numeric_postgresql.py (opt-in isolated PostgreSQL duplicate race).
Existing command lifecycle and durable automation regressions remain mandatory.
