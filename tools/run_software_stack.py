"""Run the pinned, disposable Web/Core/PostgreSQL/MQTT software-only lab."""

import argparse
import asyncio
import importlib.util
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
from uuid import uuid4

CORE = Path(__file__).resolve().parents[1]
WEB_SHA = "1a3226527e6b9501735b29e251a2982c7f5577f4"


def validate_web(root):
    root = root.resolve()
    head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != WEB_SHA:
        raise ValueError(f"Use the pinned Web checkout {WEB_SHA}")
    if subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain"], text=True
    ).strip():
        raise ValueError("Use a clean disposable Web checkout")
    if any(path.name != ".env.example" for path in root.glob(".env*")):
        raise ValueError("Refusing Web .env inputs; use the disposable checkout")
    if not (root / "node_modules/vite/bin/vite.js").is_file():
        raise ValueError("Run npm ci in the pinned Web checkout first")
    return root


async def run(
    web,
    smoke,
    *,
    stack_factory=None,
    provisioner=None,
    on_ready=None,
    login_hint=None,
    web_port=None,
):
    import aiomqtt
    import httpx
    from confirmed_live_stack import provision, stack
    from simulator.mqtt_relay import Relay
    from app.transports.mqtt_topics import TopicKind

    async with (stack_factory or stack)(CORE) as values:
        config, seed, ids, api, sql, migration = values
        await (provisioner or provision)(api, ids, seed)
        async with aiomqtt.Client(
            hostname=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
            identifier=config.client_id,
            tls_context=ssl.create_default_context(cafile=config.ca_file),
        ) as mqtt:
            relay = Relay(config.house_id, config.device_id, mqtt.publish)
            await mqtt.subscribe(relay.topic(TopicKind.SET), qos=1)
            await relay.send(TopicKind.STATUS, relay.status(True))
            await relay.state("initial-state")

            async def receive():
                async for message in mqtt.messages:
                    await relay.handle(
                        str(message.topic), bytes(message.payload), message.retain
                    )

            async def heartbeat():
                while True:
                    await asyncio.sleep(60)
                    await relay.send(TopicKind.STATUS, relay.status(True))

            receiver = asyncio.create_task(receive())
            heartbeats = asyncio.create_task(heartbeat())
            vite = None
            try:
                async with asyncio.timeout(10):
                    while not (await api.request("GET", "/devices/fixture_relay"))[
                        "online"
                    ]:
                        await asyncio.sleep(0.1)
                with socket.socket() as sock:
                    # Match Node listen semantics after a rapid restart (TIME_WAIT).
                    # This does not permit sharing a port with an active listener.
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("127.0.0.1", web_port or 0))
                    port = sock.getsockname()[1]
                environment = {
                    key: os.environ[key]
                    for key in ("PATH", "HOME", "TMPDIR")
                    if key in os.environ
                }
                environment.update(
                    VITE_DATA_MODE="core",
                    VITE_API_URL="/core-api",
                    CORE_DEV_PROXY_TARGET=api.base,
                )
                vite = subprocess.Popen(
                    [
                        "node",
                        str(web / "node_modules/vite/bin/vite.js"),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--strictPort",
                    ],
                    cwd=web,
                    env=environment,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                url = f"http://127.0.0.1:{port}"
                async with httpx.AsyncClient(base_url=url, trust_env=False) as http:
                    async with asyncio.timeout(20):
                        while True:
                            if vite.poll() is not None:
                                raise RuntimeError("Owned Vite process exited")
                            try:
                                response = await http.get("/")
                                if response.status_code == 200:
                                    break
                            except httpx.ConnectError:
                                pass
                            await asyncio.sleep(0.1)
                    print(
                        f"Owned software stack: {url}; migration {migration}",
                        flush=True,
                    )
                    if on_ready:
                        on_ready(url)
                    if smoke:
                        login = await http.post(
                            "/core-api/auth/login",
                            json={
                                "email": "owner@example.test",
                                "password": seed["password"],
                            },
                        )
                        assert login.status_code == 200
                        http.headers["Authorization"] = (
                            "Bearer " + login.json()["access_token"]
                        )
                        for on in (True, False):
                            response = await http.post(
                                "/core-api/v1/devices/fixture_relay/commands",
                                headers={"Idempotency-Key": str(uuid4())},
                                json={"on": on},
                            )
                            assert response.status_code in (200, 202)
                            command_id = response.json()["command_id"]
                            async with asyncio.timeout(15):
                                while True:
                                    receipt = await http.get(
                                        f"/core-api/v1/houses/fixture_house/commands/{command_id}"
                                    )
                                    assert receipt.status_code == 200
                                    data = receipt.json()
                                    if data["status"] == "applied":
                                        assert (
                                            data["reported_on"] is on
                                            and data["ack_status"] == "applied"
                                        )
                                        break
                                    assert data["status"] in ("queued", "dispatched")
                                    await asyncio.sleep(0.1)
                        assert sql("SELECT count(*) FROM physical_devices") == "0"
                        print(
                            "PASS launcher: Web proxy login, software ON/OFF, applied ACK/state receipts, zero physical devices",
                            flush=True,
                        )
                    else:
                        # Only temporary fixture credentials, deliberately withheld in CI smoke mode.
                        print(
                            login_hint
                            or f"Login owner@example.test / {seed['password']}\nCtrl-C stops this owned stack and removes its data.",
                            flush=True,
                        )
                        while True:
                            if vite.poll() is not None:
                                raise RuntimeError("Owned Vite process exited")
                            if heartbeats.done():
                                await heartbeats
                                raise RuntimeError("Software MQTT heartbeat stopped")
                            if receiver.done():
                                await receiver
                                raise RuntimeError("Software MQTT receiver stopped")
                            await asyncio.sleep(1)
            finally:
                if vite is not None:
                    try:
                        os.killpg(vite.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.to_thread(vite.wait, timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(vite.pid, signal.SIGKILL)
                        await asyncio.to_thread(vite.wait, timeout=5)
                receiver.cancel()
                heartbeats.cancel()
                await asyncio.gather(receiver, heartbeats, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-root", required=True, type=Path)
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("This owned-process launcher currently supports Linux/macOS only")
    web = validate_web(args.web_root)
    os.environ["LIVE_CORE_SOURCE"] = str(CORE)
    sys.path[:0] = [str(CORE), str(web / "e2e")]
    spec = importlib.util.spec_from_file_location(
        "owned_web_runner", web / "e2e/confirmed-live-runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before = module.inventory()
    try:
        asyncio.run(run(web, args.smoke_test))
    except KeyboardInterrupt:
        print("Stopped owned software stack", flush=True)
    finally:
        if module.inventory() != before:
            raise RuntimeError(
                "Docker inventory changed; inspect manually, do not delete unrelated resources"
            )
        print(
            "PASS preservation: Docker inventory unchanged after owned cleanup",
            flush=True,
        )


if __name__ == "__main__":
    main()
