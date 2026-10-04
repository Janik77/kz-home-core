# Standalone MQTT simulators (v0.8)

For an already claimed/active physical identity with a unique `kzdevice-*` account,
see [v0.12 software acceptance](PHYSICAL_ACCEPTANCE.md). It hosts this same relay
implementation, validates API/event persistence and does not require hardware.

For explicit operator bootstrap, authenticated API control and correlated
PostgreSQL event verification, follow [E2E acceptance](E2E_ACCEPTANCE.md).
Both the HTTP relay ON/OFF and MQTT motion-triggered automation scenarios have
passed live operator acceptance on production Compose, including TLS/authenticated
broker connectivity and Core reconnect/subscription restoration after restart.
These are software-simulator results, not physical ESP32/hardware verification.

Run `python -m simulator.mqtt_relay`. This process uses MQTT only, never Core's
database or HTTP API. It is separate from the in-process virtual demo simulator.
It starts with `on=false`, subscribes to its exact `/set` topic, then publishes
retained online status and state. Heartbeats occur every 60 seconds. Offline LWT
and graceful offline status include `last_seen`, as required by the current Core.

Required environment variables (no dotenv loading):

| Variable | Meaning |
|---|---|
| `RELAY_MQTT_HOST` | Verified broker hostname; `mosquitto` inside Compose |
| `RELAY_MQTT_PORT` | Optional; defaults to `8883` |
| `RELAY_MQTT_USERNAME` | Dedicated device account; never `kzhome-core` |
| `RELAY_MQTT_PASSWORD` | Operator-provided device password |
| `RELAY_MQTT_CLIENT_ID` | Unique client ID, distinct from Core |
| `RELAY_CA_FILE` | PEM CA file inside the simulator container |
| `RELAY_HOUSE_ID` | Registered house ID |
| `RELAY_DEVICE_ID` | Registered device ID belonging to that house |

TLS certificate and hostname verification are always enabled. Only mount the
public CA, never server/CA private keys. The broker SAN must match the hostname.

Commands use existing v1 models and topics. Only boolean `on` is supported.
Successful commands apply state, publish `applied` ACK, then correlated state.
Unsupported fields/invalid values receive `rejected` ACKs. Malformed envelopes,
retained commands, and conflicting reuse of an ID are dropped with a fixed warning.
ACKs copy both IDs; state copies the correlation ID. No secrets/payloads are logged.

Commands expire after 300 seconds; clocks may be at most 30 seconds ahead.
The bounded 4096-entry duplicate cache retains outcomes through command expiry.
Duplicates replay the original ACK without changing state or publishing stale
state. This cache and relay state are **not durable across process restarts**;
this is a commissioning simulator, not a firmware implementation of durable
deduplication. There is no automatic reconnect: failures exit nonzero; restart
explicitly. Core does not yet correlate pending commands or expire heartbeats.

## Existing Docker stack (PowerShell, repository root)

The checked-in optional ACL identity `relay-simulator` can only read
`kzhome/v1/e2e_house/e2e_relay/set` and write that device's `state`, `ack`, `status`.
No telemetry or wildcard access is granted. Core's existing ACL is unchanged.
Register house `e2e_house`, a room in it, and device `e2e_relay` with capability
`on_off` through authorized Core operations first. For different IDs, update the
explicit device ACL paths and environment together; do not grant wildcard access.

Add the password interactively (**omit `-c`**, which would replace Core's account):

```powershell
docker run --rm -it --user 0:0 --entrypoint mosquitto_passwd --mount "type=bind,source=$($PWD.Path)/deploy/local/mqtt,target=/bootstrap" eclipse-mosquitto:2 /bootstrap/passwords relay-simulator
docker compose --env-file deploy/local/production.env -f compose.production.yaml up -d --no-deps --force-recreate mosquitto
docker build -t kz-home-relay:v0.8-local .
```

Recreating the broker briefly interrupts MQTT; Core reconnects. PostgreSQL,
migrations and named volumes are untouched. The new image includes the simulator;
the command override below does not start Core or load production Core settings.

Create the ignored `deploy/local/relay.env` locally with these values, inserting
the password interactively in an editor (never commit it or print it):

```dotenv
RELAY_MQTT_HOST=mosquitto
RELAY_MQTT_PORT=8883
RELAY_MQTT_USERNAME=relay-simulator
RELAY_MQTT_PASSWORD=<operator-provided-device-password>
RELAY_MQTT_CLIENT_ID=e2e-relay-simulator-1
RELAY_CA_FILE=/run/mqtt/ca.crt
RELAY_HOUSE_ID=e2e_house
RELAY_DEVICE_ID=e2e_relay
```

```powershell
docker run --rm --init --name kzhome-relay-simulator --network kzhome_backend --no-healthcheck --env-file deploy/local/relay.env --mount "type=bind,source=$($PWD.Path)/deploy/local/mqtt/ca.crt,target=/run/mqtt/ca.crt,readonly" kz-home-relay:v0.8-local python -m simulator.mqtt_relay
```

Stop from another terminal with `docker stop kzhome-relay-simulator`. No broker
ports are published. The image's Core healthcheck is disabled for this process.
Host files need appropriate OS access controls; the public CA must be readable by
container UID 10001. This command runs the standalone relay only. Use
`simulator.e2e_acceptance --run` for the automated relay acceptance procedure above.

## Motion probe and automation acceptance

`python -m simulator.mqtt_motion --run --motion false` (or `true`) is a one-shot,
MQTT-only probe for `e2e_house/e2e_motion`. It requires the separate `MOTION_*`
environment values documented in Block 2 of [E2E acceptance](E2E_ACCEPTANCE.md).
The `motion-simulator` identity can publish only its own state/status; it has no
subscriptions, relay access, ACK or telemetry permission. TLS verification is
mandatory and it neither calls Core services nor opens the database.

`simulator.e2e_automation --run` hosts that probe and the existing relay through
separate authenticated MQTT connections and verifies the real AutomationService
path via persisted events and GET responses. It provisions compatible fixed-ID
records through RBAC-protected APIs, resets false baselines through MQTT, and
disables its rule on normal exit. No simulator, probe, E2E runner or bootstrap
is automatically started in production; normal pytest only tests their offline logic.
