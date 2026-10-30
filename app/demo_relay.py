"""Persistent demo-only software device; no human JWT or database access."""

import asyncio
import json
import logging
import os
import signal
import ssl
import sys
from uuid import uuid4

import aiomqtt

from app.transports.mqtt_models import AckEnvelope, CommandEnvelope, utc_now
from app.transports.mqtt_topics import TopicKind
from simulator.mqtt_relay import Relay


async def handle(relay, topic, payload, retained, mode):
    if mode == "apply":
        await relay.handle(topic, payload, retained)
        return
    if topic != relay.topic(TopicKind.SET) or retained:
        raise ValueError("Unexpected command")
    command = CommandEnvelope.model_validate_json(payload)
    if mode == "fail":
        await relay.send(
            TopicKind.ACK,
            AckEnvelope(
                command_id=command.command_id,
                correlation_id=command.correlation_id,
                timestamp=utc_now(),
                status="failed",
            ),
        )
    elif mode != "timeout":
        raise ValueError("Unknown mode")
    # timeout intentionally emits no ACK/state; Core's real deadline decides outcome.


async def run(configuration):
    if configuration["mode"] not in ("apply", "fail", "timeout"):
        raise ValueError("Unknown mode")
    context = ssl.create_default_context(cafile="/run/mqtt/ca.crt")
    relay = Relay("e2e_house", "e2e_relay", None)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    async with aiomqtt.Client(
        hostname="mosquitto",
        port=8883,
        username="relay-simulator",
        password=configuration["password"],
        identifier="demo-virtual-relay",
        tls_context=context,
        will=aiomqtt.Will(
            relay.topic(TopicKind.STATUS),
            relay.status(False).model_dump_json(),
            qos=1,
            retain=True,
        ),
    ) as mqtt:
        relay.publish = mqtt.publish
        await mqtt.subscribe(relay.topic(TopicKind.SET), qos=1)
        # Volatile software state starts OFF; advertise actual software value explicitly.
        await relay.state(str(uuid4()))
        await relay.send(TopicKind.STATUS, relay.status(True))

        async def receive():
            async for message in mqtt.messages:
                await handle(
                    relay,
                    str(message.topic),
                    bytes(message.payload),
                    message.retain,
                    configuration["mode"],
                )

        async def heartbeat():
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), 30)
                except TimeoutError:
                    await relay.send(TopicKind.STATUS, relay.status(True))

        receiver = asyncio.create_task(receive())
        pulse = asyncio.create_task(heartbeat())
        waiter = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait(
                [receiver, pulse, waiter], return_when=asyncio.FIRST_COMPLETED
            )
            if receiver.done():
                receiver.result()
            if pulse.done():
                pulse.result()
        finally:
            await relay.send(TopicKind.STATUS, relay.status(False))
            for task in (receiver, pulse, waiter):
                task.cancel()
            await asyncio.gather(receiver, pulse, waiter, return_exceptions=True)


def main():
    logging.disable(logging.CRITICAL)
    try:
        if os.environ.get("KZHOME_DEMO_ENABLED") != "1":
            raise ValueError("Demo opt-in required")
        asyncio.run(run(json.load(sys.stdin)))
    except Exception:
        print("FAIL: virtual relay stopped; details withheld")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
