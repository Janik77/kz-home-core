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

    async def connect(self) -> None:
        import aiomqtt

        self._client = aiomqtt.Client(**self._options)
        await self._client.__aenter__()

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.__aexit__(None, None, None)

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
        if self._client is None:
            raise ConnectionError("MQTT client is disconnected")
        async for message in self._client.messages:
            yield MQTTMessage(
                topic=str(message.topic),
                payload=bytes(message.payload),
                retained=message.retain,
            )


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
