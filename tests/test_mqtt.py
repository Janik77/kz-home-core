import asyncio
import json
from datetime import datetime

import pytest

from app.events import Event, EventBus
from app.transports.mqtt import MQTTGateway
from app.transports.mqtt_client import FakeMQTTClient, MQTTMessage
from app.transports.mqtt_models import MAX_CONTROL_PAYLOAD
from app.transports.mqtt_topics import (
    TOPIC_POLICY,
    TopicKind,
    build_topic,
    parse_topic,
    subscription_topics,
)


class GatewayHarness:
    def __init__(self) -> None:
        self.client = FakeMQTTClient()
        self.bus = EventBus()
        self.events: list[Event] = []
        self.states: list[tuple[str, str, dict, str]] = []
        self.statuses: list[tuple[str, str, bool, datetime]] = []
        self.identities = {("home1", "light1"), ("home1", "sensor1")}

        async def collect(event: Event) -> None:
            self.events.append(event)

        async def state(
            house_id: str, device_id: str, value: dict, correlation_id: str
        ) -> None:
            self.states.append((house_id, device_id, value, correlation_id))

        async def status(
            house_id: str, device_id: str, online: bool, last_seen: datetime
        ) -> bool:
            changed = not self.statuses or self.statuses[-1][2] != online
            self.statuses.append((house_id, device_id, online, last_seen))
            return changed

        async def identity(house_id: str, device_id: str) -> None:
            if (house_id, device_id) not in self.identities:
                raise ValueError("unauthorized device identity")

        self.bus.subscribe(collect)
        self.gateway = MQTTGateway(
            self.client, self.bus, state, status, identity, initial_backoff=0.01
        )

    def handle(self, topic: str, payload: dict | bytes) -> None:
        encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        asyncio.run(self.gateway.handle_message(MQTTMessage(topic, encoded)))


def test_topic_builder_parser_and_central_policy() -> None:
    topic = build_topic("home1", "light1", TopicKind.STATE)
    assert topic == "kzhome/v1/home1/light1/state"
    parsed = parse_topic(topic)
    assert (parsed.house_id, parsed.device_id, parsed.kind) == (
        "home1",
        "light1",
        TopicKind.STATE,
    )
    assert TOPIC_POLICY[TopicKind.STATE].qos == 1
    assert TOPIC_POLICY[TopicKind.STATE].retain is True
    assert TOPIC_POLICY[TopicKind.SET].qos == 1
    assert TOPIC_POLICY[TopicKind.SET].retain is False
    assert TOPIC_POLICY[TopicKind.ACK].qos == 1
    assert TOPIC_POLICY[TopicKind.ACK].retain is False
    assert TOPIC_POLICY[TopicKind.STATUS].retain is True
    assert TOPIC_POLICY[TopicKind.TELEMETRY].qos == 0
    assert len(subscription_topics()) == 4


@pytest.mark.parametrize(
    "topic",
    [
        "kzhome/home1/light1/state",
        "kzhome/v2/home1/light1/state",
        "kzhome/v1/home1/light1",
        "kzhome/v1/home1/light1/unknown",
        "kzhome/v1/home+/light1/state",
        "kzhome/v1/home1/bad/device/state",
    ],
)
def test_malformed_topics_are_rejected(topic: str) -> None:
    with pytest.raises(ValueError):
        parse_topic(topic)


def test_command_envelope_correlation_and_publish_policy() -> None:
    harness = GatewayHarness()
    command_id = asyncio.run(
        harness.gateway.send_command(
            "home1", "light1", {"brightness": 65}, correlation_id="corr-1"
        )
    )
    topic, payload, qos, retain = harness.client.published[0]
    body = json.loads(payload)
    assert topic == "kzhome/v1/home1/light1/set"
    assert body == {
        "protocol_version": "v1",
        "command_id": command_id,
        "correlation_id": "corr-1",
        "timestamp": body["timestamp"],
        "state": {"brightness": 65},
    }
    assert (qos, retain) == (1, False)


def test_state_message_routing_and_correlation() -> None:
    harness = GatewayHarness()
    harness.handle(
        "kzhome/v1/home1/light1/state",
        {
            "protocol_version": "v1",
            "timestamp": "2026-09-11T12:00:00Z",
            "correlation_id": "corr-state",
            "state": {"on": True},
        },
    )
    assert harness.states == [("home1", "light1", {"on": True}, "corr-state")]


def test_ack_is_normalized_event() -> None:
    harness = GatewayHarness()
    harness.handle(
        "kzhome/v1/home1/light1/ack",
        {
            "protocol_version": "v1",
            "command_id": "cmd-1",
            "correlation_id": "corr-1",
            "status": "rejected",
            "error_code": "device_busy",
            "message": "Busy",
        },
    )
    event = harness.events[-1]
    assert event.type == "device_ack_received"
    assert event.correlation_id == "corr-1"
    assert event.data["status"] == "rejected"
    assert event.data["error_code"] == "device_busy"


def test_status_online_offline_and_repeated_heartbeat() -> None:
    harness = GatewayHarness()
    base = {
        "protocol_version": "v1",
        "last_seen": "2026-09-11T12:00:00Z",
        "heartbeat_interval_seconds": 60,
    }
    harness.handle("kzhome/v1/home1/light1/status", {**base, "status": "online"})
    harness.handle("kzhome/v1/home1/light1/status", {**base, "status": "online"})
    harness.handle("kzhome/v1/home1/light1/status", {**base, "status": "offline"})
    assert [item[2] for item in harness.statuses] == [True, True, False]
    assert [event.data["status"] for event in harness.events] == ["online", "offline"]


def test_telemetry_is_separate_from_state() -> None:
    harness = GatewayHarness()
    harness.handle(
        "kzhome/v1/home1/sensor1/telemetry",
        {
            "protocol_version": "v1",
            "timestamp": "2026-09-11T12:00:00Z",
            "metrics": {"rssi": -60, "temperature": 22.5},
        },
    )
    assert harness.states == []
    assert harness.events[-1].type == "device_telemetry_received"
    assert harness.events[-1].data["metrics"]["temperature"] == 22.5


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        (
            "kzhome/v1/other/light1/state",
            {"timestamp": "2026-09-11T12:00:00Z", "state": {"on": True}},
        ),
        (
            "kzhome/v1/home1/other-device/state",
            {"timestamp": "2026-09-11T12:00:00Z", "state": {"on": True}},
        ),
        (
            "kzhome/v1/home1/light1/state",
            {
                "protocol_version": "v2",
                "timestamp": "2026-09-11T12:00:00Z",
                "state": {"on": True},
            },
        ),
    ],
)
def test_cross_house_wrong_device_and_version_are_rejected(
    topic: str, payload: dict
) -> None:
    with pytest.raises(ValueError):
        GatewayHarness().handle(topic, payload)


def test_payload_size_and_json_bounds_are_rejected() -> None:
    harness = GatewayHarness()
    with pytest.raises(ValueError, match="payload exceeds"):
        harness.handle(
            "kzhome/v1/home1/light1/state", b"x" * (MAX_CONTROL_PAYLOAD + 1)
        )
    too_deep: dict = {"value": "end"}
    for _ in range(9):
        too_deep = {"nested": too_deep}
    with pytest.raises(ValueError, match="nesting"):
        harness.handle("kzhome/v1/home1/light1/state", too_deep)


def test_gateway_clean_shutdown() -> None:
    async def scenario() -> tuple[int, bool]:
        harness = GatewayHarness()
        harness.gateway.start()
        for _ in range(20):
            if harness.client.connected:
                break
            await asyncio.sleep(0)
        await harness.gateway.stop()
        return harness.client.disconnect_calls, harness.gateway.connected

    disconnects, connected = asyncio.run(scenario())
    assert disconnects == 1
    assert connected is False


def test_reconnect_backoff_without_real_sleep() -> None:
    delays: list[float] = []
    client = FakeMQTTClient(connect_error=ConnectionError("no broker"))
    bus = EventBus()

    async def unused(*args):
        return False

    gateway: MQTTGateway

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 3:
            gateway._stopping = True

    gateway = MQTTGateway(
        client,
        bus,
        unused,
        unused,
        unused,
        sleep=sleep,
        initial_backoff=1,
        maximum_backoff=2,
    )
    asyncio.run(gateway.run())
    assert delays == [1, 2, 2]
    assert client.connect_calls == 3
