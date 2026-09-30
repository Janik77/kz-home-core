import asyncio
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class MQTTMessage:
    topic: str
    payload: bytes
    retained: bool = False


class MQTTClient(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def subscribe(self, topic: str, qos: int) -> None: ...

    async def publish(
        self, topic: str, payload: str, *, qos: int, retain: bool
    ) -> None: ...

    def messages(self) -> AsyncIterator[MQTTMessage]: ...


class AiomqttClient:
    """Small adapter that prevents aiomqtt objects leaking into Core."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        tls_enabled: bool,
        keepalive: int,
        client_id: str,
    ) -> None:
        self._options = {
            "hostname": host,
            "port": port,
            "username": username,
            "password": password,
            "identifier": client_id,
            "keepalive": keepalive,
            "tls_context": ssl.create_default_context() if tls_enabled else None,
        }
        self._client: Any | None = None
        self._stream_failed = False

    async def connect(self) -> None:
        import aiomqtt

        if self._client is not None:
            raise RuntimeError("MQTT client is already connected")
        client = aiomqtt.Client(**self._options)
        # A failed __aenter__ must not be followed by __aexit__ as though it
        # owned an established connection. aiomqtt handles failed entry itself.
        # aiomqtt connects through an executor thread. Cancelling its await does
        # not stop that thread; settle entry before releasing a late connection.
        entry = asyncio.create_task(client.__aenter__())
        try:
            await asyncio.shield(entry)
        except asyncio.CancelledError:
            try:
                await entry
            except Exception:
                pass  # Preserve shutdown cancellation, not a late connect error.
            else:
                try:
                    await client.__aexit__(None, None, None)
                except aiomqtt.MqttError:
                    pass  # Connection may have disappeared during shutdown.
            raise
        self._client = client
        self._stream_failed = False

    async def disconnect(self) -> None:
        import aiomqtt

        client, self._client = self._client, None
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except aiomqtt.MqttError:
                # aiomqtt re-raises the broker disconnect during context exit.
                # The gateway already received this failure from messages().
                if not self._stream_failed:
                    raise
            finally:
                self._stream_failed = False

    async def subscribe(self, topic: str, qos: int) -> None:
        if self._client is None:
            raise ConnectionError("MQTT client is disconnected")
        await self._client.subscribe(topic, qos=qos)

    async def publish(
        self, topic: str, payload: str, *, qos: int, retain: bool
    ) -> None:
        if self._client is None:
            raise ConnectionError("MQTT client is disconnected")
        await self._client.publish(topic, payload, qos=qos, retain=retain)

    async def messages(self) -> AsyncIterator[MQTTMessage]:
        import aiomqtt

        if self._client is None:
            raise ConnectionError("MQTT client is disconnected")
        client = self._client
        try:
            async for message in client.messages:
                yield MQTTMessage(
                    topic=str(message.topic),
                    payload=bytes(message.payload),
                    retained=message.retain,
                )
        except aiomqtt.MqttError:
            self._stream_failed = True
            raise


class FakeMQTTClient:
    """In-memory client used by tests; it never opens a network connection."""

    def __init__(self, *, connect_error: Exception | None = None) -> None:
        self.connect_error = connect_error
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connected = False
        self.subscriptions: list[tuple[str, int]] = []
        self.published: list[tuple[str, str, int, bool]] = []
        self._messages: asyncio.Queue[MQTTMessage | None] = asyncio.Queue()

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        was_connected = self.connected
        self.connected = False
        if was_connected:
            await self._messages.put(None)

    async def subscribe(self, topic: str, qos: int) -> None:
        self.subscriptions.append((topic, qos))

    async def publish(
        self, topic: str, payload: str, *, qos: int, retain: bool
    ) -> None:
        self.published.append((topic, payload, qos, retain))

    async def inject(
        self, topic: str, payload: str | bytes, *, retained: bool = False
    ) -> None:
        encoded = payload.encode() if isinstance(payload, str) else payload
        await self._messages.put(MQTTMessage(topic, encoded, retained))

    async def messages(self) -> AsyncIterator[MQTTMessage]:
        while True:
            message = await self._messages.get()
            if message is None:
                return
            yield message
