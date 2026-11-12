"""Opt-in real Mosquitto, real aiomqtt/Paho, loopback-only subscription tests."""
import asyncio
import os
from pathlib import Path
import socket
import shutil
import subprocess
import time

import aiomqtt
import pytest

from app.events import EventBus
from app.transports.mqtt import MQTTGateway
from app.transports.mqtt_client import AiomqttClient

SOURCE = os.environ.get("SUBACK_MOSQUITTO_SOURCE")
BINARY = os.environ.get("SUBACK_MOSQUITTO_BINARY")
pytestmark = pytest.mark.skipif(not (SOURCE and BINARY), reason="opt-in local Mosquitto build")


@pytest.fixture
def broker(tmp_path, monkeypatch):
    plugin = tmp_path / "acl.so"
    subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                    "-I" + str(Path(SOURCE) / "include"),
                    str(Path(__file__).parent / "fixtures/suback_acl.c"),
                    "-o", str(plugin)], check=True)
    processes = []

    def start(denied, tls):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = [f"listener {port} 127.0.0.1", "allow_anonymous true",
                  "persistence false", f"plugin {plugin}", f"plugin_opt_deny {denied}",
                  "log_type all"]
        if tls:
            key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-days", "1", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
                            "-keyout", str(key), "-out", str(cert)],
                           check=True, capture_output=True)
            config += [f"certfile {cert}", f"keyfile {key}"]
            monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        path = tmp_path / "mosquitto.conf"
        path.write_text("\n".join(config) + "\n")
        log = (tmp_path / "broker.log").open("w+")
        process = subprocess.Popen([BINARY, "-c", str(path)], stdout=log, stderr=log)
        processes.append((process, log))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log.seek(0)
                pytest.fail(log.read())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    return port
            except OSError:
                time.sleep(.02)
        pytest.fail("Owned loopback broker did not start")

    yield start
    for process, log in processes:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
    shutil.rmtree(tmp_path)


@pytest.mark.parametrize("tls", [False, True])
def test_real_broker_returns_denied_suback(broker, tls):
    """Prove this fixture yields a SUBACK refusal, not a connect failure/drop."""
    port = broker("fixture/denied", tls)

    async def scenario():
        import ssl
        async with aiomqtt.Client("127.0.0.1", port=port,
                                  tls_context=ssl.create_default_context() if tls else None) as client:
            granted = await client.subscribe("fixture/allowed", qos=1)
            denied = await client.subscribe("fixture/denied", qos=1)
            assert [code.value for code in granted] == [1]
            assert [code.value for code in denied] == [128]

    asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.mark.parametrize("tls", [False, True])
@pytest.mark.parametrize("mode", ["mandatory", "control", "granted"])
def test_gateway_readiness_with_real_broker(broker, tls, mode):
    denied = {"mandatory": "kzhome/v1/+/+/ack", "control": "kzhome/v1/+/+/control/request",
              "granted": "fixture/unused"}[mode]
    port = broker(denied, tls)

    async def scenario():
        reached = asyncio.Event()
        events, delays = [], []

        async def collect(event):
            events.append(event.type)
            if event.type == "mqtt_connected":
                reached.set()

        async def sleep(delay):
            delays.append(delay)
            reached.set()
            await asyncio.Event().wait()

        async def unused(*args):
            pass

        bus = EventBus()
        bus.subscribe(collect)
        client = AiomqttClient(host="127.0.0.1", port=port, username=None,
                               password=None, tls_enabled=tls, keepalive=60,
                               client_id="suback-integration")
        gateway = MQTTGateway(client, bus, unused, unused, unused, sleep=sleep)
        gateway.start()
        try:
            await asyncio.wait_for(reached.wait(), 10)
            assert gateway.connected is (mode != "mandatory")
            assert gateway.control_subscribed is (mode == "granted")
            assert ("mqtt_connected" in events) is (mode != "mandatory")
            assert delays == ([1] if mode == "mandatory" else [])
        finally:
            await gateway.stop()
        assert client._client is None
        assert not gateway.connected and not gateway.control_subscribed

    asyncio.run(asyncio.wait_for(scenario(), 15))
