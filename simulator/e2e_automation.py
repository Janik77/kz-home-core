"""Opt-in MQTT motion -> existing AutomationService -> MQTT relay acceptance."""

import argparse
import asyncio
import getpass
import json
import logging
import os
import ssl
import warnings
from uuid import uuid4

import aiomqtt

from app.schemas import AutomationCreate, DeviceCreate
from app.transports.mqtt_models import CommandEnvelope
from app.transports.mqtt_topics import TopicKind
from simulator.e2e_acceptance import API, DEVICE, EVENTS, Failure, evidence, poll
from simulator.mqtt_motion import MotionConfig, connection
from simulator.mqtt_relay import Config, Relay

MOTION = "/devices/e2e_motion"
RULE_ID = "e2e_motion_relay"
RULE = "/automations/" + RULE_ID


def motion_definition():
    return DeviceCreate(
        id="e2e_motion",
        name="E2E motion",
        room_id="e2e_room",
        type="motion_sensor",
        capabilities=["motion"],
        online=False,
    )


def rule_definition():
    return AutomationCreate(
        id=RULE_ID,
        name="E2E motion turns relay on",
        house_id="e2e_house",
        enabled=False,
        trigger={
            "type": "device_state",
            "device_id": "e2e_motion",
            "field": "motion",
            "operator": "eq",
            "value": True,
        },
        conditions=[],
        actions=[
            {"type": "device_state", "device_id": "e2e_relay", "state": {"on": True}}
        ],
    )


async def provision(api):
    """Use RBAC-protected APIs; never overwrite conflicting records or observed state."""
    relay = await api.request("GET", DEVICE)
    room = await api.request("GET", "/rooms/e2e_room")
    floor = await api.request("GET", "/floors/" + room["floor_id"])
    if (
        floor["house_id"] != "e2e_house"
        or relay["room_id"] != "e2e_room"
        or relay["type"] != "relay"
        or relay["capabilities"] != ["on_off"]
    ):
        raise Failure("Existing E2E room/relay is incompatible")
    devices = await api.request("GET", "/devices?house_id=e2e_house")
    sensor = next((d for d in devices if d["id"] == "e2e_motion"), None)
    if sensor and any(
        sensor[key] != motion_definition().model_dump()[key]
        for key in ("room_id", "type", "capabilities")
    ):
        raise Failure("Existing motion device is incompatible")
    rules = await api.request("GET", "/automations")
    rule = next((r for r in rules if r["id"] == RULE_ID), None)
    if rule and any(
        json.dumps(rule[key], sort_keys=True)
        != json.dumps(rule_definition().model_dump()[key], sort_keys=True)
        for key in ("house_id", "trigger", "conditions", "actions")
    ):
        raise Failure("Existing E2E automation is incompatible")
    for other in rules:
        if other["id"] == RULE_ID or not other["enabled"]:
            continue
        if other["trigger"]["device_id"] in {"e2e_motion", "e2e_relay"} or any(
            a.get("device_id") in {"e2e_motion", "e2e_relay"} for a in other["actions"]
        ):
            raise Failure("Another enabled automation uses the E2E devices")
    if sensor is None:
        await api.request("POST", "/devices", motion_definition().model_dump())
    if rule is None:
        await api.request("POST", "/automations", rule_definition().model_dump())
    await api.request("POST", RULE + "/disable")


def automation_evidence(events, correlation, before):
    fresh = [
        e
        for e in events
        if e["id"] not in before
        and e["house_id"] == "e2e_house"
        and e["correlation_id"] == correlation
    ]
    statuses = {e["event_type"] for e in fresh if e["entity_id"] == RULE_ID}
    if "automation_failed" in statuses:
        raise Failure("Core persisted a correlated automation failure")
    return {
        "motion_event": any(
            e["entity_id"] == "e2e_motion"
            and e["event_type"] == "device_state_changed"
            and e["payload"].get("state", {}).get("motion") is True
            for e in fresh
        ),
        "triggered": "automation_triggered" in statuses,
        "completed": "automation_completed" in statuses,
    }


async def scenario(api, client, relay, motion, timeout):
    phase = "MQTT false baselines"
    checks = {}
    try:
        async with asyncio.timeout(timeout):
            await client.subscribe(relay.topic(TopicKind.SET), qos=1)
            await motion.state(False, str(uuid4()))
            motion_status = motion.status(True)
            await motion.send_status(motion_status)
            relay.on = False
            await relay.state(str(uuid4()))
            relay_status = relay.status(True)
            await relay.send(TopicKind.STATUS, relay_status)

            async def baseline():
                nonlocal checks
                sensor = await api.request("GET", MOTION)
                device = await api.request("GET", DEVICE)
                checks = {
                    "motion_false": sensor["state"].get("motion") is False,
                    "relay_false": device["state"].get("on") is False,
                    "motion_fresh": sensor["online"] is True
                    and sensor["metadata"].get("last_seen")
                    == motion_status.last_seen.isoformat(),
                    "relay_fresh": device["online"] is True
                    and device["metadata"].get("last_seen")
                    == relay_status.last_seen.isoformat(),
                }
                return all(checks.values())

            await poll(baseline)
        phase = "enable automation and publish fresh MQTT motion"
        checks = {}
        async with asyncio.timeout(timeout):
            await api.request("POST", RULE + "/enable")
            before = {e["id"] for e in await api.request("GET", EVENTS)}
            correlation = str(uuid4())
            await motion.state(True, correlation)
            phase = "correlated automation MQTT relay command"
            message = await client.messages.__anext__()
            command = CommandEnvelope.model_validate_json(bytes(message.payload))
            if (
                command.correlation_id != correlation
                or command.state != {"on": True}
                or command.state.get("on") is not True
            ):
                raise Failure("Unexpected or uncorrelated relay command")
            await relay.handle(
                str(message.topic), bytes(message.payload), message.retain
            )
            _, original, ack = relay.seen[command.command_id]
            if original != command or relay.on is not True or ack.status != "applied":
                raise Failure("Relay did not apply the automation command")
            phase = "persisted motion/automation/relay ACK/state and GETs"

            async def confirmed():
                nonlocal checks
                events = await api.request("GET", EVENTS)
                checks = automation_evidence(events, correlation, before)
                checks["ack"], checks["relay_event"] = evidence(
                    events, command, True, before
                )
                sensor = await api.request("GET", MOTION)
                device = await api.request("GET", DEVICE)
                checks["motion_GET"] = sensor["state"].get("motion") is True
                checks["relay_GET"] = device["state"].get("on") is True
                return all(checks.values())

            await poll(confirmed)
    except TimeoutError:
        raise Failure("Timeout during " + phase + "; checks=" + str(checks)) from None
    except Failure as error:
        raise Failure(phase + ": " + str(error)) from None
    except Exception:
        raise Failure(phase + ": MQTT or response validation failed") from None


async def run(relay_config, motion_config, api, email, password, timeout):
    async with asyncio.timeout(timeout):
        tokens = await api.request(
            "POST", "/auth/login", {"email": email, "password": password}
        )
        api.token = tokens["access_token"]
        await provision(api)
    relay = Relay("e2e_house", "e2e_relay", None)
    try:
        async with aiomqtt.Client(
            hostname=relay_config.host,
            port=relay_config.port,
            username=relay_config.username,
            password=relay_config.password,
            identifier=relay_config.client_id,
            tls_context=ssl.create_default_context(cafile=relay_config.ca_file),
            timeout=5,
            will=aiomqtt.Will(
                relay.topic(TopicKind.STATUS),
                relay.status(False).model_dump_json(),
                qos=1,
                retain=True,
            ),
        ) as client:
            relay.publish = client.publish
            try:
                async with connection(motion_config) as motion:
                    await scenario(api, client, relay, motion, timeout)
            finally:
                async with asyncio.timeout(5):
                    await relay.send(TopicKind.STATUS, relay.status(False))
    finally:
        # Leave the dedicated rule disabled even after a failed scenario.
        try:
            async with asyncio.timeout(6):
                await api.request("POST", RULE + "/disable")
        except Exception:
            raise Failure(
                "Cleanup could not confirm the E2E rule is disabled; inspect it before rerunning"
            ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--timeout", type=int, choices=range(5, 61), default=30, metavar="5..60"
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("explicit --run is required")
    logging.disable(logging.CRITICAL)
    try:
        relay, motion = Config.from_env(), MotionConfig.from_env()
        if (relay.house_id, relay.device_id, relay.username) != (
            "e2e_house",
            "e2e_relay",
            "relay-simulator",
        ) or relay.client_id == motion.client_id:
            raise Failure(
                "Dedicated relay identity and distinct MQTT client IDs required"
            )
        email = input("E2E user email: ").strip()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("E2E user password: ")
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(
            run(relay, motion, API("http://core:8000"), email, password, args.timeout)
        )
    except Failure as error:
        print("FAIL: " + str(error))
        raise SystemExit(1) from None
    except (Exception, KeyboardInterrupt):
        print(
            "FAIL: provisioning/login, TLS/MQTT, cleanup, timeout or interruption; check prerequisites"
        )
        raise SystemExit(1) from None
    print(
        "PASS: MQTT motion -> AutomationService -> MQTT relay -> correlated ACK/state -> persisted true; rule disabled"
    )


if __name__ == "__main__":
    main()
