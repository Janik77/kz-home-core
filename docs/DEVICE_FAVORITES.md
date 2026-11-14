# Personal device favorites: Web contract

Base: Core #42 `82653a849d2b61479de1c3bb4d74e7f388523667`.
This feature is preference storage, not device control. No command, MQTT publication,
state mutation, lifecycle/ACK or physical gate changes occur when changing a favorite.
There is no global `favorite` Boolean on DeviceRead or DeviceORM.

## Existing operations (do not duplicate in Web)

| User action | Existing API / permission |
| --- | --- |
| Create room | POST /rooms, floor_id/name; house.manage |
| Rename/move room | PATCH /rooms/{id}, name/floor_id; house.manage in source and destination |
| Create device | POST /devices, room_id/name/type/capabilities/state; device.manage; physical identity still requires onboarding |
| Rename/assign room | PATCH /devices/{id}, name/room_id; device.manage in source and destination |
| Read device/current state | GET /devices?house_id=… and GET /devices/{id}; device.read |
| Event history | GET /events?house_id=…&event_type=…&limit=… (1–500); event.read, security.audit.read for security events |
| Scene definitions | POST/GET /scenes, GET/PATCH/DELETE /scenes/{id}; existing scene permissions and expected_revision conflict contract |
| Confirmed software scene runs | Existing /v1/houses/{house}/scene-runs contract; separate explicit actions, not legacy run acceptance |
| Automation definitions | Existing /automations CRUD and enable/disable/run; automation.manage for writes; legacy execution is not ACK-confirmed |

No CRUD/history/run API is added or replaced by favorites.

## HTTP

Bearer authentication required for every request. Current user comes ONLY from verified
authentication. Each route requires `device.read` in the path house. There is no user_id
parameter, global toggle or write to DeviceRead. Favorites are usable by residents and
other existing roles with device.read; they do not require device.control/manage.

* `GET /v1/houses/{house_id}/me/device-favorites`
  -> 200 `{"device_ids":["relay_1","relay_2"]}` in ascending ID order. Empty -> `[]`.
* `PUT /v1/houses/{house_id}/me/device-favorites/{device_id}`
  -> 204, empty response. No body (optional `{}` also accepted). Sets favorite true;
  repeated/concurrent PUTs remain one row and return 204 when the target remains visible.
* `DELETE /v1/houses/{house_id}/me/device-favorites/{device_id}`
  -> 204, empty response, no body/optional `{}`. Sets favorite false only for this user
  and house. Also 204 if already absent, deleted or moved: no device-existence oracle.
  Does not delete another user's record or the same user's record in another house.

All successful responses use `Cache-Control: no-store`. Identity fields in PUT/DELETE
JSON are rejected (422); query user_id cannot select a different principal.
Missing/invalid token or inactive user -> 401. No house membership -> 404, matching
existing Core non-enumeration policy. Insufficient read permission -> 403. PUT of an
absent/foreign/moved device -> 404. A concurrent FK target/membership deletion can return
409; reload the authorized context rather than blindly retrying. PUT/DELETE is a set
operation, never a read-modify-write toggle. Racing opposite operations have database
serialization order, not a promise that the last browser response wins; reload after
settlement and do not reorder optimistic responses across user/house contexts.

## Scope, movement and deletion

The unique key is `(authenticated user, original house, device)`. Reads AND insertion
select devices through current room/floor and the authoritative physical binding used
by DeviceRepository. Physical binding wins if structural data is corrupt; favorites
never change or bypass that binding. Active membership/read role and active user are
also checked in the SQL selection, not merely a previous HTTP GET/check.

A cross-house device/room/floor move immediately hides the original-house favorite and
does not transfer it to the destination. The original preference is retained but hidden;
it can reappear if the same device returns to that original house. DELETE in the old
house can clear it even while the device is elsewhere. Movement within one house retains
it. Device deletion cascades favorites; ID reuse after deletion does not inherit them.
Membership removal cascades that user's favorites for that house, including hidden ones;
rejoining starts empty. User/house deletion retains existing Core restriction semantics
(e.g. memberships must be removed first); this feature does not loosen those protections.
PostgreSQL FKs protect bulk/concurrent deletes; ORM cascades also cover SQLite development
API deletion. Raw SQLite SQL requires foreign_keys=ON for database-level cascade semantics.

## Web integration and auth transitions

Fetch after login and house selection, then map device.favorite from membership in the
returned ID set. Keep the set/cache keyed by BOTH authenticated account and house. On
logout, account change or house change, clear displayed favorites immediately, cancel
old requests and discard responses belonging to the old context. Do not hydrate the next
account from a previous account's localStorage or optimistic cache. Re-fetch after errors,
changes in membership, device refresh/movement and completed concurrent writes.

Logging out does not delete preferences; logging back in restores them from Core. Current
Core logout revokes the refresh session, not already issued access JWTs: they retain their
existing validity until expiry, subject to active-user and membership checks. Web must
remove its bearer token on logout. This feature neither changes nor claims stronger auth
revocation. Missing favorites API on an older Core is unsupported/unavailable, not evidence
that the user saved an empty list. This branch does not edit Web or implement its mapping.

## Migration and persistent profiles

Additive `0011_device_favorites` follows `0010_scene_runs`: one table, a composite primary
key, a membership FK and device FK with ON DELETE CASCADE, and a device lookup index. No
existing device, house, room, membership, scene, run or command data is rewritten.
Downgrade refuses a nonempty favorites table to prevent silent preference loss. An empty
favorites downgrade drops only the new table/index and preserves earlier data.

New disposable/fresh persistent test profiles use this migration and an exact new runtime
pin, while all existing path/inode/database/volume/source binding checks remain enabled.
Ordinary restore still refuses cross-version inputs. This integration adds one explicit,
isolated upgrade from a trusted #42 backup into a NEW profile, with actual source revision
checks and old-table preservation checks. See [SOFTWARE_UPGRADE.md](SOFTWARE_UPGRADE.md)
for exact pins, fresh setup, supported upgrade and rollback. Identity-less old drafts and
all other source versions remain unsupported; never edit identity fields to force access.
CI verifies this on disposable services, not on any user's WSL installation.

## Checks and boundaries

SQLite HTTP tests cover multiple users/houses, body/query spoofing, account switch/logout,
restart, move via device/room/floor, removal/rejoin, cascades and authoritative physical
read scope using synthetic inventory. No physical device is contacted. Real PostgreSQL
CI adds concurrent HTTP PUT/DELETE, fresh-app reads, direct SQL FK cascades and membership
revocation blocked by an uncommitted favorite insert followed by proper cascade cleanup.
Migration tests verify preservation, ORM parity, empty roundtrip and nonempty rollback
refusal. Persistent-profile acceptance adds favorites to real HTTP restart/backup/restore
snapshots without altering profile identities. Existing Core, MQTT/TLS, Web and scene
checks remain enabled. Exact final SHA/results are recorded in the draft PR.
