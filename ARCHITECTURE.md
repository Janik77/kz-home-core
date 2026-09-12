# KZ Home Core architecture

## Layers

1. **API layer** (`app/api`) validates HTTP input with Pydantic and delegates to services.
2. **Service layer** (`app/services`) owns device, scene and automation business rules.
3. **Repository layer** (`app/repositories`) contains parameterized SQLAlchemy 2 queries.
4. **Database** (`app/models`, `app/db`, `alembic`) persists the house graph, rules and audit events in PostgreSQL; JSON documents use JSONB.
5. **EventBus** (`app/events`) connects state changes to automation, event history and WebSocket delivery in one process.
6. **Transport layer** (`app/transports`) isolates device commands from MQTT or future hardware protocols.

The transport-independent device contract and proposed MQTT v1 mapping are
defined in [Device Protocol v1](DEVICE_PROTOCOL.md). Runtime transport support
is intentionally separate from that architecture specification.

## Automation Engine

Automations are declarative data: one `device_state` trigger, AND-combined
`device_state`/`time` conditions, and ordered `device_state`/`delay` actions.
Comparisons use the fixed operators `eq`, `neq`, `gt`, `gte`, `lt`, and `lte`.
StateValidator checks every command against the target device capabilities and
house boundary before a rule is stored. Correlation IDs and a maximum execution
depth protect event chains from loops. Each rule failure is isolated and recorded
in EventLog without secrets.

There are deliberately no dynamic imports, `eval`, `exec`, shell commands, or
general expression language in the engine.

## Human identity and authorization

Authentication follows the same API → service → repository → database boundary.
Users have normalized unique email addresses and Argon2id hashes, never plaintext
passwords. Short-lived signed access JWTs identify a human; refresh JWT identifiers
are SHA-256 hashed in server-side sessions, revoked on logout, and rotated on every
refresh. Deactivation immediately prevents login, refresh, and authenticated API use.
Production rejects absent, short, or placeholder signing secrets.

House membership is the authorization boundary: membership in one house conveys
no rights in another. The centralized permission matrix grants owners every house
permission, narrower commissioning rights to installers, diagnostic device rights
to technicians, and normal control/scene rights to residents. Authentication only
establishes identity; `require_house_permission` performs the separate house-scoped
authorization step. v0.6a establishes and tests this boundary, while v0.6b will
apply it across existing endpoints.

Human JWT identity is separate from physical-device identity. MQTT devices must
never use user tokens, and device credentials must never be stored in `users` or
refresh sessions. Device provisioning and credentials are future work.

Any future AI action must execute with the authenticated caller's identity and
permissions. AI cannot elevate roles, alter membership, bypass
`require_house_permission`, access JWT secrets/refresh tokens/device credentials,
call transports directly, or bypass `DeviceService` and `StateValidator`.

## Device Gateway and future boundaries

The v0.5 MQTT Gateway is a lifecycle-managed transport adapter. It maps Device
Protocol v1 envelopes to normalized events, but opens database sessions only
through application callbacks that invoke DeviceService. DeviceService remains
the authority for identity, house, capability, and state validation. Broker
reconnect runs in the background and does not gate HTTP application startup.

A future **AI layer** may translate natural language into automation JSON, but
that JSON must pass the same Pydantic, capability, device and house validation;
AI will never execute code. Future **hardware layers** (MQTT devices, BLE Mesh,
Zigbee, Matter) will implement transport interfaces and cannot bypass services or
state validation. BLE Mesh, Zigbee, and Matter integrations are not implemented.
