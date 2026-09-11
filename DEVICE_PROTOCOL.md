# KZ Home Device Protocol v1

## 1. Purpose and scope

This document freezes the logical contract between KZ Home Core and physical
devices for protocol version **v1**. The contract is transport-independent:
MQTT is the first proposed adapter, while BLE Mesh, Zigbee, Matter, and future
transports must map to the same normalized device concepts rather than leaking
transport-specific addresses or messages into Core.

This is an implementation contract, not a runtime implementation. The keywords
**MUST**, **MUST NOT**, **SHOULD**, and **MAY** express requirements.

The normalized model consists of:

- **Device**: stable identity, ownership, model, firmware, and lifecycle.
- **Capability**: the declared, typed fields a device can report or accept.
- **State**: desired or current values for capability-backed fields.
- **Command**: an idempotently identified request to change desired state.
- **Event**: an observed state, acknowledgement, availability, or telemetry
  occurrence with correlation metadata.

## 2. Versioning and compatibility

Every device identifies its `protocol_version`; this document defines the exact
string `v1`. The proposed MQTT namespace is:

```text
kzhome/v1/{house_id}/{device_id}/...
```

Additive changes are backward-compatible when old participants can safely
ignore them. Examples include a new optional envelope field, a new optional
telemetry metric, or a newly declared device capability. Implementations MUST
ignore unknown optional envelope metadata unless this document says otherwise,
but MUST reject unknown state fields because physical intent must be explicit.
Removing or renaming a required field, changing field meaning or type, changing
topic semantics, or changing command/ACK guarantees is breaking. A breaking
change requires a new major protocol and namespace, for example `v2` and
`kzhome/v2/...`. Devices and adapters MUST NOT infer compatibility merely from
firmware versions.

Core MAY operate v1 and future versions concurrently through separate adapters.
Version translation, when safe and explicit, belongs in the adapter; the service
and automation layers consume only normalized objects.

## 3. Device identity

A normalized Device has at least:

| Field | Type | Requirement |
|---|---|---|
| `device_id` | string | Stable, opaque, and globally unique within KZ Home. It MUST NOT change after provisioning or be recycled. |
| `house_id` | string | Stable identifier of the owning security/automation boundary. |
| `hardware_model` | string | Manufacturer/model identifier used to select an approved capability profile. |
| `firmware_version` | string | Device-reported firmware build/version. It is informational, not protocol negotiation. |
| `protocol_version` | string | `v1` for this protocol. |

Identifiers are application identifiers, not display names. Each transport may
also have an address (MQTT topic, Zigbee IEEE address, BLE address, or Matter
node ID), but that address is adapter-owned routing metadata. In particular,
MQTT topic strings MUST NOT be used as database identity. Core authorizes the
`house_id`/`device_id` association server-side; a payload cannot reassign it.

## 4. Declarative capability model

Capabilities are the allow-list and schema for state. A capability map is keyed
by a stable field name. Each entry MUST contain `type` (`boolean`, `integer`,
`number`, `string`, or a deliberately defined compound type) and `writable`.
Numeric capabilities SHOULD define inclusive `min`/`max`; constrained strings
SHOULD define an `enum`. Units belong in optional `unit` metadata. Capability
metadata is declarative and MUST NOT contain executable code.

Relay:

```json
{
  "on": {
    "type": "boolean",
    "writable": true
  }
}
```

Dimmable light:

```json
{
  "on": {
    "type": "boolean",
    "writable": true
  },
  "brightness": {
    "type": "integer",
    "min": 0,
    "max": 100,
    "writable": true
  }
}
```

Cover (v1 defines `0` as fully closed and `100` as fully open):

```json
{
  "position": {
    "type": "integer",
    "min": 0,
    "max": 100,
    "writable": true
  }
}
```

Sensor capabilities MUST be read-only, for example:

```json
{
  "motion": {
    "type": "boolean",
    "writable": false
  }
}
```

Core MUST obtain capabilities from a trusted device/model registry and validate
all state and commands against them. A device MUST NOT acquire authority merely
by claiming extra capabilities. The future AI layer may select only capabilities
already declared for the target Device; it MUST never invent capabilities.

## 5. State semantics and validation

**Current state** is the latest authoritative physical/logical state reported by
the device. **Desired state** is Core's requested target. A command's `state`
contains desired state; a `state` event contains current state. Desired state is
not proof of execution and MUST NOT overwrite current state until a device state
report confirms it. An `accepted` ACK is likewise not confirmation of current
state. An `applied` ACK says execution completed, but Core SHOULD still reconcile
against a subsequent/current state report.

State is a partial JSON object: omitted capability fields mean “no update,” not
`null`, false, or zero. Every included field MUST:

1. exactly match a declared capability name;
2. match its JSON type (a JSON boolean is not an integer; integers have no
   fractional component);
3. satisfy inclusive bounds, enum, length, and other declared constraints; and
4. be writable when it appears in a command. Reports MAY include writable and
   read-only capabilities because both can describe current state.

Unknown state fields MUST be rejected as `unsupported_capability`, not silently
stored. Invalid types, ranges, enums, or nulls MUST be rejected as
`invalid_value`. An empty command state MUST be rejected as `protocol_error`.
For a report containing any invalid field, the adapter MUST reject the whole
report atomically and emit/record a protocol error; it MUST NOT partially update
current state. Unknown optional fields outside the `state` object may be ignored
for forward compatibility. Implementations MUST bound nesting and JSON parsing
as specified in §17.

State reports SHOULD include `timestamp` and `correlation_id` in their envelope.
When a report results from a command, the device MUST copy that command's
`correlation_id`. An unsolicited physical change generates a new correlation ID
at its first trusted ingress if the device did not provide one. Timestamps use
RFC 3339 UTC with milliseconds (for example `2026-09-11T12:00:00.000Z`). Core
records receipt time independently and MUST NOT trust device clocks for access
control or event ordering.

## 6. Commands and idempotency

A Core-to-device command payload contains:

```json
{
  "command_id": "cmd_01J...",
  "correlation_id": "corr_01J...",
  "timestamp": "2026-09-11T12:00:00.000Z",
  "state": {
    "brightness": 65
  }
}
```

- `command_id` uniquely identifies one command attempt and MUST be unique for
  the device. A retry of the same logical command uses the same `command_id`.
- `correlation_id` ties the command to its API request, automation execution, or
  originating event and is preserved end-to-end.
- `timestamp` is when Core created the command, in RFC 3339 UTC.
- `state` is a non-empty partial desired-state object validated as in §5.

Delivery is at-least-once in practice. Devices MUST deduplicate recently seen
`command_id` values in durable storage appropriate to their safety model. A
duplicate MUST NOT repeat an unsafe physical action (for example pulsing a
garage relay); it SHOULD return the previously known ACK outcome. Core retries
with the same ID after missing delivery/ACK and creates a new ID only for a new
intent. Deduplication retention MUST cover the maximum command retry/expiry
window; a concrete adapter configuration MUST define that window before launch.
Absolute desired state is preferred over non-idempotent verbs such as “toggle.”

Devices MUST reject stale commands according to a product-defined expiry policy
rather than execute arbitrarily old retained/queued intent. The MQTT `set`
message is never retained, which limits but does not eliminate staleness.

## 7. MQTT v1 mapping

Topic segments are the authorized canonical IDs; they MUST use a restricted,
documented character set and MUST NOT contain `/`, `+`, `#`, NUL, or traversal
forms. The adapter verifies that topic identity matches authenticated principal
and registered Device. Payloads are UTF-8 JSON objects.

| Topic | Publisher | Subscriber | Payload purpose | Retained | QoS recommendation |
|---|---|---|---|---|---|
| `kzhome/v1/{house_id}/{device_id}/state` | Device | Core MQTT adapter | Authoritative current capability state and event metadata | **Yes**, latest valid state | QoS 1 |
| `kzhome/v1/{house_id}/{device_id}/set` | Core MQTT adapter | Device | Desired-state command envelope | **No** | QoS 1 |
| `kzhome/v1/{house_id}/{device_id}/ack` | Device | Core MQTT adapter | Execution progress/result for a command | **No** | QoS 1 |
| `kzhome/v1/{house_id}/{device_id}/telemetry` | Device | Core telemetry adapter | Non-authoritative diagnostics/measurements | **No** | QoS 0 normally; QoS 1 for explicitly critical metrics |
| `kzhome/v1/{house_id}/{device_id}/status` | Device (including broker-published LWT) | Core MQTT adapter | Availability and last-seen/heartbeat data | **Yes**, latest status | QoS 1 |

QoS 1 provides at-least-once MQTT delivery, not exactly-once device execution;
command idempotency is therefore mandatory. Retained state/status bootstrap a
subscriber but their timestamps still require freshness checks. The broker MUST
be configured so devices can publish only their own `state`, `ack`, `telemetry`,
and `status`, and subscribe only to their own `set` topic. Core access is scoped
by its server role and still checked against house ownership.

The v1 MQTT mapping above differs from the current placeholder MQTTTransport;
see §19. No runtime behavior is changed by this document.

## 8. Acknowledgements

An ACK payload contains:

```json
{
  "command_id": "cmd_01J...",
  "correlation_id": "corr_01J...",
  "timestamp": "2026-09-11T12:00:00.420Z",
  "status": "applied"
}
```

`status` is exactly one of:

- `accepted`: validated and queued/started, but not yet physically complete;
- `applied`: physical execution completed successfully;
- `rejected`: not accepted (validation, authorization, unsupported intent);
- `failed`: accepted but execution did not complete successfully.

`error_code` and `message` are optional. `rejected` and `failed` SHOULD include
`error_code`; `message` is bounded human-readable diagnostics and MUST NOT be
used for program logic. Multiple ACKs MAY progress from `accepted` to a terminal
`applied`, `rejected`, or `failed`; terminal outcomes MUST NOT regress. Each ACK
copies both IDs from the command. Unknown `command_id` ACKs are recorded as
protocol anomalies and MUST NOT mutate state.

An MQTT publish acknowledgement means only that the broker handled delivery at
the selected QoS. It does not mean that a device accepted, applied, or even
received the physical instruction. Only a protocol ACK describes command
processing, and only current state confirms the reported state of the device.

## 9. Availability: online, offline, and heartbeat

Availability is distinct from device state. Before subscribing, a device
configures an MQTT Last Will and Testament (LWT) on its own `status` topic with
QoS 1, retained true, and an `offline` payload. After connection and subscription
readiness, it publishes retained `online`. On graceful shutdown it SHOULD
publish retained `offline` before disconnecting.

An online payload contains `status: "online"`, device-observed `last_seen`, and
`heartbeat_interval_seconds`. While connected, the device republishes `online`
as a heartbeat. The LWT offline payload contains `status: "offline"`; because a
preconfigured LWT timestamp can become stale, Core treats its `last_seen` as a
hint and records broker receipt time. Core derives offline on heartbeat timeout
even if the LWT is delayed or absent. A practical default is a 60-second
heartbeat and offline after 150 seconds (2.5 intervals); negotiated/configured
values MUST stay within §17 limits.

Status transitions are retained so reconnecting Core instances receive the
latest availability claim. Retention does not establish freshness: Core compares
timestamps and receipt time. `online` means transport reachability recently
observed, not correct hardware operation or command success.

## 10. Telemetry

Telemetry is diagnostic/time-series information, separate from authoritative
device state. Typical fields include `rssi`, `uptime`, `temperature`,
`free_heap`, and `firmware_version`. Telemetry envelopes SHOULD include a
timestamp and a `metrics` object. Metrics MUST use documented types and units.

Receiving telemetry MUST NOT automatically declare a capability, update desired
or current state, or make a field writable. A product may deliberately model a
measurement such as room temperature as a read-only capability; that explicit
capability state remains independently validated even if a similar telemetry
metric exists. Telemetry is not retained by MQTT and may be sampled/dropped
under load.

## 11. Error model

Machine decisions use stable `snake_case` error codes, not messages. v1 defines:

| Code | Meaning |
|---|---|
| `unsupported_capability` | State names a capability not declared for the Device, or attempts to command read-only data. |
| `invalid_value` | Value has the wrong type or violates declared constraints. |
| `device_busy` | Device cannot accept the command at present; retry may be possible. |
| `hardware_failure` | Physical execution failed. |
| `unauthorized` | Authenticated principal is not allowed to perform the operation. |
| `timeout` | Processing or physical execution exceeded its deadline. |
| `protocol_error` | Malformed envelope, missing field, unsupported version, invalid transition, or other contract violation. |

Codes are stable within v1. New codes MAY be added; consumers MUST handle an
unknown code as a generic error while preserving it for diagnostics. Messages
MUST be safe for logs, MUST NOT contain credentials or sensitive payloads, and
MUST NOT be parsed. Adapters SHOULD attach the command/correlation IDs and safe
context to structured audit events.

## 12. Security boundaries

Production architecture MUST enforce all of the following:

- unique credentials per physical device; no shared fleet-wide device password;
- TLS for MQTT, including server authentication and an approved certificate and
  cipher policy;
- broker topic ACLs limited to the authenticated device's identity and direction;
- strict house isolation and no device access to another house;
- credential revocation and rotation without changing `device_id`;
- no credentials, tokens, private keys, or other secrets in MQTT payloads,
  retained messages, error messages, or logs; and
- authoritative server-side authorization and capability validation even when
  transport ACLs have already allowed delivery.

Transport authentication proves a principal, not authorization for arbitrary
payload identity. Core MUST derive/check house and device ownership against its
trusted registry. Rate, size, schema, replay, and timestamp checks occur at the
adapter boundary before an event enters DeviceService or Automation Engine.

Secure Boot, Flash Encryption, protected key storage, anti-rollback controls,
and signed OTA images are future firmware-layer requirements. They are not
implemented or specified operationally here.

## 13. Conceptual provisioning state machine

```text
manufactured -> unprovisioned -> provisioning -> active -> revoked
                         ^             |
                         +-------------+  (failed/cancelled attempt)
```

- `manufactured`: factory identity and model metadata created; never authorized
  to a house.
- `unprovisioned`: eligible to begin ownership binding, but no normal device
  topic access.
- `provisioning`: short-lived enrollment in progress with narrowly scoped
  authorization.
- `active`: bound to exactly one house and permitted normal protocol operation.
- `revoked`: credentials and normal topic access disabled. Revocation does not
  erase the stable device identity or audit history.

Only a trusted provisioning authority may transition states. A failed or
cancelled provisioning attempt returns safely to `unprovisioned`; a revoked
device requires an explicit trusted recovery/re-provisioning flow rather than
self-reactivation. Exact flows, bootstrap credentials, ownership transfer, and
storage are deferred to a later provisioning design and implementation.

## 14. Transport abstraction

`DeviceService` and `Automation Engine` MUST NOT construct, parse, subscribe to,
or otherwise depend directly on MQTT topic structure. They operate on normalized
`Device`, `Capability`, `State`, `Command`, and `Event` objects. A transport
interface accepts a normalized Command and delivers normalized inbound Events.

MQTT is an adapter responsible for topic mapping, JSON encoding/decoding,
protocol-version routing, QoS/retention choices, and transport authentication
context. Future Zigbee, BLE Mesh, and Matter adapters map their clusters,
attributes, addresses, and delivery outcomes into the same normalized model.
Transport-specific metadata MAY be carried as adapter diagnostics but MUST NOT
become identity, capability, or authorization truth.

## 15. Events and correlation

The same `correlation_id` is preserved through:

```text
API request or Automation event
  -> DeviceService
  -> transport adapter
  -> device command
  -> ACK and resulting state event
```

`command_id` identifies one idempotent device command; `correlation_id` groups
the wider causal chain, which may contain several commands and events. Every
boundary SHOULD record both where applicable, together with trusted house/device
identity and timestamps. A spontaneous device event starts a new correlation
chain at its first trusted ingress when necessary.

This enables an operator to reconstruct causality without MQTT log scraping and
lets loop protection recognize events produced by the same automation chain.
Correlation alone is not authorization and MUST NOT suppress unrelated events.
Core still enforces maximum automation depth/event count and must not trust a
device-supplied ID to grant access.

## 16. AI security boundary

A future AI component may produce only proposed declarative commands or
automations. Its output passes the same schema, capability, house, permission,
and loop validation as any untrusted API input. AI MUST NEVER:

- publish arbitrary MQTT messages;
- execute shell commands or Python;
- access device credentials;
- bypass capability validation;
- bypass house or permission validation;
- write directly to hardware; or
- invent devices, capabilities, transport addresses, or successful outcomes.

Only DeviceService may turn an authorized validated proposal into a normalized
Command, and only a transport adapter may encode it for a device.

## 17. Protocol limits

Implementations MUST reject over-limit input before expensive processing. A
deployment MAY configure stricter values; relaxing these v1 wire limits requires
an explicit reviewed extension.

| Item | v1 limit |
|---|---|
| MQTT JSON payload | 16 KiB encoded UTF-8 for state, set, ACK, and status; 32 KiB for telemetry |
| State fields | 64 per state object |
| Telemetry metrics | 128 per `metrics` object |
| JSON nesting | 8 levels maximum; capability state SHOULD be flat |
| Identifier length | 128 UTF-8 bytes for IDs; topic-safe IDs use a stricter adapter character policy |
| General string value | 1,024 UTF-8 bytes unless its schema is stricter |
| Human-readable `message` | 512 UTF-8 bytes |
| Telemetry frequency | Average at most 1 message/second/device, burst at most 10; slower defaults SHOULD be used |
| Heartbeat frequency | Interval from 30 seconds to 5 minutes; 60 seconds recommended |

High-rate measurements SHOULD be batched, aggregated, or handled by a separate
explicitly designed stream rather than weakening this control protocol. Core
and broker apply rate limits and may drop excess telemetry; state, ACK, and
status processing takes priority.

## 18. Complete payload examples

### Relay/light current state

Published by the device to `kzhome/v1/house_123/device_light_7/state`:

```json
{
  "timestamp": "2026-09-11T12:00:01.000Z",
  "correlation_id": "corr_01JABC",
  "state": {
    "on": true,
    "brightness": 65
  }
}
```

### Brightness command

Published by Core to `kzhome/v1/house_123/device_light_7/set`:

```json
{
  "command_id": "cmd_01JXYZ",
  "correlation_id": "corr_01JABC",
  "timestamp": "2026-09-11T12:00:00.000Z",
  "state": {
    "brightness": 65
  }
}
```

### Command ACK

Published by the device to `kzhome/v1/house_123/device_light_7/ack`:

```json
{
  "command_id": "cmd_01JXYZ",
  "correlation_id": "corr_01JABC",
  "timestamp": "2026-09-11T12:00:00.420Z",
  "status": "applied"
}
```

A failure example adds stable diagnostics:

```json
{
  "command_id": "cmd_01JXYZ",
  "correlation_id": "corr_01JABC",
  "timestamp": "2026-09-11T12:00:00.420Z",
  "status": "failed",
  "error_code": "hardware_failure",
  "message": "Dimmer did not confirm output"
}
```

### Motion sensor current state

Published to `kzhome/v1/house_123/device_motion_2/state`; `motion` is declared
read-only and cannot appear in a command:

```json
{
  "timestamp": "2026-09-11T12:02:03.120Z",
  "correlation_id": "corr_01JMOTION",
  "state": {
    "motion": true
  }
}
```

### Device online status

Published retained to `kzhome/v1/house_123/device_light_7/status`:

```json
{
  "timestamp": "2026-09-11T12:03:00.000Z",
  "status": "online",
  "last_seen": "2026-09-11T12:03:00.000Z",
  "heartbeat_interval_seconds": 60
}
```

### Telemetry

Published non-retained to
`kzhome/v1/house_123/device_light_7/telemetry`:

```json
{
  "timestamp": "2026-09-11T12:03:10.000Z",
  "metrics": {
    "rssi": -61,
    "uptime": 86420,
    "temperature": 42.5,
    "free_heap": 118240,
    "firmware_version": "1.4.2"
  }
}
```

Metric units for this model (for example dBm, seconds, degrees Celsius, and
bytes) are defined by its trusted telemetry schema, not inferred from values.

## 19. Compatibility with the current code

The existing `MQTTTransport` is explicitly a placeholder and already isolates
an MQTT client behind the transport layer, which is directionally compatible
with this design. It is not wire-compatible with the frozen v1 mapping:

- it uses `kzhome/{house_id}/{device_id}/...` without the `v1` segment;
- it sends a bare state object on `set`, without `command_id`,
  `correlation_id`, or `timestamp`;
- it sends and accepts a bare object on `state`, rather than the event envelope;
- it has no ACK, telemetry, status, heartbeat, or LWT behavior;
- its client publish interface cannot express QoS or retained policy; and
- inbound filtering validates topic shape only minimally and has no protocol
  limits or transport identity/house authorization context.

Those differences are expected future adapter work, not silent changes to the
current runtime. Implementing v1 MUST update the adapter behind the normalized
transport interface and add compatibility/migration tests deliberately; this
architecture-only change does neither.

## 20. Non-goals for this change

- No real MQTT broker connection.
- No authentication or RBAC implementation.
- No provisioning implementation.
- No OTA implementation.
- No BLE Mesh, Zigbee, or Matter implementation.
- No database schema changes.
- No migrations.
- No AI implementation.
- No services, models, API endpoints, simulator behavior, firmware, hardware
  integration, or runtime transport changes.
