import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.events import Event, EventBus
from app.schemas import DeviceState
from app.transports.mqtt_client import MQTTClient, MQTTMessage
from app.transports.mqtt_models import (
    AckEnvelope,
    CommandEnvelope,
    MAX_CONTROL_PAYLOAD,
    MAX_TELEMETRY_PAYLOAD,
    StateEnvelope,
    StatusEnvelope,
    TelemetryEnvelope,
    utc_now,
    validate_json_bounds,
)
from app.transports.mqtt_topics import (
    PROTOCOL_VERSION,
    TOPIC_POLICY,
    TopicKind,
    build_topic,
    parse_topic,
    subscription_topics,
)

logger = logging.getLogger(__name__)

StateHandler = Callable[[str, str, DeviceState, str], Awaitable[None]]
StatusHandler = Callable[[str, str, bool, datetime], Awaitable[bool]]
IdentityHandler = Callable[[str, str], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]


class MQTTGateway:
    """Lifecycle-managed Device Protocol v1 MQTT adapter."""

    def __init__(
        self,
        client: MQTTClient,
        event_bus: EventBus,
        state_handler: StateHandler,
        status_handler: StatusHandler,
        identity_handler: IdentityHandler,
        *,
        sleep: Sleep = asyncio.sleep,
        initial_backoff: float = 1.0,
        maximum_backoff: float = 30.0,
    ) -> None:
        self.client = client
        self.event_bus = event_bus
        self.state_handler = state_handler
        self.status_handler = status_handler
        self.identity_handler = identity_handler
        self.sleep = sleep
        self.initial_backoff = initial_backoff
        self.maximum_backoff = maximum_backoff
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.connected = False

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self.run(), name="kzhome-mqtt-gateway")

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.connected:
            await self._disconnect()

    async def run(self) -> None:
        backoff = self.initial_backoff
        while not self._stopping:
            try:
                await self.client.connect()
                self.connected = True
                backoff = self.initial_backoff
                for topic, qos in subscription_topics():
                    await self.client.subscribe(topic, qos)
                await self.event_bus.publish(Event(type="mqtt_connected", data={}))
                async for message in self.client.messages():
                    try:
                        await self.handle_message(message)
                    except Exception as error:
                        logger.warning(
                            "Rejected MQTT message on %s: %s",
                            message.topic,
                            type(error).__name__,
                        )
                if not self._stopping:
                    raise ConnectionError("MQTT message stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("MQTT connection unavailable: %s", type(error).__name__)
            finally:
                await self._disconnect()
            if not self._stopping:
                await self.sleep(backoff)
                backoff = min(backoff * 2, self.maximum_backoff)

    async def _disconnect(self) -> None:
        was_connected = self.connected
        self.connected = False
        try:
            await self.client.disconnect()
        except Exception as error:
            logger.error("MQTT disconnect failed: %s", type(error).__name__)
        if was_connected:
            await self.event_bus.publish(Event(type="mqtt_disconnected", data={}))

    async def send_command(
        self,
        house_id: str,
        device_id: str,
        state: DeviceState,
        *,
        correlation_id: str = "",
    ) -> str:
        correlation_id = correlation_id or str(uuid4())
        command_id = str(uuid4())
        envelope = CommandEnvelope(
            protocol_version=PROTOCOL_VERSION,
            command_id=command_id,
            correlation_id=correlation_id,
            timestamp=utc_now(),
            state=state,
        )
        policy = TOPIC_POLICY[TopicKind.SET]
        await self.client.publish(
            build_topic(house_id, device_id, TopicKind.SET),
            envelope.model_dump_json(),
            qos=policy.qos,
            retain=policy.retain,
        )
        return command_id

    async def handle_message(self, message: MQTTMessage) -> None:
        parsed = parse_topic(message.topic)
        if parsed.kind is TopicKind.SET:
            raise ValueError("Core does not consume set topics")
        limit = (
            MAX_TELEMETRY_PAYLOAD
            if parsed.kind is TopicKind.TELEMETRY
            else MAX_CONTROL_PAYLOAD
        )
        if len(message.payload) > limit:
            raise ValueError("MQTT payload exceeds protocol limit")
        try:
            raw = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("MQTT payload must be a UTF-8 JSON object") from error
        if not isinstance(raw, dict):
            raise ValueError("MQTT payload must be a JSON object")
        validate_json_bounds(raw)
        if raw.get("protocol_version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            raise ValueError("Unsupported MQTT protocol version")
        try:
            await self._route(parsed.house_id, parsed.device_id, parsed.kind, raw)
        except ValidationError as error:
            raise ValueError("Invalid MQTT payload envelope") from error

    async def _route(
        self, house_id: str, device_id: str, kind: TopicKind, raw: dict[str, Any]
    ) -> None:
        await self.identity_handler(house_id, device_id)
        if kind is TopicKind.STATE:
            envelope = StateEnvelope.model_validate(raw)
            await self.state_handler(
                house_id,
                device_id,
                envelope.state,
                envelope.correlation_id,
            )
            return
        if kind is TopicKind.STATUS:
            envelope = StatusEnvelope.model_validate(raw)
            changed = await self.status_handler(
                house_id,
                device_id,
                envelope.status == "online",
                envelope.last_seen,
            )
            if changed:
                await self.event_bus.publish(
                    Event(
                        type="device_status_changed",
                        data={
                            "house_id": house_id,
                            "device_id": device_id,
                            "status": envelope.status,
                            "last_seen": envelope.last_seen.isoformat(),
                        },
                    )
                )
            return
        if kind is TopicKind.ACK:
            envelope = AckEnvelope.model_validate(raw)
            await self.event_bus.publish(
                Event(
                    type="device_ack_received",
                    data={
                        "house_id": house_id,
                        "device_id": device_id,
                        **envelope.model_dump(
                            mode="json", exclude={"protocol_version", "correlation_id"}
                        ),
                    },
                    correlation_id=envelope.correlation_id,
                )
            )
            return
        envelope = TelemetryEnvelope.model_validate(raw)
        await self.event_bus.publish(
            Event(
                type="device_telemetry_received",
                data={
                    "house_id": house_id,
                    "device_id": device_id,
                    "timestamp": envelope.timestamp.isoformat(),
                    "metrics": envelope.metrics,
                },
                correlation_id=envelope.correlation_id,
            )
        )


# Compatibility name for callers of the v0.4 placeholder.
MQTTTransport = MQTTGateway
