"""Standalone TLS MQTT relay; never opens Core's database or calls its services."""

import asyncio
import json
import logging
import os
import signal
import ssl
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import aiomqtt

from app.transports.mqtt_models import (
    AckEnvelope,
    CommandEnvelope,
    MAX_CONTROL_PAYLOAD,
    StateEnvelope,
    StatusEnvelope,
    utc_now,
    validate_json_bounds,
)
from app.transports.mqtt_topics import TOPIC_POLICY, TopicKind, build_topic

logger = logging.getLogger(__name__)
COMMAND_WINDOW = timedelta(seconds=300)


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    ca_file: str
    house_id: str
    device_id: str
    client_id: str

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.environ.get("RELAY_" + name, "")
            if not value:
                raise ValueError("Missing RELAY_" + name)
            return value

        config = cls(
            required("MQTT_HOST"),
            int(os.environ.get("RELAY_MQTT_PORT", "8883")),
            required("MQTT_USERNAME"),
            required("MQTT_PASSWORD"),
            required("CA_FILE"),
            required("HOUSE_ID"),
            required("DEVICE_ID"),
            required("MQTT_CLIENT_ID"),
        )
        if config.username == "kzhome-core":
            raise ValueError("A separate device account is required")
        if not 1 <= config.port <= 65535:
            raise ValueError("Invalid broker port")
        build_topic(config.house_id, config.device_id, TopicKind.SET)
        return config


class Relay:
    def __init__(self, house_id, device_id, publish):
        self.house_id, self.device_id = house_id, device_id
        self.publish = publish
        self.on = False
        self.seen = {}

    def topic(self, kind):
        return build_topic(self.house_id, self.device_id, kind)

    async def send(self, kind, envelope):
        policy = TOPIC_POLICY[kind]
        await self.publish(
            self.topic(kind),
            envelope.model_dump_json(),
            qos=policy.qos,
            retain=policy.retain,
        )

    def status(self, online):
        now = utc_now()
        return StatusEnvelope(
            timestamp=now,
            last_seen=now,
            status="online" if online else "offline",
            heartbeat_interval_seconds=60,
        )

    async def state(self, correlation_id):
        await self.send(
            TopicKind.STATE,
            StateEnvelope(
                timestamp=utc_now(),
                correlation_id=correlation_id,
                state={"on": self.on},
            ),
        )

    async def handle(self, topic, payload, retained=False):
        if topic != self.topic(TopicKind.SET) or retained:
            raise ValueError("Unexpected or retained command")
        if len(payload) > MAX_CONTROL_PAYLOAD:
            raise ValueError("Oversized command")
        raw = json.loads(payload.decode("utf-8"))
        validate_json_bounds(raw)
        command = CommandEnvelope.model_validate(raw)
        if any(
            len(value.encode("utf-8")) > 128
            for value in (command.command_id, command.correlation_id)
        ):
            raise ValueError("Oversized identifier")
        now = utc_now()
        if command.timestamp.tzinfo is None:
            raise ValueError("Command timestamp must have a timezone")
        self.seen = {key: item for key, item in self.seen.items() if item[0] >= now}
        previous = self.seen.get(command.command_id)
        if previous:
            _, original, ack = previous
            if command != original:
                raise ValueError("Command ID reused with different content")
            await self.send(TopicKind.ACK, ack)
            return
        error = None
        if not now - COMMAND_WINDOW <= command.timestamp <= now + timedelta(seconds=30):
            error = "timeout"
        elif set(command.state) != {"on"}:
            error = "unsupported_capability"
        elif type(command.state["on"]) is not bool:
            error = "invalid_value"
        if len(self.seen) >= 4096:
            raise ValueError("Command cache capacity reached")
        if error is None:
            self.on = command.state["on"]
        ack = AckEnvelope(
            command_id=command.command_id,
            correlation_id=command.correlation_id,
            timestamp=now,
            status="rejected" if error else "applied",
            error_code=error,
        )
        self.seen[command.command_id] = (
            max(now, command.timestamp) + COMMAND_WINDOW,
            command,
            ack,
        )
        await self.send(TopicKind.ACK, ack)
        if error is None:
            await self.state(command.correlation_id)


async def run(config):
    context = ssl.create_default_context(cafile=config.ca_file)
    relay = Relay(config.house_id, config.device_id, None)
    will = aiomqtt.Will(
        relay.topic(TopicKind.STATUS),
        relay.status(False).model_dump_json(),
        qos=1,
        retain=True,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass  # Windows uses asyncio.run's KeyboardInterrupt cancellation.
    async with aiomqtt.Client(
        hostname=config.host,
        port=config.port,
        username=config.username,
        password=config.password,
        identifier=config.client_id,
        tls_context=context,
        will=will,
        keepalive=60,
    ) as client:
        relay.publish = client.publish
        await client.subscribe(relay.topic(TopicKind.SET), qos=1)
        await relay.send(TopicKind.STATUS, relay.status(True))
        await relay.state(str(uuid4()))

        async def receive():
            async for message in client.messages:
                try:
                    await relay.handle(
                        str(message.topic), bytes(message.payload), message.retain
                    )
                except (ValueError, RecursionError):
                    logger.warning("Rejected invalid relay command")

        async def heartbeat():
            while True:
                await asyncio.sleep(60)
                await relay.send(TopicKind.STATUS, relay.status(True))

        tasks = [
            asyncio.create_task(receive()),
            asyncio.create_task(heartbeat()),
            asyncio.create_task(stop.wait()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await relay.send(TopicKind.STATUS, relay.status(False))


def main():
    logging.basicConfig(level=logging.INFO)
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run(Config.from_env()))
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.error("Relay stopped: check configuration, broker access, and TLS trust")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
