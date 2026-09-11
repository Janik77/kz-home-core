# KZ Home Device Protocol v1

This document freezes the first transport-independent device contract for KZ Home.
The Core domain works with normalized `Device`, `Capability`, `State`, `Command`, and
`Event` concepts. MQTT is only one adapter. Future BLE Mesh, Zigbee, Matter, or
other transports must map into the same model instead of changing Core business
rules.

## 1. Protocol versioning

Protocol major version: `v1`.

For MQTT, all wire topics use a versioned namespace:

```text
kzhome/v1/{house_id}/{device_id}/...
```

Backward-compatible additions may remain in `v1`. Any breaking wire-contract
change requires a new major namespace, for example `kzhome/v2/...`.

The protocol version is not the same as firmware version or hardware model.

## 2. Device identity

Every physical device has a stable identity with at least:

- `device_id`
- `house_id`
- `hardware_model`
- `firmware_version`
- `protocol_version`

`device_id` is stable and unique within KZ Home. It must not be derived from an
MQTT topic or treated as transport-specific identity.

Recommended normalized identity example:

```json
{
  "device_id": "hall_light_01",
  "house_id": "house_001",
  "hardware_model": "kzh-relay-c6-1ch",
  "firmware_version": "1.0.0",
  "protocol_version": "v1"
}
```

## 3. Capability model

Capabilities are declarative allow-lists describing exactly what a device can
report or accept. The future AI layer, API clients, transports, and automations
must never invent capabilities.

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

Cover / curtain:

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

Motion sensor:

```json
{
  "motion": {
    "type": "boolean",
    "writable": false
  },
  "battery": {
    "type": "integer",
    "min": 0,
    "max": 100,
    "writable": false
  }
}
```

Sensor-only capabilities are read-only.

## 4. State semantics

KZ Home distinguishes desired state from current device-reported state.

- **Desired state** is command intent from Core.
- **Current state** is what the device reports after actual processing.
- Current state is authoritative for physical reality.

State contains only fields backed by declared capabilities. Unknown fields are
rejected. Writable commands may modify only capabilities marked `writable=true`.
Invalid commands are rejected atomically rather than partially applied.

Example current state:

```json
{
  "on": true,
  "brightness": 42
}
```

## 5. Command envelope

Commands from Core to a device contain at least:

```json
{
  "command_id": "01J9XYZ123ABC",
  "correlation_id": "corr-7a69e7d3",
  "timestamp": "2026-09-11T14:00:00Z",
  "state": {
    "on": true,
    "brightness": 35
  }
}
```

Requirements:

- `command_id` uniquely identifies one logical device command.
- `correlation_id` traces the parent API / automation execution.
- `timestamp` is ISO 8601 UTC.
- `state` must pass capability validation.

Devices must treat duplicate `command_id` values idempotently. Retransmission of
the same command must not cause unsafe repeated physical actions.

## 6. MQTT v1 topics

### State

```text
kzhome/v1/{house_id}/{device_id}/state
```

- Publisher: device
- Subscriber: Core / gateway
- Purpose: authoritative current device state
- QoS: 1 recommended
- Retained: yes, current snapshot only

### Command

```text
kzhome/v1/{house_id}/{device_id}/set
```

- Publisher: Core / gateway
- Subscriber: target device
- Purpose: desired state command envelope
- QoS: 1 recommended
- Retained: no

### ACK

```text
kzhome/v1/{house_id}/{device_id}/ack
```

- Publisher: device
- Subscriber: Core / gateway
- Purpose: command lifecycle acknowledgement
- QoS: 1 recommended
- Retained: no

### Telemetry

```text
kzhome/v1/{house_id}/{device_id}/telemetry
```

- Publisher: device
- Subscriber: Core / observability consumer
- Purpose: diagnostic and operational measurements
- QoS: 0 normally; 1 only where loss is unacceptable
- Retained: no

### Status

```text
kzhome/v1/{house_id}/{device_id}/status
```

- Publisher: device / MQTT broker LWT
- Subscriber: Core / gateway
- Purpose: availability and last-seen data
- QoS: 1 recommended
- Retained: yes

## 7. Command acknowledgement

ACK payload:

```json
{
  "command_id": "01J9XYZ123ABC",
  "correlation_id": "corr-7a69e7d3",
  "status": "applied",
  "timestamp": "2026-09-11T14:00:01Z"
}
```

Allowed statuses:

- `accepted` — syntactically valid and queued / accepted by the device
- `applied` — physical execution completed successfully
- `rejected` — device intentionally refused the command
- `failed` — execution was attempted but failed

Optional fields:

```json
{
  "error_code": "hardware_failure",
  "message": "relay driver reported failure"
}
```

MQTT delivery is not proof of physical execution. An MQTT PUBACK or an
application-level `accepted` ACK does not mean the relay, motor, light, or other
hardware actually changed state.

## 8. Online / offline and heartbeat

A device publishes retained availability on the `status` topic.

Online example:

```json
{
  "online": true,
  "last_seen": "2026-09-11T14:00:00Z",
  "firmware_version": "1.0.0"
}
```

Offline example:

```json
{
  "online": false,
  "last_seen": "2026-09-11T13:59:30Z"
}
```

Production MQTT devices must configure a retained Last Will and Testament that
marks the device offline if the broker detects an ungraceful disconnect.

Recommended heartbeat interval for normal powered devices: 30-60 seconds.
Battery devices may use a longer interval according to power constraints.

## 9. Telemetry

Telemetry is diagnostic data and is separate from authoritative writable state.

Example:

```json
{
  "timestamp": "2026-09-11T14:00:00Z",
  "rssi": -61,
  "uptime_s": 86400,
  "free_heap": 183120,
  "board_temperature_c": 41.2,
  "firmware_version": "1.0.0"
}
```

Telemetry must not automatically become writable state or a command surface.

## 10. Error model

Machine-readable error codes are stable protocol values. Human-readable
messages are optional diagnostics and must not be the only error signal.

Initial v1 codes:

- `unsupported_capability`
- `invalid_value`
- `device_busy`
- `hardware_failure`
- `unauthorized`
- `timeout`
- `protocol_error`

Future compatible error codes may be added without changing the v1 major
namespace.

## 11. Security boundaries

Protocol v1 requires the following architecture for production integrations:

- unique credentials per physical device
- TLS for production MQTT
- broker topic ACLs
- strict house isolation
- credential revocation
- credential rotation
- no shared fleet-wide device password
- no secrets in MQTT payloads, EventLog, or application logs
- a device may not access another house namespace
- server-side authorization is authoritative

Firmware security requirements include Secure Boot, Flash Encryption, signed
OTA, rollback protection, and protected device credentials. They are not
implemented by this documentation-only change.

## 12. Provisioning state machine

Conceptual lifecycle:

```text
manufactured
    ↓
unprovisioned
    ↓
provisioning
    ↓
active
    ↓
revoked
```

- `manufactured`: device exists but has no customer assignment.
- `unprovisioned`: ready for secure onboarding.
- `provisioning`: credentials and house assignment are being established.
- `active`: allowed to participate in the house protocol.
- `revoked`: credentials / authorization are invalidated and normal operation is denied.

The actual provisioning implementation comes in a later version.

## 13. Transport abstraction

Core business logic must not depend on MQTT topic strings.

Core works on normalized concepts:

```text
Device
Capability
State
Command
Event
```

Adapters translate between transport-specific messages and these normalized
objects.

```text
Automation / API
      ↓
DeviceService
      ↓
Normalized Command
      ↓
Transport adapter
      ↓
MQTT / BLE Mesh / Zigbee / Matter
```

No transport adapter may bypass DeviceService, capability validation, house
isolation, or later authorization checks.

## 14. Event and correlation flow

`correlation_id` is preserved end to end:

```text
API / Automation
→ DeviceService
→ Transport
→ command payload
→ device ACK
→ device state event
→ EventLog / WebSocket
```

This allows audit reconstruction, troubleshooting, and automation loop
protection.

## 15. AI security boundary

A future AI layer may propose only declarative commands or automation rules.
They must pass the same validation as any other client.

AI must never:

- publish arbitrary MQTT messages
- execute shell commands
- execute arbitrary Python
- access device credentials
- bypass capability validation
- bypass house / permission validation
- write directly to physical hardware

## 16. Protocol limits

Initial v1 limits should remain conservative and bounded:

- maximum MQTT application payload: 16 KiB
- maximum state fields per message: 64
- maximum capability fields per device: 64
- maximum string field length: 512 characters unless explicitly narrower
- maximum human diagnostic message length: 1024 characters
- normal powered-device heartbeat: no faster than once per 10 seconds; 30-60 seconds recommended
- normal telemetry: no faster than once per second unless a device profile explicitly requires otherwise

Gateways must reject oversized or structurally invalid payloads before they reach
Core business logic.

## 17. Complete examples

### Relay / light state

Topic:

```text
kzhome/v1/house_001/hall_light_01/state
```

Payload:

```json
{
  "on": true
}
```

### Brightness command

Topic:

```text
kzhome/v1/house_001/hall_light_01/set
```

Payload:

```json
{
  "command_id": "01J9XYZ123ABC",
  "correlation_id": "corr-7a69e7d3",
  "timestamp": "2026-09-11T14:00:00Z",
  "state": {
    "on": true,
    "brightness": 15
  }
}
```

### Command ACK

Topic:

```text
kzhome/v1/house_001/hall_light_01/ack
```

Payload:

```json
{
  "command_id": "01J9XYZ123ABC",
  "correlation_id": "corr-7a69e7d3",
  "status": "applied",
  "timestamp": "2026-09-11T14:00:01Z"
}
```

### Motion sensor state

Topic:

```text
kzhome/v1/house_001/hall_motion_01/state
```

Payload:

```json
{
  "motion": true,
  "battery": 91
}
```

### Device online status

Topic:

```text
kzhome/v1/house_001/hall_light_01/status
```

Payload:

```json
{
  "online": true,
  "last_seen": "2026-09-11T14:00:00Z",
  "firmware_version": "1.0.0"
}
```

### Telemetry

Topic:

```text
kzhome/v1/house_001/hall_light_01/telemetry
```

Payload:

```json
{
  "timestamp": "2026-09-11T14:00:00Z",
  "rssi": -61,
  "uptime_s": 86400,
  "free_heap": 183120,
  "firmware_version": "1.0.0"
}
```

## 18. Non-goals for this protocol-freeze change

This document does not implement:

- a real MQTT broker connection
- authentication or RBAC
- secure provisioning
- OTA
- BLE Mesh
- Zigbee
- Matter
- database schema changes
- Alembic migrations
- AI execution

The next implementation stage should adapt the existing MQTT transport to this
frozen contract without allowing transport-specific concerns to leak into Core.
