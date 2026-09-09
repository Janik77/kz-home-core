import json
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.models import Device, DeviceState

StateHandler = Callable[[str, DeviceState], Awaitable[None]]


class MQTTClient(Protocol):
    async def publish(self, topic: str, payload: str) -> None: ...


class NullMQTTClient:
    async def publish(self, topic: str, payload: str) -> None:
        pass


class MQTTTransport:
    """MQTT adapter; the core remains independent of any MQTT library."""

    def __init__(self, house_id: str, client: MQTTClient | None = None, state_handler: StateHandler | None = None) -> None:
        self.house_id = house_id
        self.client = client or NullMQTTClient()
        self.state_handler = state_handler

    def state_topic(self, device_id: str) -> str:
        return f"kzhome/{self.house_id}/{device_id}/state"

    def command_topic(self, device_id: str) -> str:
        return f"kzhome/{self.house_id}/{device_id}/set"

    async def publish_state(self, device: Device) -> None:
        await self.client.publish(self.state_topic(device.id), json.dumps(device.state))

    async def send_command(self, device_id: str, state: DeviceState) -> None:
        await self.client.publish(self.command_topic(device_id), json.dumps(state))

    async def handle_message(self, topic: str, payload: str) -> None:
        prefix = f"kzhome/{self.house_id}/"
        if self.state_handler is None or not topic.startswith(prefix) or not topic.endswith("/state"):
            return
        device_id = topic.split("/")[-2]
        state = json.loads(payload)
        if not isinstance(state, dict):
            raise ValueError("MQTT state payload must be a JSON object")
        await self.state_handler(device_id, state)
