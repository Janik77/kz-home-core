"""Core-only expiry foundation: physical dispatch is deliberately unavailable."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from app.core import Settings
from app.core.errors import ConflictError, InvalidReferenceError, UnavailableError
from app.events import EventBus
from app.models import DeviceCommandORM, DeviceORM
from app.services.command_clock import CommandClock
from app.transports.mqtt import MQTTGateway
from app.transports import mqtt
from app.transports.mqtt_client import FakeMQTTClient, MQTTMessage
from simulator.mqtt_relay import Relay
from test_command_lifecycle import fixture
from test_onboarding import activate, claim_device, onboarding as onboarding_fixture


@pytest.fixture(params=["esp32-c6-relay-v1", "esp32-relay-v1"])
def onboarding(tmp_path, monkeypatch, request):
    yield from onboarding_fixture.__wrapped__(tmp_path, monkeypatch, request)


def gateway(monkeypatch):
    clock_values = [datetime(2026, 11, 2, tzinfo=UTC), 0.0]
    clock = CommandClock(lambda: clock_values[0], lambda: clock_values[1])
    monkeypatch.setattr("app.transports.mqtt.command_clock", clock)
    wire = FakeMQTTClient()
    guard = AsyncMock()
    service = MQTTGateway(
        wire, EventBus(), AsyncMock(), AsyncMock(), AsyncMock(), command_guard=guard
    )
    return service, wire, clock_values


def advance(values, seconds):
    values[0] += timedelta(seconds=seconds)
    values[1] += seconds


def test_original_deadline_survives_delay_repeat_and_reconnect(monkeypatch):
    service, wire, values = gateway(monkeypatch)
    issued = values[0]
    deadline = issued + timedelta(seconds=30)
    kwargs = dict(
        command_id="same",
        correlation_id="same-correlation",
        timestamp=issued,
        expires_at=deadline,
    )

    async def check():
        advance(values, 12)
        await service.send_command("house", "software", {"on": True}, **kwargs)
        advance(values, 5)
        # A transport reconnect must not derive a fresh timestamp/window.
        await service._disconnect()
        await wire.connect()
        await service.send_command("house", "software", {"on": True}, **kwargs)
        assert wire.published[0] == wire.published[1]
        body = json.loads(wire.published[0][1])
        assert (
            datetime.fromisoformat(body["timestamp"].replace("Z", "+00:00")) == issued
        )
        assert (
            datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
            == deadline
        )
        advance(values, 13)
        with pytest.raises(ConflictError):
            await service.send_command("house", "software", {"on": True}, **kwargs)
        assert len(wire.published) == 2

    asyncio.run(check())


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": None},
        {"timestamp": None},
        {"expires_at": datetime(2026, 11, 2)},
        {"expires_at": datetime(2026, 11, 2, tzinfo=UTC)},
        {"expires_at": datetime(2026, 11, 2, tzinfo=UTC) + timedelta(seconds=31)},
    ],
)
def test_gateway_has_no_deadline_fallback(monkeypatch, changes):
    service, wire, values = gateway(monkeypatch)
    kwargs = {
        "timestamp": values[0],
        "expires_at": values[0] + timedelta(seconds=30),
        **changes,
    }
    with pytest.raises(InvalidReferenceError):
        asyncio.run(service.send_command("house", "software", {"on": True}, **kwargs))
    assert wire.published == []


@pytest.mark.parametrize("jump", [-60, 60])
def test_clock_fault_latches_and_does_not_publish(monkeypatch, jump):
    service, wire, values = gateway(monkeypatch)
    kwargs = dict(timestamp=values[0], expires_at=values[0] + timedelta(seconds=30))
    service_clock = mqtt.command_clock
    service_clock.now()
    values[0] += timedelta(seconds=jump)
    with pytest.raises(UnavailableError):
        asyncio.run(service.send_command("house", "software", {"on": True}, **kwargs))
    values[0] -= timedelta(seconds=jump)
    with pytest.raises(UnavailableError):
        service_clock.now()
    assert wire.published == []


def test_no_guard_cannot_publish_even_with_valid_deadline():
    wire = FakeMQTTClient()
    service = MQTTGateway(wire, EventBus(), AsyncMock(), AsyncMock(), AsyncMock())
    with pytest.raises(ConflictError):
        asyncio.run(service.send_command("house", "device", {"on": True}))
    assert wire.published == []


def test_software_relay_rejects_delayed_first_delivery():
    now = datetime.now(UTC)
    relay = Relay("house", "software", AsyncMock())
    raw = json.dumps(
        {
            "command_id": "delayed",
            "correlation_id": "correlation",
            "timestamp": (now - timedelta(seconds=60)).isoformat(),
            "expires_at": (now - timedelta(seconds=30)).isoformat(),
            "state": {"on": True},
        }
    ).encode()
    asyncio.run(relay.handle("kzhome/v1/house/software/set", raw))
    assert not relay.on
    body = json.loads(relay.publish.call_args.args[1])
    assert body["status"] == "rejected" and body["error_code"] == "timeout"


@pytest.mark.parametrize(
    "path,method,body",
    [
        ("/devices/physical1/on", "post", None),
        ("/devices/physical1/off", "post", None),
        ("/devices/physical1/state", "patch", {"on": True}),
        ("/v1/devices/physical1/commands", "post", {"on": True}),
    ],
)
def test_old_physical_device_is_blocked_in_all_http_paths(
    onboarding, path, method, body
):
    client, engine, headers, _, wire, _ = onboarding
    claim_device(onboarding)
    assert activate(onboarding).status_code == 200
    with Session(engine) as session:
        session.get(DeviceORM, "physical1").online = True
        session.commit()
    response = getattr(client, method)(
        path,
        json=body,
        headers={
            **headers["owner"],
            "Idempotency-Key": "explicit-test-intent-key",
        },
    )
    assert response.status_code == 409
    assert wire.published == []
    with Session(engine) as session:
        assert session.query(DeviceCommandORM).count() == 0


def test_scene_and_automation_cannot_bypass_physical_gate(onboarding):
    client, _, headers, _, wire, _ = onboarding
    claim_device(onboarding)
    activate(onboarding)
    action = {"device_id": "physical1", "state": {"on": True}}
    for kind, body, action_path in [
        ("scenes", {"actions": [action]}, "run"),
        (
            "automations",
            {
                "trigger": {"device_id": "physical1", "field": "on", "value": True},
                "actions": [action],
            },
            "run",
        ),
    ]:
        response = client.post(
            f"/{kind}",
            json={"id": "blocked", "name": "Blocked", "house_id": "a", **body},
            headers=headers["owner"],
        )
        assert response.status_code == 201, response.text
        response = client.post(
            f"/{kind}/blocked/{action_path}", headers=headers["owner"]
        )
        # Automations report a failed execution event; scenes propagate 409.
        assert response.status_code in (200, 409)
    assert wire.published == []


def test_fake_time_probe_and_metadata_never_enable_physical_dispatch(onboarding):
    client, _, _, _, wire, app = onboarding
    claim_device(onboarding)
    activate(onboarding)
    gateway = app.state.mqtt_gateway
    with pytest.raises(ValueError):
        client.portal.call(
            gateway.handle_message,
            MQTTMessage(
                "kzhome/v1/a/physical1/ack",
                b'{"message_type":"time_probe","nonce":"x"}',
            ),
        )
    client.portal.call(
        gateway.handle_message,
        MQTTMessage(
            "kzhome/v1/a/physical1/status",
            json.dumps(
                {
                    "status": "online",
                    "last_seen": datetime.now(UTC).isoformat(),
                    "firmware_version": "0.12.0",
                    "command_policy": "deadline-v1",
                    "time_proof": "claimed-by-device",
                }
            ).encode(),
        ),
    )
    with pytest.raises(ConflictError):
        client.portal.call(gateway.send_command, "a", "physical1", {"on": True})
    assert wire.published == []


def test_restart_does_not_restore_physical_permission(onboarding):
    from fastapi.testclient import TestClient
    from app.main import create_app

    _, _, _, _, _, original = onboarding
    claim_device(onboarding)
    activate(onboarding)
    wire = FakeMQTTClient()
    restarted = create_app(
        original.state.settings, run_simulator=False, mqtt_client=wire
    )
    with TestClient(restarted) as client:
        with pytest.raises(ConflictError):
            client.portal.call(
                restarted.state.mqtt_gateway.send_command,
                "a",
                "physical1",
                {"on": True},
            )
    assert wire.published == []


def test_unclassified_device_metadata_cannot_claim_software_permission(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            await h.api.request(
                "POST",
                "/devices",
                {
                    "id": "unclassified",
                    "room_id": "fixture_room",
                    "name": "Unknown",
                    "type": "relay",
                    "capabilities": ["on_off"],
                    "online": True,
                    "metadata": {"software": True, "command_policy": "deadline-v1"},
                },
            )
            response = await h.post(device="unclassified")
            assert response.status_code == 409
            assert h.wire.published == []

    asyncio.run(check())


def test_ledger_deadline_is_wire_deadline_and_idempotency_does_not_resend(
    tmp_path, monkeypatch
):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            key = "immutable-original-deadline"
            receipt = (await h.post(key=key)).json()
            sent = json.loads(h.wire.published[-1][1])

            def parse(value):
                return datetime.fromisoformat(value.replace("Z", "+00:00"))

            assert parse(sent["expires_at"]) == parse(receipt["expires_at"])
            assert parse(sent["timestamp"]) == parse(receipt["created_at"])
            assert (await h.post(key=key)).json() == receipt
            assert len(h.wire.published) == 1
            h.expire(receipt)
            await h.ack(receipt)
            await h.state(receipt, True)
            assert (await h.get(receipt)).json()["status"] == "timed_out"
            assert (await h.post(key=key)).json()["status"] == "timed_out"
            assert len(h.wire.published) == 1

    asyncio.run(check())


def test_software_allowlist_is_explicit_and_strict(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("MQTT_ENABLED", "false")
    monkeypatch.delenv("MQTT_SOFTWARE_DEVICES", raising=False)
    assert Settings.from_env().mqtt_software_devices == ()
    monkeypatch.setenv("MQTT_SOFTWARE_DEVICES", '[["house","software"]]')
    assert Settings.from_env().mqtt_software_devices == (("house", "software"),)
    monkeypatch.setenv("MQTT_SOFTWARE_DEVICES", '[["house","+"]]')
    with pytest.raises(ValueError):
        Settings.from_env()
