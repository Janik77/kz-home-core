"""Opt-in acceptance for one already commissioned physical relay; no DB access."""

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
from pydantic import TypeAdapter

from app.schemas.onboarding import TopicID
from app.transports.mqtt_topics import TopicKind
from simulator.e2e_acceptance import API, Failure, poll, scenario as relay_scenario
from simulator.mqtt_relay import Config, Relay


def paths(config):
    for value in (config.house_id, config.device_id):
        TypeAdapter(TopicID).validate_python(value)
    if config.username != "kzdevice-" + config.device_id:
        raise Failure("A commissioned physical-device MQTT identity is required")
    return (
        "/devices/" + config.device_id,
        f"/houses/{config.house_id}/physical-devices/{config.device_id}",
        f"/events?house_id={config.house_id}&limit=500",
    )


def secret_free(value, secrets=()):
    """Check ordinary reads without displaying responses on failure."""
    if isinstance(value, dict):
        for key, item in value.items():
            if any(
                part in key.lower()
                for part in (
                    "password",
                    "token",
                    "secret",
                    "claim_code",
                    "hardware_id",
                )
            ):
                raise Failure("An ordinary API response contains a private field")
            secret_free(item, secrets)
    elif isinstance(value, list):
        for item in value:
            secret_free(item, secrets)
    if any(secret and secret in json.dumps(value) for secret in secrets):
        raise Failure("An ordinary API response contains credential material")


async def preflight(config, api, email, password):
    device, physical, _ = paths(config)
    async with asyncio.timeout(15):
        if await api.request("GET", "/ready") != {"status": "ready"}:
            raise Failure("Core is not ready at the packaged migration head")
        tokens = await api.request(
            "POST", "/auth/login", {"email": email, "password": password}
        )
        api.token = tokens["access_token"]
        binding = await api.request("GET", physical)
        current = await api.request("GET", device)
        if (
            binding["device_id"] != config.device_id
            or binding["house_id"] != config.house_id
            or binding["status"] != "active"
            or binding["protocol_version"] != "v1"
            or binding["hardware_model"] != "esp32-c6-relay-v1"
            or current["type"] != "relay"
            or current["capabilities"] != ["on_off"]
        ):
            raise Failure("The physical relay must be correctly bound and active")
        # Require diagnostic access; residents can control but cannot read events.
        await api.request("GET", f"/events?house_id={config.house_id}&limit=1")
        rules = await api.request("GET", "/automations")
        if any(
            rule["enabled"] and config.device_id in referenced_ids(rule)
            for rule in rules
        ):
            raise Failure(
                "Disable automations referencing this relay before acceptance"
            )
        secret_free([binding, current], (password, config.password, api.token))


def referenced_ids(value):
    if isinstance(value, dict):
        return set().union(*(referenced_ids(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(referenced_ids(item) for item in value))
    return {value} if isinstance(value, str) else set()


def device_events(rows, config):
    return [
        row
        for row in rows
        if row["house_id"] == config.house_id and row["entity_id"] == config.device_id
    ]


async def scenario(config, api, client, relay, timeout, *, quiet=1):
    device, physical, events = paths(config)
    phase = "HTTP authentication and house isolation"
    try:
        async with asyncio.timeout(timeout):
            token, api.token = api.token, None
            try:
                await api.request("POST", device + "/on", expected_status=401)
            finally:
                api.token = token
            foreign = "acceptance-" + uuid4().hex
            await api.request(
                "GET",
                f"/houses/{foreign}/physical-devices/{config.device_id}",
                expected_status=404,
            )
        commands = await relay_scenario(
            api, client, relay, timeout, device=device, events_path=events
        )
        phase = "duplicate command/ACK and unchanged state report"
        async with asyncio.timeout(timeout):
            before = device_events(await api.request("GET", events), config)
            state_ids = {
                r["id"] for r in before if r["event_type"] == "device_state_changed"
            }
            old_ids = {r["id"] for r in before}
            # Replay the exact ON receipt through the unchanged relay cache after OFF.
            # The ACK traverses the real broker; no state or physical action is repeated.
            await relay.state(
                commands[-1].correlation_id
            )  # Duplicate current OFF report.
            await relay.handle(
                relay.topic(TopicKind.SET), commands[0].model_dump_json().encode()
            )

            async def replayed():
                rows = device_events(await api.request("GET", events), config)
                return any(
                    r["id"] not in old_ids
                    and r["event_type"] == "device_ack_received"
                    and r["correlation_id"] == commands[0].correlation_id
                    and r["payload"].get("command_id") == commands[0].command_id
                    and r["payload"].get("status") == "applied"
                    for r in rows
                )

            await poll(replayed)
            rows = device_events(await api.request("GET", events), config)
            if (
                relay.on is not False
                or (await api.request("GET", device))["state"].get("on") is not False
                or {r["id"] for r in rows if r["event_type"] == "device_state_changed"}
                != state_ids
            ):
                raise Failure(
                    "Replay changed resulting state or repeated a state event"
                )
        print("PASS replay: cached ACK persisted; OFF state/event unchanged")
        phase = "offline delivery failure observation"
        async with asyncio.timeout(timeout):
            await client.unsubscribe(relay.topic(TopicKind.SET))
            status = relay.status(False)
            await relay.send(TopicKind.STATUS, status)

            async def offline():
                current = await api.request("GET", device)
                return (
                    current["online"] is False
                    and current["metadata"].get("last_seen")
                    == status.last_seen.isoformat()
                )

            await poll(offline)
            before = device_events(await api.request("GET", events), config)
            before_ids = {r["id"] for r in before}
            # Existing behavior: HTTP confirms dispatch, even while device is offline.
            response = await api.request("POST", device + "/on", expected_status=200)
            if response["state"].get("on") is not False:
                raise Failure("Offline dispatch overwrote observed state")
            await client.subscribe(relay.topic(TopicKind.SET), qos=1)
            try:
                async with asyncio.timeout(quiet):
                    await client.messages.__anext__()
            except TimeoutError:
                pass
            else:
                raise Failure(
                    "Offline command was queued or retained for this clean session"
                )
            rows = device_events(await api.request("GET", events), config)
            if any(
                r["id"] not in before_ids
                and r["event_type"] in ("device_state_changed", "device_ack_received")
                for r in rows
            ):
                raise Failure("Offline command falsely produced completion evidence")
            current = await api.request("GET", device)
            if (
                current["state"].get("on") is not False
                or current["online"] is not False
            ):
                raise Failure("Offline resulting state/availability changed")
            secret_free(
                [
                    current,
                    await api.request("GET", "/devices?house_id=" + config.house_id),
                    await api.request("GET", physical),
                    rows,
                ],
                (config.password, api.token),
            )
        print(
            "PASS offline: dispatch only; no ACK/state confirmation or retained command"
        )
        return commands
    except TimeoutError:
        raise Failure("Timeout during " + phase) from None
    except Failure:
        raise
    except Exception:
        raise Failure(phase + ": transport or response validation failed") from None


async def run(config, api, email, password, timeout):
    await preflight(config, api, email, password)
    relay = Relay(config.house_id, config.device_id, None)
    async with aiomqtt.Client(
        hostname=config.host,
        port=config.port,
        username=config.username,
        password=config.password,
        identifier=config.client_id,
        timeout=5,
        tls_context=ssl.create_default_context(cafile=config.ca_file),
        will=aiomqtt.Will(
            relay.topic(TopicKind.STATUS),
            relay.status(False).model_dump_json(),
            qos=1,
            retain=True,
        ),
    ) as client:
        relay.publish = client.publish
        try:
            return await scenario(config, api, client, relay, timeout)
        finally:
            try:
                async with asyncio.timeout(5):
                    await relay.send(TopicKind.STATUS, relay.status(False))
            except Exception:
                raise Failure(
                    "Cleanup could not publish offline; inspect resulting state"
                ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="authorize real ON/OFF and offline-dispatch checks",
    )
    parser.add_argument(
        "--timeout", type=int, choices=range(5, 61), default=30, metavar="5..60"
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("explicit --run is required")
    logging.disable(logging.CRITICAL)
    try:
        config = Config.from_env()
        paths(config)
        email = input("House operator email: ").strip()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("House operator password (hidden): ")
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(run(config, API("http://core:8000"), email, password, args.timeout))
    except Failure as error:
        print("FAIL: " + str(error))
        raise SystemExit(1) from None
    except (Exception, KeyboardInterrupt):
        print(
            "FAIL: configuration, TLS/MQTT, authentication or validation; no acceptance claimed"
        )
        raise SystemExit(1) from None
    print(
        "PASS: commissioned physical identity software acceptance; hardware not tested"
    )


if __name__ == "__main__":
    main()
