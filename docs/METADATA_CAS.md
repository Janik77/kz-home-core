# Metadata edits (schema 0017 candidate)

Existing PATCH routes remain `/houses/{id}`, `/floors/{id}`, `/rooms/{id}` and
`/devices/{id}`. Their GET/list/create responses expose `metadata_revision`.
No new CRUD endpoints and no MQTT commands are introduced.

A metadata edit must carry `expected_metadata_revision`, a strict integer from
1 through 2147483646, obtained from the same resource read. Name, parent, room
icon, floor order and device configuration changes require it. Missing/null,
bool, fractional/string/overflow values return 422. A stale token returns 409
with `code: metadata_revision_conflict`. Every successful CAS, including a
no-op, advances exactly once. Reload and explicitly reconcile a draft after a
conflict; never automatically retry a write using a fresh token.

This deliberately rejects old metadata writers with 422, rather than claiming
lost-update protection while permitting them to overwrite a newer draft.
Deploy the matching Web and Core as a compatible version. Older Core without
`metadata_revision` must be read-only for these editing controls.

House timezone-only PATCH retains `expected_timezone_revision` and does not
advance metadata revision. A combined name/timezone save requires both tokens;
both updates are one SQL transaction. A failed token changes neither field.
Device reports, heartbeats and last_seen do not advance metadata revision.

Source and destination ancestry use the existing ordered house lock domain.
Ancestry, active account and memberships are rechecked after waiting, with
membership/account locks retained through CAS. Nested creates share the same
parent checks. Existing physical binding transfer/deletion restrictions remain.
The Web restricts this iteration to moves inside the selected house; server-side
cross-house requests still require both existing manage permissions and binding
checks. Accepted metadata operations never provision hardware or dispatch SET.

0017 adds non-null counters at 1 without rewriting existing columns. Downgrade
refuses once any counter differs from 1; use a pre-upgrade backup instead of
silently discarding revisions. Supported-profile migration qualification is a
separate integration gate; this document does not authorize editing profile
pins or upgrading the owner's active installation.
