import asyncio
import logging

import aiomqtt
import pytest

from app.events import EventBus
from app.transports.mqtt import MQTTGateway
from app.transports.mqtt_client import AiomqttClient
from app.transports.mqtt_topics import subscription_topics


def adapter():
    return AiomqttClient(
        host="broker.invalid",
        port=8883,
        username="core",
        password="test",
        tls_enabled=True,
        keepalive=60,
        client_id="test-core",
    )


class Session:
    def __init__(self, fail_entry=False, fail_subscription=False):
        self.fail_entry = fail_entry
        self.fail_subscription = fail_subscription
        self.subscriptions = []
        self.exits = 0
        self.broken = False
        self.drop = asyncio.Event()

    async def __aenter__(self):
        if self.fail_entry:
            raise aiomqtt.MqttError("unavailable")
        return self

    async def __aexit__(self, *args):
        self.exits += 1
        if self.broken:
            raise aiomqtt.MqttError("same broker failure on exit")

    async def subscribe(self, topic, qos):
        if self.fail_subscription:
            raise aiomqtt.MqttError("subscription unavailable")
        self.subscriptions.append((topic, qos))
        return (qos,)

    @property
    def messages(self):
        async def stream():
            await self.drop.wait()
            self.broken = True
            raise aiomqtt.MqttError("Disconnected during message iteration")
            yield  # pragma: no cover

        return stream()


def test_disconnect_reconnect_restores_subscriptions(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.transports.mqtt")
    async def scenario():
        sessions = []
        delays = []
        restored = asyncio.Event()

        def factory(**options):
            assert options["tls_context"].check_hostname
            session = Session()
            sessions.append(session)
            if len(sessions) == 1:
                session.drop.set()
            return session

        monkeypatch.setattr(aiomqtt, "Client", factory)
        bus = EventBus()

        async def event_handler(event):
            if event.type == "mqtt_connected" and len(sessions) == 2:
                restored.set()

        async def unused(*args):
            pass

        async def sleep(delay):
            delays.append(delay)

        bus.subscribe(event_handler)
        gateway = MQTTGateway(adapter(), bus, unused, unused, unused, sleep=sleep)
        gateway.start()
        await asyncio.wait_for(restored.wait(), 1)
        assert gateway.connected
        assert [s.subscriptions for s in sessions] == [list(subscription_topics())] * 2
        await gateway.stop()
        assert not gateway.connected
        assert [s.exits for s in sessions] == [1, 1]
        assert delays == [1]

    asyncio.run(scenario())
    assert "MQTT disconnect failed" not in caplog.text
    assert caplog.text.count("MQTT connected; inbound subscriptions restored") == 2


@pytest.mark.parametrize("failure", ["entry", "subscription", "stream"])
def test_repeated_failures_back_off_and_stop(monkeypatch, failure):
    async def scenario():
        sessions, delays = [], []
        waiting = asyncio.Event()

        def factory(**options):
            session = Session(failure == "entry", failure == "subscription")
            session.drop.set()
            sessions.append(session)
            return session

        monkeypatch.setattr(aiomqtt, "Client", factory)

        async def unused(*args):
            pass

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 4:
                waiting.set()
                await asyncio.Event().wait()

        gateway = MQTTGateway(
            adapter(),
            EventBus(),
            unused,
            unused,
            unused,
            sleep=sleep,
            maximum_backoff=4,
        )
        gateway.start()
        await asyncio.wait_for(waiting.wait(), 1)
        assert not gateway.connected
        await gateway.stop()
        assert delays == [1, 2, 4, 4]
        assert len(sessions) == 4
        assert all(s.exits == (0 if failure == "entry" else 1) for s in sessions)

    asyncio.run(scenario())


def test_unexpected_cleanup_failure_is_not_suppressed(monkeypatch):
    async def scenario():
        session = Session()
        monkeypatch.setattr(aiomqtt, "Client", lambda **options: session)
        client = adapter()
        await client.connect()
        session.broken = True
        with pytest.raises(aiomqtt.MqttError):
            await client.disconnect()
        await client.disconnect()  # Idempotent; failed cleanup is not retried.
        assert session.exits == 1

    asyncio.run(scenario())


def test_shutdown_during_connect_cleans_late_connection(monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowSession(Session):
            async def __aenter__(self):
                entered.set()
                await release.wait()
                return self

        session = SlowSession()
        monkeypatch.setattr(aiomqtt, "Client", lambda **options: session)
        client = adapter()
        connecting = asyncio.create_task(client.connect())
        await entered.wait()
        connecting.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        await client.disconnect()
        assert session.exits == 1
        assert client._client is None

    asyncio.run(scenario())


@pytest.mark.parametrize("result", [(0,), (1,), (2,), "v5_success"])
def test_suback_granted_codes(monkeypatch, result):
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.reasoncodes import ReasonCode

    async def scenario():
        class SubscriptionSession(Session):
            async def subscribe(self, topic, qos):
                return ([ReasonCode(PacketTypes.SUBACK, identifier=1)]
                        if result == "v5_success" else result)

        monkeypatch.setattr(aiomqtt, "Client", lambda **options: SubscriptionSession())
        client = adapter()
        await client.connect()
        await client.subscribe("kzhome/v1/+/+/ack", 1)
        await client.disconnect()

    asyncio.run(scenario())


@pytest.mark.parametrize("result", [(128,), (), (0, 1), "v5_denied"])
@pytest.mark.parametrize("control_only", [False, True])
def test_denied_suback_does_not_mark_subscription_ready(monkeypatch, result, control_only):
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.reasoncodes import ReasonCode

    async def scenario():
        reached = asyncio.Event()
        sessions, events, delays = [], [], []

        class SubscriptionSession(Session):
            async def subscribe(self, topic, qos):
                self.subscriptions.append((topic, qos))
                if not control_only or topic.endswith("/control/request"):
                    return ([ReasonCode(PacketTypes.SUBACK, identifier=135)]
                            if result == "v5_denied" else result)
                return (qos,)

        def factory(**options):
            session = SubscriptionSession()
            sessions.append(session)
            return session

        monkeypatch.setattr(aiomqtt, "Client", factory)

        async def unused(*args):
            pass

        async def collect(event):
            events.append(event.type)
            if event.type == "mqtt_connected":
                reached.set()

        async def sleep(delay):
            delays.append(delay)
            reached.set()
            await asyncio.Event().wait()

        bus = EventBus()
        bus.subscribe(collect)
        gateway = MQTTGateway(adapter(), bus, unused, unused, unused, sleep=sleep)
        gateway.start()
        try:
            await asyncio.wait_for(reached.wait(), 1)
            assert not gateway.control_subscribed
            assert gateway.connected is control_only
            assert ("mqtt_connected" in events) is control_only
            assert delays == ([] if control_only else [1])
            assert sessions[0].exits == (0 if control_only else 1)
        finally:
            await gateway.stop()
        assert sessions[0].exits == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("grant", [1, 128])
def test_real_aiomqtt_suback_over_loopback(grant):
    """Real aiomqtt/Paho socket path; peer is a minimal MQTT fixture, not a broker."""
    async def scenario():
        packets = []
        done = asyncio.get_running_loop().create_future()

        async def packet(reader):
            header = (await reader.readexactly(1))[0]
            length, multiplier = 0, 1
            for _ in range(4):
                digit = (await reader.readexactly(1))[0]
                length += (digit & 127) * multiplier
                if not digit & 128:
                    return header, await reader.readexactly(length)
                multiplier *= 128
            raise AssertionError("Invalid MQTT remaining length")

        async def peer(reader, writer):
            try:
                header, payload = await packet(reader)
                assert header == 0x10  # CONNECT
                assert payload[:7] == b"\x00\x04MQTT\x04"  # MQTT 3.1.1
                writer.write(b"\x20\x02\x00\x00")
                await writer.drain()
                header, payload = await packet(reader)
                assert header == 0x82  # SUBSCRIBE
                packets.append(payload[2:])
                writer.write(b"\x90\x03" + payload[:2] + bytes([grant]))
                await writer.drain()
                header, _ = await packet(reader)
                assert header == 0xE0  # DISCONNECT
                done.set_result(None)
            except Exception as error:
                done.set_exception(error)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(peer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = AiomqttClient(host="127.0.0.1", port=port, username=None,
                               password=None, tls_enabled=False, keepalive=60,
                               client_id="suback-regression")
        async with server:
            try:
                await client.connect()
                if grant == 128:
                    with pytest.raises(ConnectionError, match="not granted"):
                        await client.subscribe("fixture/ack", 1)
                else:
                    await client.subscribe("fixture/ack", 1)
            finally:
                await client.disconnect()
            await asyncio.wait_for(done, 2)
        assert packets == [b"\x00\x0bfixture/ack\x01"]

    asyncio.run(asyncio.wait_for(scenario(), 5))
