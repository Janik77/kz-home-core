# TEST ONLY: Core PR #32 + #33 integration

This draft integration branch combines Core CI/migration tests from #32
(`159eb98f2349b1aa2b8afd313c3a3f1fbf9b6bd2`) with the exact three-file patch from
#33 (`bbb9d7af8647ecc727b9c8201a936f66fa99a91e`). It is not a replacement feature
PR and must not be merged automatically. Keep the branch/PR until explicitly
instructed otherwise. Original #32/#33 branches are unchanged.

The inherited offline and PostgreSQL jobs remain intact. A separate CI job
builds Mosquitto 2.0.22 from official source commit
`766fa2c5a9bed6c65249cf555f430b982c417480` with TLS enabled. The codeload archive
is SHA-256 pinned to
`c68089994b371eb55eb111869d9b530d86df4612730338f68714ce73980ff6be`.
No sudo, package/service installation, production endpoint, production secret,
deployment, device or permission change is involved. The workflow retains
read-only GitHub permissions and disabled checkout credential persistence.

## Real-broker boundary

`tests/test_mqtt_suback_broker.py` compiles a test-only ACL plugin using official
Mosquitto headers. It denies exactly the configured subscription filter via
MOSQ_ACL_SUBSCRIBE, leaving connection and other subscriptions permitted. An
ordinary read ACL alone is insufficient evidence of a SUBACK denial: a broker
may instead accept the subscription and filter delivery.

The broker is real, built unmodified from pinned source. Core's AiomqttClient,
MQTTGateway, aiomqtt and Paho are real. The deterministic broker ACL policy is a
test fixture, not the production ACL. State/status/identity callback bodies are
unused test handlers; no DB-backed device flow or physical command is exercised.

Eight scenarios cover raw successful/denied broker SUBACK and gateway readiness
for mandatory denial, optional control denial and successful subscriptions, each
with TCP and TLS. TLS uses the normal verified SSL context with a disposable
self-signed trust anchor, valid IP/hostname SAN and one-day lifetime. No insecure
TLS option is used. Broker listeners bind only 127.0.0.1 on temporary ports.
Processes terminate in fixture teardown, and fixture files including keys are
removed. No persistent service is installed. The tiny port allocation/start gap
may fail if another local process claims the port; startup then fails visibly.

## Reproduction

Set SUBACK_MOSQUITTO_SOURCE to the pinned source directory and
SUBACK_MOSQUITTO_BINARY to its built src/mosquitto, then run:

```
python -m pytest tests/test_mqtt_suback_broker.py -v -ra
```

The workflow contains the exact download/checksum/build commands. Missing opt-in
environment makes the ordinary offline suite skip these eight service tests;
the dedicated job always sets both variables and runs all eight. Existing live
PostgreSQL/MQTT/hardware skips are not treated as passing.

Local real-broker suite: 8 passed. The combined full suite before these opt-in
fixtures: 695 passed, 29 skipped. Ruff and whitespace checks pass. Final CI
results and exact integration head are recorded in the draft PR description.

This validates SUBACK handling and TLS connectivity, not physical qualification,
real firmware scheduling, NVS/GPIO, network fault timing or the entire command
lifecycle. Production command gates and Firmware remain untouched.

Official references:
- https://github.com/eclipse-mosquitto/mosquitto/tree/766fa2c5a9bed6c65249cf555f430b982c417480
- https://mosquitto.org/documentation/dynamic-security/
