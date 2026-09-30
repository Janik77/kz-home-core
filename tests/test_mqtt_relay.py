import asyncio
import json
from datetime import timedelta

import pytest

from app.transports.mqtt_models import CommandEnvelope, utc_now
from simulator.mqtt_relay import Config, Relay


def command(command_id="one", **changes):
    return (
        CommandEnvelope(
            command_id=command_id,
            correlation_id="correlation",
            timestamp=utc_now(),
            state={"on": True},
        )
        .model_copy(update=changes)
        .model_dump_json()
        .encode()
    )


def harness():
    published = []

    async def publish(topic, payload, **policy):
        published.append((topic, json.loads(payload), policy))

    return Relay("house", "relay", publish), published


def test_environment_configuration_requires_device_identity(monkeypatch):
    values = {
        "MQTT_HOST": "mosquitto",
        "MQTT_USERNAME": "relay-simulator",
        "MQTT_PASSWORD": "unit-test-placeholder",
        "CA_FILE": "/ca.crt",
        "HOUSE_ID": "house",
        "DEVICE_ID": "relay",
        "MQTT_CLIENT_ID": "relay-1",
    }
    for key, value in values.items():
        monkeypatch.setenv("RELAY_" + key, value)
    monkeypatch.delenv("RELAY_MQTT_PORT", raising=False)
    config = Config.from_env()
    assert config.port == 8883
    assert "unit-test-placeholder" not in repr(config)
    monkeypatch.setenv("RELAY_MQTT_USERNAME", "kzhome-core")
    with pytest.raises(ValueError):
        Config.from_env()
    monkeypatch.delenv("RELAY_MQTT_PASSWORD")
    with pytest.raises(ValueError):
        Config.from_env()


def test_applied_state_and_duplicate_does_not_reapply_old_intent():
    async def scenario():
        relay, sent = harness()
        first = command()
        topic = "kzhome/v1/house/relay/set"
        await relay.handle(topic, first)
        assert sent[0][1]["status"] == "applied"
        assert sent[0][1]["command_id"] == "one"
        assert sent[1][1]["state"] == {"on": True}
        assert all(item[1]["correlation_id"] == "correlation" for item in sent)
        assert sent[0][2] == {"qos": 1, "retain": False}
        assert sent[1][2] == {"qos": 1, "retain": True}
        await relay.handle(topic, command("two", state={"on": False}))
        count = len(sent)
        await relay.handle(topic, first)
        assert len(sent) == count + 1
        assert sent[-1][1] == sent[0][1]
        assert relay.on is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "state,code",
    [({"on": 1}, "invalid_value"), ({"brightness": 10}, "unsupported_capability")],
)
def test_rejected_state(state, code):
    relay, sent = harness()
    asyncio.run(relay.handle("kzhome/v1/house/relay/set", command(state=state)))
    assert len(sent) == 1
    assert sent[0][1]["status"] == "rejected"
    assert sent[0][1]["error_code"] == code
    assert relay.on is False


@pytest.mark.parametrize(
    "payload",
    [
        b"invalid",
        b"[]",
        b"{}",
        b"x" * 16385,
        command(protocol_version="v2"),
        command(state={}),
    ],
)
def test_malformed_commands_do_not_publish(payload):
    relay, sent = harness()
    with pytest.raises(ValueError):
        asyncio.run(relay.handle("kzhome/v1/house/relay/set", payload))
    assert sent == []


def test_stale_command_rejected_and_offline_has_last_seen():
    relay, sent = harness()
    asyncio.run(
        relay.handle(
            "kzhome/v1/house/relay/set",
            command(timestamp=utc_now() - timedelta(minutes=6)),
        )
    )
    assert sent[0][1]["error_code"] == "timeout"
    assert relay.status(False).last_seen is not None


def test_retained_wrong_topic_and_conflicting_duplicate_rejected():
    async def scenario():
        relay, _ = harness()
        topic = "kzhome/v1/house/relay/set"
        with pytest.raises(ValueError):
            await relay.handle(topic, command(), retained=True)
        with pytest.raises(ValueError):
            await relay.handle("kzhome/v1/other/relay/set", command())
        await relay.handle(topic, command())
        with pytest.raises(ValueError):
            await relay.handle(topic, command(state={"on": False}))

    asyncio.run(scenario())
