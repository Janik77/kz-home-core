import re
from dataclasses import dataclass
from enum import Enum

PROTOCOL_VERSION = "v1"
TOPIC_ROOT = f"kzhome/{PROTOCOL_VERSION}"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class TopicKind(str, Enum):
    STATE = "state"
    SET = "set"
    ACK = "ack"
    TELEMETRY = "telemetry"
    STATUS = "status"
    CONTROL_COMMAND = "control/command"
    CONTROL_REQUEST = "control/request"
    CONTROL_REPLY = "control/reply"


@dataclass(frozen=True)
class TopicPolicy:
    qos: int
    retain: bool


TOPIC_POLICY = {
    TopicKind.STATE: TopicPolicy(qos=1, retain=True),
    TopicKind.SET: TopicPolicy(qos=1, retain=False),
    TopicKind.ACK: TopicPolicy(qos=1, retain=False),
    TopicKind.TELEMETRY: TopicPolicy(qos=0, retain=False),
    TopicKind.STATUS: TopicPolicy(qos=1, retain=True),
    TopicKind.CONTROL_COMMAND: TopicPolicy(qos=1, retain=False),
    TopicKind.CONTROL_REQUEST: TopicPolicy(qos=1, retain=False),
    TopicKind.CONTROL_REPLY: TopicPolicy(qos=1, retain=False),
}


@dataclass(frozen=True)
class ParsedTopic:
    house_id: str
    device_id: str
    kind: TopicKind


def _validate_identifier(value: str, name: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid MQTT {name}")


def build_topic(house_id: str, device_id: str, kind: TopicKind) -> str:
    _validate_identifier(house_id, "house_id")
    _validate_identifier(device_id, "device_id")
    return f"{TOPIC_ROOT}/{house_id}/{device_id}/{kind.value}"


def parse_topic(topic: str) -> ParsedTopic:
    parts = topic.split("/")
    if len(parts) not in (5, 6) or parts[:2] != ["kzhome", PROTOCOL_VERSION]:
        raise ValueError("Malformed or unsupported MQTT topic")
    house_id, device_id = parts[2:4]
    raw_kind = "/".join(parts[4:])
    _validate_identifier(house_id, "house_id")
    _validate_identifier(device_id, "device_id")
    try:
        kind = TopicKind(raw_kind)
    except ValueError as error:
        raise ValueError("Unsupported MQTT topic kind") from error
    return ParsedTopic(house_id, device_id, kind)


def subscription_topics() -> tuple[tuple[str, int], ...]:
    inbound = (
        TopicKind.STATE,
        TopicKind.ACK,
        TopicKind.TELEMETRY,
        TopicKind.STATUS,
        TopicKind.CONTROL_REQUEST,
    )
    return tuple(
        (f"{TOPIC_ROOT}/+/+/{kind.value}", TOPIC_POLICY[kind].qos)
        for kind in inbound
    )
