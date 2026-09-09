from fastapi.testclient import TestClient

from app.main import app, devices


def test_health_and_seed_data() -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert {item["id"] for item in client.get("/devices").json()} == {"light1", "relay1", "motion1"}
        assert client.get("/rooms").json()[0]["id"] == "living_room"


def test_switching_device_broadcasts_websocket_event() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        client.post("/devices/light1/off")
        response = client.post("/devices/light1/on")
        assert response.status_code == 200
        assert websocket.receive_json() == {"type": "device_state_changed", "device_id": "light1", "state": "on"}


def test_motion_automation_turns_light_on() -> None:
    with TestClient(app) as client:
        client.post("/devices/light1/off")
        client.post("/devices/motion1/off")
        # A sensor update uses the service because on/off endpoints are intended
        # for actuator commands.
        import asyncio

        asyncio.run(devices.set_state("motion1", True))
        assert client.get("/devices/light1").json()["state"] == "on"


def test_not_found() -> None:
    with TestClient(app) as client:
        assert client.get("/devices/unknown").status_code == 404
        assert client.post("/scenes/unknown/run").status_code == 404
