import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4
from time import monotonic

from pydantic import ValidationError

from app.events import Event, EventBus
from app.core.errors import ConflictError, InvalidReferenceError, UnavailableError
from app.services.control_authority import default_control_authority
from app.transports.control_models import decode_request
from app.services.command_clock import COMMAND_LIFETIME, command_clock
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
        command_ack_handler: Callable[[str, str, AckEnvelope, bool], Awaitable[None]]
        | None = None,
        command_state_handler: Callable[
            [str, str, StateEnvelope, bool], Awaitable[None]
        ]
        | None = None,
        command_guard: IdentityHandler | None = None,
        control_authority=None,
        control_stage_handler=None,
        control_request_handler=None,
    ) -> None:
        self.client = client
        self.event_bus = event_bus
        self.state_handler = state_handler
        self.status_handler = status_handler
        self.identity_handler = identity_handler
        self.sleep = sleep
        self.initial_backoff = initial_backoff
        self.maximum_backoff = maximum_backoff
        self.command_ack_handler = command_ack_handler
        self.command_state_handler = command_state_handler
        self.command_guard = command_guard
        self.control_authority = control_authority or default_control_authority
        self.control_stage_handler = control_stage_handler
        self.control_request_handler = control_request_handler
        self._control_devices = {}
        self._control_second = -1
        self._control_count = 0
        self.control_subscribed = False
        if not 0 < initial_backoff <= maximum_backoff:
            raise ValueError("MQTT backoff must be positive and bounded")
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
                self.control_subscribed = False
                for topic, qos in subscription_topics():
                    try:
                        await self.client.subscribe(topic, qos)
                    except Exception:
                        if topic.endswith("/control/request"):
                            logger.warning("Control subscription unavailable; physical control stays closed")
                            continue
                        raise
                    if topic.endswith("/control/request"):
                        self.control_subscribed = True
                self.connected = True
                logger.info("MQTT connected; inbound subscriptions restored")
                await self.event_bus.publish(Event(type="mqtt_connected", data={}))
                async for message in self.client.messages():
                    backoff = self.initial_backoff
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
        self.control_subscribed = False
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
        command_id: str = "",
        timestamp: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        await self.authorize_command(house_id, device_id)
        if (
            timestamp is None or expires_at is None
            or timestamp.tzinfo is None or expires_at.tzinfo is None
            or timestamp.utcoffset() != timedelta(0)
            or expires_at.utcoffset() != timedelta(0)
            or not timedelta(0) < expires_at - timestamp <= COMMAND_LIFETIME
        ):
            raise InvalidReferenceError("A valid original command deadline is required")
        correlation_id = correlation_id or str(uuid4())
        command_id = command_id or str(uuid4())
        envelope = CommandEnvelope(
            protocol_version=PROTOCOL_VERSION,
            command_id=command_id,
            correlation_id=correlation_id,
            timestamp=timestamp,
            expires_at=expires_at,
            state=state,
        )
        policy = TOPIC_POLICY[TopicKind.SET]
        now = command_clock.now()
        if not timestamp <= now < expires_at:
            raise ConflictError("Command is outside its original validity interval")
        await self.client.publish(
            build_topic(house_id, device_id, TopicKind.SET),
            envelope.model_dump_json(),
            qos=policy.qos,
            retain=policy.retain,
        )
        return command_id

    async def authorize_command(self, house_id: str, device_id: str) -> None:
        if self.command_guard is None:
            raise ConflictError("MQTT command destination is not approved")
        await self.command_guard(house_id, device_id)

    async def send_control_command(self, house_id: str, device_id: str, command_id: str) -> None:
        if self.control_stage_handler is None:
            raise ConflictError("Physical control authority is unavailable")
        delivery = await self.control_stage_handler(house_id, device_id, command_id)
        if not self.connected or not self.control_subscribed:
            raise UnavailableError("Control transport subscription is unavailable")
        self.control_authority.validate_delivery(delivery)
        await self.client.publish(build_topic(house_id, device_id, TopicKind.CONTROL_COMMAND),
                                  delivery.payload, qos=1, retain=False)

    async def _control_request(self, parsed, message):
        if message.retained:
            raise ValueError("Retained control requests are forbidden")
        request = decode_request(message.payload)
        # Transport-level limiter protects DB even when timing is unqualified.
        tick = monotonic()
        self._control_devices = {key: value for key, value in self._control_devices.items()
                                 if tick - value < 1}
        second = int(tick)
        if second != self._control_second:
            self._control_second, self._control_count = second, 0
        key = parsed.house_id, parsed.device_id
        if key in self._control_devices or len(self._control_devices) >= 4096 or self._control_count >= 100:
            return
        self._control_devices[key] = tick
        self._control_count += 1
        await self.identity_handler(*key)
        if self.control_request_handler is None:
            raise ConflictError("Physical control authority is unavailable")
        try:
            delivery = await self.control_request_handler(*key, request)
            if not self.connected or not self.control_subscribed:
                raise UnavailableError("Control transport subscription is unavailable")
            self.control_authority.validate_delivery(delivery)
            payload = delivery.payload
        except (ConflictError, UnavailableError):
            payload = json.dumps({**request.model_dump(), "type": "permit_denied",
                                  "reason": "unavailable"}, separators=(",", ":"))
        await self.client.publish(build_topic(*key, TopicKind.CONTROL_REPLY),
                                  payload, qos=1, retain=False)

    async def handle_message(self, message: MQTTMessage) -> None:
        parsed = parse_topic(message.topic)
        if parsed.kind in (TopicKind.SET, TopicKind.CONTROL_COMMAND, TopicKind.CONTROL_REPLY):
            raise ValueError("Core does not consume command/reply topics")
        if parsed.kind is TopicKind.CONTROL_REQUEST:
            await self._control_request(parsed, message)
            return
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
            await self._route(
                parsed.house_id, parsed.device_id, parsed.kind, raw, message.retained
            )
        except ValidationError as error:
            raise ValueError("Invalid MQTT payload envelope") from error

    async def _route(
        self,
        house_id: str,
        device_id: str,
        kind: TopicKind,
        raw: dict[str, Any],
        retained: bool = False,
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
            if self.command_state_handler is not None:
                await self.command_state_handler(
                    house_id, device_id, envelope, retained
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
            if self.command_ack_handler is not None:
                await self.command_ack_handler(house_id, device_id, envelope, retained)
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
