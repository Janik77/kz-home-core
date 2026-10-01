# Physical device onboarding foundation (v0.10)

This block implements Core inventory, ownership binding and an admission gate.
It does not issue MQTT credentials, configure a broker, provision Wi-Fi, or change
Device Protocol v1. The v0.8 software E2E remains verified and merged. The separate
ESP32-C6 firmware v0.9 is implemented/merged according to project context; physical
hardware acceptance is deferred until hardware is available.

## Trusted inventory and claim proof

An operator with trusted server/database access registers inventory explicitly;
there is no public inventory-registration or inventory-enumeration API. This uses
the same operational trust boundary as the existing explicit E2E bootstrap, not
a second human authorization system. Ordinary customer/installer JWTs cannot
create trusted hardware identities or capability profiles.

Inventory has a unique operator-verified `hardware_id` (serial/factory identifier),
an immutable globally unique Core `device_id`, an approved `hardware_model`, and
`protocol_version=v1`. IDs are never recycled. The hardware identifier alone is
not possession proof or hardware attestation. Register an actually inspected
device and independently verify its identifier before handing over its claim code.

Prepare a fresh random 32-byte code encoded as 43 URL-safe characters, equivalent
to `secrets.token_urlsafe(32)`, using a trusted secret-generation tool. A memorable
password, serial, MAC address, or deterministic code is unacceptable. The format
check cannot prove entropy; generation and secure handoff are operator duties.
Keep the code in a protected external secret store until secure delivery to the
installer/customer. Do not put it in shell arguments, terminal transcripts,
request/access logs, Git, device metadata, or MQTT. No code is generated, echoed,
or returned by Core. Enter it through the hidden prompt:

```text
python -m app.register_physical_device --device-id device_c6_001 --hardware-id serial_c6_001 --hardware-model esp32-c6-relay-v1
```

Use the configured project interpreter/container and intended database settings.
The command checks schema readiness; it never migrates or connects MQTT. It
refuses getpass's echoed-input fallback. Conflicting inventory registrations fail
without replacing existing IDs or claim-code hashes. Rerunning registration is
an explicit conflict, not secret rotation. Only SHA-256 of the high-entropy code
is stored in the private inventory table, and claim consumes that hash atomically.
The code authorizes one ownership binding; it is not a reusable MQTT password or
a human access/refresh token. Unclaimed inventory has no house or runtime Device
and cannot use normal MQTT topics. Claim codes currently have no expiry/recovery
API; protect them until claim and do not import devices with compromised codes.

The sole approved profile `esp32-c6-relay-v1` creates a `relay` with `on_off`.
This is a reviewed logical v1 contract, not a claim of electrical/firmware hardware
validation. Claimants cannot supply capabilities, observed state, online status,
metadata, protocol version, or a replacement device ID. Extend approved profiles
in server code with explicit review/tests when another model is supported.

## Lifecycle and ownership

```text
unprovisioned -> provisioning -> active <-> inactive -> revoked
                     |            |                    ^
                     +-> inactive +--------------------+
                     +---------------------------------+
```

- `unprovisioned`: trusted inventory, unclaimed, no runtime Device/topic admission.
- `provisioning`: code consumed, permanently bound to one house, offline runtime
  Device created in the selected room with empty observed state. MQTT is denied.
- `active`: Core admits v1 messages and sends validated commands. Activation does
  not mark it online, connect it, or prove broker setup/hardware success.
- `inactive`: reversible Core admission/control suspension; online is forced
  false, observed state/history and permanent binding are retained.
- `revoked`: terminal Core admission/control denial; identity/binding/state/history
  are retained. No reactivation, ownership transfer, deletion or ID reuse API exists.

This is a minimal implementation of Protocol v1's conceptual provisioning phases:
factory/manufactured registration is an external trusted step, and `inactive`
is a reversible local suspension of an already provisioned identity. The firmware
does not receive these states in a new wire envelope. Failure of claim rolls back
to unprovisioned with its code hash intact; after a successful claim, failed
external setup leaves it provisioning/inactive. It never releases ownership or
restores a consumed claim code automatically. Revoked recovery/transfer and
cancel/re-provision workflows require a later explicit trusted design.

## API and existing house RBAC

All endpoints require a valid active human user via the existing JWT dependency.
`device.onboard` is a permission in the existing role matrix: owner and installer
have it; technician and resident do not. Superuser metadata grants no bypass.
Reading bound lifecycle records uses existing `device.read` for house members.

| Method/path | Permission | Result |
|---|---|---|
| `POST /houses/{house_id}/device-claims` | `device.onboard` | 201, new provisioning binding |
| `GET /houses/{house_id}/physical-devices` | `device.read` | Bound records for this house only |
| `GET /houses/{house_id}/physical-devices/{device_id}` | `device.read` | One bound lifecycle record |
| `POST /houses/{house_id}/physical-devices/{device_id}/activate` | `device.onboard` | 200, Core admission enabled |
| `POST /houses/{house_id}/physical-devices/{device_id}/deactivate` | `device.onboard` | 200, Core admission suspended |
| `POST /houses/{house_id}/physical-devices/{device_id}/revoke` | `device.onboard` | 200, terminal Core denial |

Claim request (the code placeholder must be replaced in memory, never logged):

```json
{
  "hardware_id": "serial_c6_001",
  "claim_code": "<43-character random one-time code>",
  "room_id": "room_hall",
  "name": "Hall relay"
}
```

Claim/read/lifecycle response shape:

```json
{
  "device_id": "device_c6_001",
  "house_id": "house_001",
  "hardware_model": "esp32-c6-relay-v1",
  "protocol_version": "v1",
  "status": "provisioning",
  "created_at": "2026-10-01T00:00:00Z",
  "updated_at": "2026-10-01T00:00:00Z"
}
```

Activation requires `{"broker_access_confirmed": true}`. This is the authorized
operator/installer's explicit attestation of external per-device credential,
TLS/CA trust and exact directional ACL preparation, not a Core verification of
Mosquitto. Deactivate/revoke have no required body. Repeating a transition to its
current state returns 200 without changing timestamps/online or duplicating audit.
A repeated claim returns 409 without resetting state, creating another Device,
changing ownership or accepting a consumed code. Revoked -> active/inactive is 409.

No/invalid authentication is 401. A house outside the user's memberships is 404;
a member lacking permission receives 403. Wrong house, unknown hardware/device,
wrong code and a room outside the requested house are hidden as 404. An already
bound foreign hardware identity returns 404 even to a user who owns both houses.
An already bound identity in the requested house returns 409. Malformed/extra
onboarding fields return a fixed 422 response without echoing input/claim codes.

Ordinary device APIs preserve their existing shapes. Physical profile/state/
online/metadata fields cannot be overwritten by management PATCH; physical records
allow only name and same-house room edits. Command endpoints still require
`device.control`, active lifecycle and a configured transport. Commands do not
overwrite observed state. Inactive/provisioning/revoked messages are rejected at
the common MQTT identity gate for state, status, ACK and telemetry, and again in
DeviceService for state/status. Legacy, virtual and v0.8 E2E records remain on
their existing paths; they are not implicitly adopted into physical inventory.

Physical ownership is authoritative for ordinary GET/list authorization. Moving
a Device, its room, or its floor into another house cannot transfer that binding,
including when a caller owns both houses. Deleting a physical Device or its parent
room/floor/house is rejected, including after revocation. Database RESTRICT FKs
add protection against deleting the bound Device/house in PostgreSQL (and SQLite
when foreign keys are enabled). Application guards protect the existing SQLite
test/development configuration too. Direct privileged SQL is a trusted operator
boundary; it must not mutate these bindings. Inconsistent location data fails
closed for physical control/MQTT, while reads retain the original house scope.

## Persistence and transactions

Explicit revision `0005_device_onboarding` follows `0004_event_log_correlation`.
It adds only `physical_devices`, unique hardware/device bindings, house index,
RESTRICT references and lifecycle/protocol/binding check constraints. Existing
devices are not backfilled or assigned invented hardware identities. The sole
head is 0005; run `alembic upgrade head` explicitly before deploying this Core.
No migration or registration runs at startup. `/ready` requires the new head.

Claims commit the runtime Device, binding, consumed code hash, and a fixed safe
`device_onboarding_changed` EventLog record in one transaction. State transitions
commit lifecycle, offline availability and the audit record together. All failures
roll back. Audit payload is an allowlist of device ID, human actor ID and status;
no code, hash, credential or arbitrary request metadata is written to it.

PostgreSQL row locks on houses serialize claim against structural mutation;
inventory row locks serialize competing claims/transitions. Conditional updates
and device identity uniqueness prevent a second claim from succeeding. SQLite
does not implement PostgreSQL row locks and is for local/offline tests, not
concurrent production commissioning. It may return a database lock error under
concurrent writers; retry explicitly. A transition cannot undo already dispatched
physical work, and messages admitted before suspension may finish processing.
Admission checks for commands/ACK/telemetry do not hold row locks across awaited
network/event delivery. State/status persistence locks the lifecycle row and
commits synchronously before event publication, preventing late reports from
restoring online availability after a completed suspension.

Downgrade requires an online check and refuses while any inventory rows exist,
including unclaimed or revoked rows. With empty inventory it drops the new table
and leaves legacy data intact. Do not erase identities to bypass this guard.

## MQTT credentials: deliberate external boundary

The checked-in Mosquitto deployment uses TLS server authentication plus
`password_file` and directional `acl_file`. Inputs are mounted read-only and
copied into private broker tmpfs on startup. Core has only its own MQTT account
and public CA trust; it has no broker admin API, password-file write mount,
certificate authority, credential vault or dynamic-security plugin integration.
Publisher authentication identity is not delivered to Core with MQTT messages.

Therefore automatic reusable-password issuance/rotation/revocation is **not
implemented**. Core does not persist or return MQTT passwords, Wi-Fi passwords,
human tokens, CA/private keys or credential authority material for devices.
Neither lifecycle APIs nor ordinary GET/list expose claim codes/hashes or MQTT
credentials. There is no fake credential endpoint or claim that setting a DB
status revokes a broker account.

The operator must separately create a unique broker principal, bind it explicitly
to the returned stable house/device IDs using exact directional v1 ACLs, deliver
the broker address, public CA, IDs and credentials securely to the firmware, and
confirm preparation before activation. Broker usernames/client IDs are external
routing/authentication configuration; `device_id` remains Core identity, and
human JWTs must never become device credentials. Never reuse Core's MQTT account,
a user's password or a fleet-wide device password.

Required v1 ACL directions for the approved relay are read only its own `set`;
write only its own `state`, `ack`, `telemetry`, `status`. No wildcards or cross-house
access. Rotation keeps the same Core device ID. Deactivation/revocation immediately
deny new Core commands and ingress; the operator must also remove/disable broker
credentials/ACLs, terminate live sessions, and account for retained/queued traffic
to complete transport revocation. The existing file-staging model requires an
explicit protected broker configuration update/recreation. Core performs none of
these operations. No files under `deploy/local` are read or changed by this block.

## Remaining release work

Before real device commissioning: secure code generation/physical identity
verification and handoff; a reviewed external broker account/ACL/rotation/revoke
procedure (or a separately designed least-privilege authority); HTTPS and request
body/access-log redaction plus claim/login rate limiting; and an approved device
network path with verified TLS hostname/public CA. Current Compose deliberately
does not expose MQTT to an ESP32 on the LAN.

The separate firmware needs secure installation of IDs, broker identity, CA and
network configuration. BLE/SoftAP/QR/mobile UX, automatic discovery, CA issuance,
OTA and flashing are deferred. Protocol freshness/heartbeat, command outcomes,
durable retries/deduplication and physical automation loop safety remain existing
release gaps. Hardware acceptance waits for hardware and does not block these
server-side foundations. Optional dedicated PostgreSQL tests and live broker
negative-ACL/revocation verification must be run before production commissioning.

## Validation of this block

The pre-change suite passed 156 tests with 2 optional PostgreSQL skips. The focused
onboarding/schema/auth/RBAC/MQTT/E2E/bootstrap suite passed 143 tests with 4 skips;
the full suite passed 233 tests with 4 skips. All four require explicit
`TEST_POSTGRESQL_URL`: two existing migration/length cases and two new same-house/
cross-house concurrent-claim cases. The concurrency checks create a uniquely named
schema in a dedicated test database, commit isolated setup for independent worker
transactions, and remove only that schema in cleanup. They never use `.env` or
`DATABASE_URL`. They were not run against PostgreSQL in this session.

Ruff, compileall and `git diff --check` passed; Alembic reports only
`0005_device_onboarding`. IDE inspections found no errors in changed Python files;
IDE build reports success with limited diagnostics. The existing Starlette/AnyIO
deprecation warning and unrelated IDE type/configuration warnings remain. Tests
use temporary databases and in-memory MQTT, and exercise denial/IDOR, duplicate
claims, terminal revocation, safe validation errors/response isolation, rollback,
parent CRUD bypasses, approved profiles, active physical v1 traffic, migration
alignment/downgrade protection, and legacy/E2E regressions. No live installation
migration, broker or hardware acceptance was performed.
