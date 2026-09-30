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
