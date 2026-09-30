"""Standalone, one-shot Protocol v1 motion probe; MQTT only, no Core services."""

import argparse
import asyncio
import logging
import os
import ssl
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

import aiomqtt

from app.transports.mqtt_models import StateEnvelope, StatusEnvelope, utc_now
from app.transports.mqtt_topics import TOPIC_POLICY, TopicKind, build_topic


@dataclass(frozen=True)
class MotionConfig:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    ca_file: str
    client_id: str

    @classmethod
    def from_env(cls):
        def required(key):
            value = os.environ.get("MOTION_" + key, "")
            if not value:
                raise ValueError("Missing motion configuration")
            return value

        config = cls(
            required("MQTT_HOST"),
            int(os.environ.get("MOTION_MQTT_PORT", "8883")),
            required("MQTT_USERNAME"),
            required("MQTT_PASSWORD"),
            required("CA_FILE"),
            required("MQTT_CLIENT_ID"),
        )
        if config.username != "motion-simulator" or not 1 <= config.port <= 65535:
            raise ValueError("Dedicated motion identity and valid port required")
        return config


class Motion:
    def __init__(self, publish):
        self.publish = publish

    @staticmethod
    def topic(kind):
        if kind not in (TopicKind.STATE, TopicKind.STATUS):
            raise ValueError("Motion probe only publishes its own state/status")
        return build_topic("e2e_house", "e2e_motion", kind)

    @staticmethod
    def status(online):
        now = utc_now()
        return StatusEnvelope(
            timestamp=now, last_seen=now, status="online" if online else "offline"
        )

    async def send_status(self, status):
        await self._send(TopicKind.STATUS, status)

    async def state(self, motion, correlation_id):
        if type(motion) is not bool or not correlation_id:
            raise ValueError("Boolean motion and correlation ID required")
        await self._send(
            TopicKind.STATE,
            StateEnvelope(
                timestamp=utc_now(),
                correlation_id=correlation_id,
                state={"motion": motion},
            ),
        )

    async def _send(self, kind, envelope):
        policy = TOPIC_POLICY[kind]
        await self.publish(
            self.topic(kind),
            envelope.model_dump_json(),
            qos=policy.qos,
            retain=policy.retain,
        )


@asynccontextmanager
async def connection(config):
    context = ssl.create_default_context(cafile=config.ca_file)
    motion = Motion(None)
    async with aiomqtt.Client(
        hostname=config.host,
        port=config.port,
        username=config.username,
        password=config.password,
        identifier=config.client_id,
        tls_context=context,
        timeout=5,
        will=aiomqtt.Will(
            motion.topic(TopicKind.STATUS),
            motion.status(False).model_dump_json(),
            qos=1,
            retain=True,
        ),
    ) as client:
        motion.publish = client.publish
        try:
            yield motion
        finally:
            async with asyncio.timeout(5):
                await motion.send_status(motion.status(False))


async def probe(config, value):
    async with asyncio.timeout(15):
        async with connection(config) as motion:
            await motion.send_status(motion.status(True))
            await motion.state(value, str(uuid4()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--motion", choices=("false", "true"), required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error("explicit --run is required")
    logging.disable(logging.CRITICAL)
    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(probe(MotionConfig.from_env(), args.motion == "true"))
    except (Exception, KeyboardInterrupt):
        print("FAIL: motion probe configuration, TLS/MQTT, timeout or interruption")
        raise SystemExit(1) from None
    print("Motion published; Core persistence/automation not checked by this probe")


if __name__ == "__main__":
    main()
