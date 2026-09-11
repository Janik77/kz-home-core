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

## Future boundaries

A future **AI layer** may translate natural language into automation JSON, but
that JSON must pass the same Pydantic, capability, device and house validation;
AI will never execute code. Future **hardware layers** (MQTT devices, BLE Mesh,
Zigbee, Matter) will implement transport interfaces and cannot bypass services or
state validation. None of those integrations is implemented in v0.4.
