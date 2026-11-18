"""Bounded TLS MQTT numeric software sensor; never controls relay hardware.

Reuses the existing motion-simulator identity/ACL for e2e_motion. No credentials,
permissions or profiles are created/changed by this probe.
"""

import argparse
import asyncio
import logging
import os
from datetime import timedelta
from uuid import uuid4
from simulator.mqtt_motion import Motion, MotionConfig, connection
from app.services.numeric_state import NUMERIC_RANGES, valid_number
from app.transports.mqtt_models import StateEnvelope, utc_now
from app.transports.mqtt_topics import TopicKind, build_topic


class NumericSensor(Motion):
    def __init__(self, publish, house, device):
        super().__init__(publish)
        if (house, device) != ("e2e_house", "e2e_motion"):
            raise ValueError("Explicit software sensor identity required")
        self.house, self.device = house, device

    def topic(self, kind):
        if kind not in (TopicKind.STATE, TopicKind.STATUS):
            raise ValueError("Sensor publishes only its own state/status")
        return build_topic(self.house, self.device, kind)

    async def reading(self, field, value, *, stale=False, duplicate=False):
        if not valid_number(field, value):
            raise ValueError("Invalid numeric sample")
        envelope = StateEnvelope(
            timestamp=utc_now() - timedelta(seconds=180 if stale else 0),
            correlation_id=str(uuid4()),
            state={field: value},
        )
        # Retained snapshots cannot trigger rules. Sensor samples are always live.
        for _ in range(2 if duplicate else 1):
            await self.publish(
                self.topic(TopicKind.STATE),
                envelope.model_dump_json(),
                qos=1,
                retain=False,
            )


async def probe(config, args):
    sensor = NumericSensor(None, args.house, args.device)
    async with asyncio.timeout(120):
        async with connection(config, sensor):
            await sensor.send_status(sensor.status(True))
            for value in args.values:
                await sensor.reading(
                    args.field, value, stale=args.stale, duplicate=args.duplicate
                )
                await asyncio.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--house", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--field", choices=tuple(NUMERIC_RANGES), required=True)
    parser.add_argument("--values", nargs="+", type=float, required=True)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--duplicate", action="store_true")
    parser.add_argument("--stale", action="store_true")
    args = parser.parse_args()
    if not args.run or not 1 <= len(args.values) <= 60 or not 0.1 <= args.interval <= 2:
        parser.error("--run, 1..60 samples and 0.1..2 second interval required")
    if any(not valid_number(args.field, v) for v in args.values):
        parser.error("Values must be finite and within sensor range")
    logging.disable(logging.CRITICAL)
    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(probe(MotionConfig.from_env(), args))
    except (Exception, KeyboardInterrupt):
        print("FAIL: numeric probe configuration, TLS/MQTT, timeout or interruption")
        raise SystemExit(1) from None
    print("Software numeric samples published; inspect Core run receipts for success")


if __name__ == "__main__":
    main()
