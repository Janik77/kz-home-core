# MQTT subscription readiness regression

Baseline: Core main `2359807c2781ce956ed43bc603772b66dd27955b`.
Firmware main: `c9ba186907fec6bf6237f4ce811272743ece0334`.

## Confirmed defect and minimal repair

The pinned aiomqtt 2.4.0 `Client.subscribe()` returns SUBACK codes; its callback
resolves the future even for a broker rejection. AiomqttClient discarded that
result. Consequently MQTTGateway could announce `mqtt_connected` without its
required inbound subscriptions, or set `control_subscribed=True` after denial
of `control/request`. Missing ACK/state delivery could leave commands timing out
while transport diagnostics reported readiness.

Validate the one-filter SUBACK in the existing adapter. Only a single grant
(0, 1 or 2, including Paho ReasonCode values) succeeds. Denial or malformed
cardinality raises ConnectionError. Existing gateway behavior then disconnects
and backs off for mandatory subscriptions, or keeps the optional physical
control subscription closed. No broker permissions are modified. A granted
subscription is not proof of device execution or of future message delivery.

## Independent contract review

- Device identity and house binding pass through DeviceService.require_house and
  PhysicalBindingGuard. StateValidator checks advertised capabilities and types;
  command validation excludes read-only sensor fields.
- CommandService persists ID, correlation, intent, original deadline and scoped
  idempotency hash before publish. Conflicting key reuse is rejected. No replay
  worker or reconnect command resubmission is introduced.
- Desired commands do not overwrite current Device state. Current reports go
  through report_state; changed values generate device_state_changed events.
- Applied completion requires scoped/correlated applied ACK plus matching live
  boolean state; retained messages cannot complete the ledger. Control records
  additionally require permit evidence. PUBACK alone is not execution evidence.
- MQTT topic QoS/retain mapping and wire envelopes remain unchanged. Production
  settings require TLS; AiomqttClient uses the default verified SSL context.
  Reconnect restores subscriptions before announcing readiness. This fix closes
  the missing broker-grant check in that readiness sequence.
- PhysicalBindingGuard.require_command_contract still rejects physical HTTP
  commands. Firmware production qualification remains false. Successful software
  command tests and host C tests do not qualify physical actuation.

No AGENTS.md or .agents/skills files are present in this Core revision.

## Reproduction and evidence boundaries

```
python -m pytest tests/test_mqtt_lifecycle.py -q
python -m pytest tests/test_mqtt_lifecycle.py tests/test_mqtt.py tests/test_command_lifecycle.py tests/test_control_authority.py -q
python -m ruff check .
python /path/to/firmware/tests/run_host_tests.py --cmake /path/to/cmake --core-root "$PWD"
```

Eight new negative SUBACK cases fail on unmodified main. Lifecycle tests after
repair: 20 passed. They cover integer and Paho ReasonCode grants/refusals,
empty/multiple results, mandatory versus optional control denial, cleanup,
readiness events, and existing reconnect/backoff behavior. Two tests use actual
aiomqtt/Paho over loopback TCP against a minimal MQTT 3.1.1 protocol peer. That
peer is a test fixture, not Mosquitto; those sockets deliberately do not use TLS.
The adapter/gateway denial matrix uses substituted aiomqtt sessions.

Firmware main host runner passes against patched Core: 20,361 legacy checks,
40,205 control checks, five actual Core authority/C scenarios, eight GPIO stub
variants, 31 compile guards, CMake isolation, both MQTT worker suites and 12
status messages per board accepted by actual Core. SDK scheduling, time, NVS
and GPIO are substituted. This does not demonstrate ESP-IDF/hardware execution.

Docker, PostgreSQL and Mosquitto are unavailable in this local environment.
Live broker ACL/TLS refusal and service E2E were not run here.

## Existing PR compatibility and CI dependency

Core PR #32 (`159eb98f2349b1aa2b8afd313c3a3f1fbf9b6bd2`) owns CI and obsolete
migration-head test corrections. This patch changes neither those files nor
migration expectations. Main currently has no workflow; PR #32 must supply CI
before this main-based PR can obtain its normal Actions checks. Do not interpret
an empty check list as success, or reuse PR #32's green run for this patch.
Core PR #14 is an older authentication foundation; this transport-only patch
introduces no auth/schema changes.

Firmware PRs #6/#8 add host/target CI. #7/#9/#10 add service/HTTP-worker tests and
physical-contract documentation, with #9/#10 stacked on #7 and the service
fixture pinned to Core PR #32. This patch changes no firmware-facing payloads.
Those pinned service tests must explicitly select this Core revision (plus #32's
migration-test corrections) before claiming service integration for this fix.
No existing PR, main branch, secret, gate, deployment or hardware is changed.
