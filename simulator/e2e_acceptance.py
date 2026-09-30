"""Explicit broker acceptance runner; never collected or started by pytest."""

import argparse
import asyncio
import getpass
import json
import logging
import os
import ssl
import urllib.error
import urllib.request
import warnings
from uuid import uuid4

import aiomqtt

from app.transports.mqtt_models import CommandEnvelope
from app.transports.mqtt_topics import TopicKind
from simulator.mqtt_relay import Config, Relay

DEVICE = "/devices/e2e_relay"
EVENTS = "/events?house_id=e2e_house&limit=500"


class Failure(Exception):
    """Only fixed, secret-free diagnostics may be passed to this exception."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base):
        self.base = base
        self.token = None
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    async def request(self, method, path, body=None):
        def send():
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            request = urllib.request.Request(
                self.base + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers=headers,
                method=method,
            )
            try:
                with self.opener.open(request, timeout=5) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                raise Failure(f"HTTP request failed (status {error.code})") from None
            except Exception:
                raise Failure("HTTP transport or JSON response failed") from None

        return await asyncio.to_thread(send)


def evidence(events, command, expected, before):
    fresh = [
        event
        for event in events
        if event["id"] not in before
        and event["house_id"] == "e2e_house"
        and event["entity_id"] == "e2e_relay"
        and event["correlation_id"] == command.correlation_id
    ]
    acks = [
        event
        for event in fresh
        if event["event_type"] == "device_ack_received"
        and event["payload"].get("command_id") == command.command_id
    ]
    if any(event["payload"].get("status") in {"rejected", "failed"} for event in acks):
        raise Failure("Core persisted a matching negative ACK")
    ack = any(event["payload"].get("status") == "applied" for event in acks)
    state = any(
        event["event_type"] == "device_state_changed"
        and event["payload"].get("state", {}).get("on") is expected
        for event in fresh
    )
    return ack, state


async def poll(check):
    while not await check():
        await asyncio.sleep(0.2)


async def scenario(api, client, relay, timeout):
    phase = "initial MQTT status/state and persisted off baseline"
    observed = ""
    try:
        async with asyncio.timeout(timeout):
            await client.subscribe(relay.topic(TopicKind.SET), qos=1)
            status = relay.status(True)
            await relay.send(TopicKind.STATUS, status)
            await relay.state(str(uuid4()))

            async def baseline():
                device = await api.request("GET", DEVICE)
                return (
                    device["state"].get("on") is False
                    and device["online"] is True
                    and device["metadata"].get("last_seen")
                    == status.last_seen.isoformat()
                )

            await poll(baseline)
        used_ids = set()
        used_correlations = set()
        for expected, action in ((True, "on"), (False, "off")):
            phase = action + ": snapshot event history"
            observed = ""
            async with asyncio.timeout(timeout):
                before = {event["id"] for event in await api.request("GET", EVENTS)}
                phase = action + ": HTTP command"
                await api.request("POST", DEVICE + "/" + action)
                phase = action + ": simulator MQTT receipt/application"
                # Messages are buffered by aiomqtt while the HTTP request finishes.
                while True:
                    message = await client.messages.__anext__()
                    command = CommandEnvelope.model_validate_json(
                        bytes(message.payload)
                    )
                    if command.command_id not in used_ids:
                        break
                    # QoS 1 may replay the completed ON command during OFF.
                    # Relay validates identical content and only replays its ACK.
                    await relay.handle(
                        str(message.topic), bytes(message.payload), message.retain
                    )
                if (
                    command.state != {"on": expected}
                    or type(command.state.get("on")) is not bool
                    or command.command_id in used_ids
                    or command.correlation_id in used_correlations
                ):
                    raise Failure("Unexpected MQTT command or reused identifiers")
                await relay.handle(
                    str(message.topic), bytes(message.payload), message.retain
                )
                _, original, ack = relay.seen[command.command_id]
                if (
                    original != command
                    or relay.on is not expected
                    or ack.status != "applied"
                    or ack.correlation_id != command.correlation_id
                ):
                    raise Failure("Simulator did not apply the received command")
                used_ids.add(command.command_id)
                used_correlations.add(command.correlation_id)
                phase = action + ": persisted matching ACK/state and final GET"

                async def confirmed():
                    nonlocal observed
                    events = await api.request("GET", EVENTS)
                    has_ack, has_state = evidence(events, command, expected, before)
                    device = await api.request("GET", DEVICE)
                    stored = device["state"].get("on") is expected
                    observed = (
                        f" (ACK={has_ack}, state_event={has_state}, GET={stored})"
                    )
                    return has_ack and has_state and stored

                await poll(confirmed)
                print(
                    f"PASS {action.upper()}: HTTP, MQTT apply, matching ACK/state, persisted GET"
                )
    except TimeoutError:
        raise Failure("Timeout during " + phase + observed) from None
    except Failure as error:
        raise Failure(phase + ": " + str(error)) from None
    except Exception:
        raise Failure(phase + ": MQTT or response validation failed") from None


async def run(config, api, email, password, timeout):
    async with asyncio.timeout(10):
        tokens = await api.request(
            "POST", "/auth/login", {"email": email, "password": password}
        )
        api.token = tokens["access_token"]
    relay = Relay(config.house_id, config.device_id, None)
    context = ssl.create_default_context(cafile=config.ca_file)
    async with aiomqtt.Client(
        hostname=config.host,
        port=config.port,
        username=config.username,
        password=config.password,
        identifier=config.client_id,
        tls_context=context,
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
            await scenario(api, client, relay, timeout)
        finally:
            async with asyncio.timeout(5):
                await relay.send(TopicKind.STATUS, relay.status(False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", help="authorize the real ON/OFF cycle"
    )
    parser.add_argument(
        "--timeout", type=int, choices=range(5, 61), default=30, metavar="5..60"
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("explicit --run is required")
    # Suppress third-party diagnostics; never echo exceptions, payloads or credentials.
    logging.disable(logging.CRITICAL)
    try:
        config = Config.from_env()
        if (config.house_id, config.device_id) != ("e2e_house", "e2e_relay"):
            raise Failure("Only e2e_house/e2e_relay is allowed")
        email = input("E2E user email: ").strip()
        with warnings.catch_warnings():
            # Refuse getpass's echoed-input fallback (e.g. missing docker -it).
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("E2E user password: ")
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(run(config, API("http://core:8000"), email, password, args.timeout))
    except Failure as error:
        print("FAIL: " + str(error))
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("FAIL: interrupted")
        raise SystemExit(1) from None
    except Exception:
        print(
            "FAIL: configuration, TLS/MQTT, login, or response validation; check operator prerequisites"
        )
        raise SystemExit(1) from None
    print("PASS: false -> ON -> true -> OFF -> false")


if __name__ == "__main__":
    main()
