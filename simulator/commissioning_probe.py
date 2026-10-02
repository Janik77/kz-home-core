"""Opt-in broker ACL probe; no Core HTTP/database access and no valid commands.

Uses MQTT 5 authorization reasons (the firmware wire contract remains v1).
Non-retained, deliberately invalid-v1 markers cannot command a conforming relay.
Run with the physical relay/simulator disconnected and exclusive operator access.
"""

import argparse
from contextlib import contextmanager
import getpass
import logging
from queue import Queue
import ssl
import time
import warnings
from uuid import uuid4

import paho.mqtt.client as mqtt

from simulator.mqtt_relay import Config


class Failure(Exception):
    """Fixed, secret-free diagnostics only."""


class Denied(Failure):
    pass


class Session:
    def __init__(self, config, username, password):
        self.connected = Queue()
        self.published = Queue()
        self.subscribed = Queue()
        self.messages = Queue()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="commissioning-" + uuid4().hex,
            protocol=mqtt.MQTTv5,
            reconnect_on_failure=False,
        )
        self.client.tls_set_context(ssl.create_default_context(cafile=config.ca_file))
        self.client.username_pw_set(username, password)
        self.client.on_connect = lambda c, u, f, reason, p: self.connected.put(
            reason.value
        )
        self.client.on_publish = lambda c, u, mid, reason, p: self.published.put(
            (mid, reason.value)
        )
        self.client.on_subscribe = lambda c, u, mid, reasons, p: self.subscribed.put(
            (mid, [r.value for r in reasons])
        )
        self.client.on_message = lambda c, u, message: self.messages.put(
            (message.topic, bytes(message.payload))
        )
        self.config = config

    def open(self):
        self.client.connect_timeout = 5
        self.client.connect(self.config.host, self.config.port, keepalive=30)
        self.client.loop_start()
        reason = self.connected.get(timeout=5)
        if reason in (134, 135):
            raise Denied("Broker authentication denied")
        if reason != 0:
            raise Failure("Broker connection was not accepted")

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()

    def subscribe(self, topics):
        result, mid = self.client.subscribe([(topic, 1) for topic in topics])
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise Failure("Subscription could not be sent")
        received_mid, reasons = self.subscribed.get(timeout=5)
        if received_mid != mid:
            raise Failure("Unexpected subscription acknowledgment")
        return reasons

    def publish(self, topic, payload, denied=False):
        message = self.client.publish(topic, payload, qos=1, retain=False)
        if message.rc != mqtt.MQTT_ERR_SUCCESS:
            raise Failure("Publish could not be sent")
        mid, reason = self.published.get(timeout=5)
        if mid != message.mid or (reason != 135 if denied else reason >= 128):
            raise Failure("Unexpected publish authorization result")

    def received(self, expected, marker):
        pending = set(expected)
        deadline = time.monotonic() + 5
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Failure("Expected delivery timed out")
            topic, payload = self.messages.get(timeout=remaining)
            if topic not in expected:
                raise Failure("Device received a forbidden topic")
            if payload == marker:
                pending.discard(topic)


@contextmanager
def connection(config, username, password):
    session = Session(config, username, password)
    try:
        session.open()
        yield session
    finally:
        session.close()


def require_denied(config, username, password):
    try:
        with connection(config, username, password):
            pass
    except Denied:
        return
    raise Failure("Broker accepted an identity that must be denied")


def probe(config, observer_password):
    # Random, unregistered foreign IDs avoid touching another real device.
    other = "probe-" + uuid4().hex
    own = f"kzhome/v1/{config.house_id}/{config.device_id}/"
    foreign = [
        f"kzhome/v1/{config.house_id}/{other}/",
        f"kzhome/v1/{other}/{config.device_id}/",
    ]
    marker = ('{"commissioning_probe":"' + uuid4().hex + '"}').encode()
    require_denied(config, None, None)
    require_denied(config, config.username, uuid4().hex)
    with connection(config, "kzhome-core", observer_password) as observer:
        allowed = [own + kind for kind in ("ack", "state", "status")]
        if observer.subscribe(allowed) != [1, 1, 1]:
            raise Failure("Observer subscriptions were denied")
        with connection(config, config.username, config.password) as device:
            filters = [own + "set", "kzhome/v1/+/+/set", "#", own + "state"]
            filters += [prefix + "set" for prefix in foreign]
            reasons = device.subscribe(filters)
            if reasons[0] != 1 or any(r not in (0, 1, 135) for r in reasons):
                raise Failure("Unexpected subscription authorization result")
            for topic in allowed:
                device.publish(topic, marker)
            observer.received(allowed, marker)
            # Includes reversed direction and telemetry, which this relay never needs.
            forbidden = [own + "set", own + "telemetry"]
            forbidden += [
                p + kind
                for p in foreign
                for kind in ("set", "ack", "state", "status", "telemetry")
            ]
            for topic in forbidden:
                device.publish(topic, marker, denied=True)
            for prefix in foreign:
                observer.publish(prefix + "set", marker)
            # Same publisher/connection ordering: a final allowed delivery is the
            # positive control after forbidden reads, even if wildcard SUBACKs pass.
            observer.publish(own + "set", marker)
            device.received({own + "set"}, marker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--expect-denied",
        action="store_true",
        help="check old/revoked credentials only",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("explicit --run is required")
    logging.disable(logging.CRITICAL)
    try:
        config = Config.from_env()
        if config.username != "kzdevice-" + config.device_id:
            raise Failure("The dedicated physical-device username is required")
        if args.expect_denied:
            require_denied(config, config.username, config.password)
        else:
            with warnings.catch_warnings(record=False):
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Core MQTT observer password (hidden): ")
            probe(config, password)
    except (Exception, KeyboardInterrupt):
        print(
            "FAIL: broker authentication/ACL, TLS, timeout or configuration; no acceptance claimed"
        )
        raise SystemExit(1) from None
    print(
        "PASS: broker denied old/revoked credentials"
        if args.expect_denied
        else "PASS: verified TLS, authentication and directional device ACL isolation"
    )


if __name__ == "__main__":
    main()
