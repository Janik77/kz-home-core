"""Offline checks for the opt-in runner; no Docker, MQTT or database connection."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.transports.mqtt_models import CommandEnvelope, utc_now
from simulator.e2e_acceptance import API, Failure, evidence, scenario
from simulator.mqtt_relay import Relay


def command(value=True, identifier="cmd"):
    return CommandEnvelope(
        command_id=identifier,
        correlation_id="corr-" + identifier,
        timestamp=utc_now(),
        state={"on": value},
    )


def event(kind, payload, identifier="cmd"):
    return {
        "id": kind + identifier,
        "house_id": "e2e_house",
        "entity_id": "e2e_relay",
        "event_type": kind,
        "correlation_id": "corr-" + identifier,
        "payload": payload,
    }


def events():
    return [
        event("device_ack_received", {"command_id": "cmd", "status": "applied"}),
        event("device_state_changed", {"state": {"on": True}}),
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("house_id", "foreign"),
        ("entity_id", "foreign"),
        ("correlation_id", "old"),
    ],
)
def test_foreign_or_uncorrelated_evidence_cannot_pass(field, value):
    rows = events()
    for row in rows:
        row[field] = value
    assert evidence(rows, command(), True, set()) == (False, False)


def test_stale_wrong_command_and_nonboolean_state_cannot_pass():
    rows = events()
    assert evidence(rows, command(), True, {row["id"] for row in rows}) == (
        False,
        False,
    )
    rows[0]["payload"]["command_id"] = "different"
    rows[1]["payload"]["state"]["on"] = 1
    assert evidence(rows, command(), True, set()) == (False, False)


def test_negative_ack_fails_even_with_matching_state():
    rows = events()
    rows[0]["payload"]["status"] = "rejected"
    with pytest.raises(Failure, match="negative ACK"):
        evidence(rows, command(), True, set())


class FakeStack:
    def __init__(self, missing=None):
        self.queue = asyncio.Queue()
        self.messages = self
        self.rows = []
        self.device = {"state": {"on": True}, "online": False, "metadata": {}}
        self.actions = []
        self.missing = missing
        self.last_command = None

    async def __anext__(self):
        return await self.queue.get()

    async def subscribe(self, topic, qos):
        assert topic == "kzhome/v1/e2e_house/e2e_relay/set"
        assert qos == 1

    async def publish(self, topic, payload, **kwargs):
        data = json.loads(payload)
        if topic.endswith("/status"):
            self.device["online"] = True
            self.device["metadata"]["last_seen"] = data["last_seen"].replace(
                "Z", "+00:00"
            )
        elif topic.endswith("/ack") and self.missing != "ack":
            identifier = data["command_id"]
            self.rows.append(event("device_ack_received", data, identifier))
        elif topic.endswith("/state"):
            if data["correlation_id"].startswith("corr-"):
                if self.missing != "state":
                    self.rows.append(
                        event("device_state_changed", data, data["correlation_id"][5:])
                    )
            if self.missing != "get" or not self.actions:
                self.device["state"] = data["state"]

    async def request(self, method, path, body=None):
        if path.startswith("/events"):
            return copy.deepcopy(self.rows)
        if method == "GET":
            return copy.deepcopy(self.device)
        self.actions.append(path.rsplit("/", 1)[1])
        if self.missing == "http":
            raise Failure("HTTP request failed (status 403)")
        if self.missing == "receipt":
            return self.device
        # Include a duplicate ON delivery when issuing OFF, as allowed by QoS 1.
        if self.last_command:
            await self.queue.put(self.last_command)
        cmd = command(self.actions[-1] == "on", self.actions[-1])
        self.last_command = SimpleNamespace(
            topic="kzhome/v1/e2e_house/e2e_relay/set",
            payload=cmd.model_dump_json().encode(),
            retain=False,
        )
        await self.queue.put(self.last_command)
        return self.device


def test_full_scenario_checks_both_transitions_and_duplicate_delivery(capsys):
    async def check():
        stack = FakeStack()
        relay = Relay("e2e_house", "e2e_relay", stack.publish)
        await scenario(stack, stack, relay, 1)
        assert stack.actions == ["on", "off"]
        assert stack.device["state"]["on"] is False
        assert relay.on is False

    asyncio.run(check())
    assert "PASS OFF" in capsys.readouterr().out


@pytest.mark.parametrize(
    "missing,diagnostic",
    [
        ("ack", "ACK=False"),
        ("state", "state_event=False"),
        ("get", "GET=False"),
        ("receipt", "simulator MQTT receipt"),
        ("http", "status 403"),
    ],
)
def test_missing_round_trip_evidence_fails_with_bounded_diagnostics(
    missing, diagnostic
):
    async def check():
        stack = FakeStack(missing)
        relay = Relay("e2e_house", "e2e_relay", stack.publish)
        with pytest.raises(Failure, match=diagnostic):
            await scenario(stack, stack, relay, 0.05)

    asyncio.run(check())


def test_http_errors_do_not_echo_secrets():
    class BrokenOpener:
        def open(self, *args, **kwargs):
            raise ValueError("password=secret DATABASE_URL=secret token=secret")

    api = API("http://core:8000")
    api.opener = BrokenOpener()
    with pytest.raises(Failure) as error:
        asyncio.run(api.request("GET", "/devices/e2e_relay"))
    assert str(error.value) == "HTTP transport or JSON response failed"
