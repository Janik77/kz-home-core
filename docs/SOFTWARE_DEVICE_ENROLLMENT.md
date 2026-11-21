# Opt-in software-only device enrollment

This is a closed pilot fixture path, not an ESP32/third-party commissioning,
discovery, broker credential, or home install feature. It is off by default
and refused in production. Physical authorization and wire expiry are unchanged.

In a **disposable TLS MQTT lab only**, first provision unique synthetic MQTT
principals with directional ACLs and a stable house and device identifier.
Keep the acquisition of credentials in the runner/installer; not in Web.
The operator must include each exact identity in the normal MQTT software
allowlist and in this separate profile list:

```text
MQTT_SOFTWARE_DEVICES=[["fixture_house","fixture_relay"],["fixture_house","fixture_temp"]]
KZHOME_SOFTWARE_DEVICE_CANDIDATES=[["fixture_house","fixture_relay","relay"],["fixture_house","fixture_temp","temperature_sensor"]]
```

Do not preseed the runtime Device rows for those IDs. Use a valid owner or
installer JWT; no browser credentials (MQTT or operator) are created or sent.
Wait for the runtime record before the fixture sends fresh state and status;
traffic before enrollment cannot discover or enroll devices.

- `GET /v1/houses/{house}/software-device-candidates`: available trusted IDs
  and profile identifiers. The listing is not a liveness probe.
- `POST /v1/houses/{house}/software-device-candidates/{id}/claim`: exact
  `{"room_id":"fixture_room","name":"Hall light"}`. Returns 201 and ordinary
  DeviceRead. Initial online is false and state is empty. Device type and
  capabilities come from the trusted profile, not from the request.
- A deliberate same-body repeat returns the existing device and does not write
  another enrollment audit event. A mismatched repeat returns 409. An unknown
  response should first be reconciled via normal same-house GET, with no
  auto-replay. Foreign scope and unavailable candidate are hidden as 404.
- Existing guarded metadata PATCH can rename/move within the same house.
  Removing the binding or changing houses needs a separate decommission flow;
  keep old identity/history and prevent stale MQTT traffic from being adopted.
- Keep real report time distinct from enrollment and command submission.
  Relay uses only on/off; temperature is observed, never commandable.

Software admission assumes the operator is trusted and the broker publish/
subscribe ACLs were provisioned correctly. Tests with an in-memory MQTT
transport do not verify an actual broker account, Android, Windows, networking,
hardware, two-channel customer acceptance, or an extended run.
