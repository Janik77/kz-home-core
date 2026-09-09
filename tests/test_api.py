from fastapi.testclient import TestClient

from app.main import create_app


def client() -> TestClient:
    return TestClient(create_app(run_simulator=False))


def test_health() -> None:
    with client() as api:
        assert api.get("/health").json() == {
            "status": "ok",
            "version": "0.2.0",
            "devices": 5,
            "automations": 1,
        }


def test_get_devices() -> None:
    with client() as api:
        response = api.get("/devices")
        assert response.status_code == 200
        assert len(response.json()) == 5
        assert response.json()[0]["metadata"] == {"protocol": "virtual"}


def test_patch_device_state_and_websocket_event() -> None:
    with client() as api, api.websocket_connect("/ws") as websocket:
        response = api.patch(
            "/devices/living_room_light/state",
            json={"on": True, "brightness": 50},
        )
        assert response.status_code == 200
        assert response.json()["state"] == {"on": True, "brightness": 50}
        assert websocket.receive_json() == {
            "type": "device_state_changed",
            "device_id": "living_room_light",
            "state": {"on": True, "brightness": 50},
        }


def test_scene_execution() -> None:
    with client() as api:
        api.patch("/devices/living_room_light/state", json={"on": True})
        api.patch("/devices/living_room_curtain/state", json={"position": 80})
        response = api.post("/scenes/leave_home/run")
        assert response.status_code == 200
        assert api.get("/devices/living_room_light").json()["state"]["on"] is False
        assert api.get("/devices/living_room_curtain").json()["state"]["position"] == 0


def test_automation_trigger() -> None:
    with client() as api, api.websocket_connect("/ws") as websocket:
        response = api.patch("/devices/hall_motion/state", json={"motion": True})
        assert response.status_code == 200
        assert websocket.receive_json()["device_id"] == "living_room_light"
        assert websocket.receive_json() == {
            "type": "automation_triggered",
            "automation_id": "hall_motion_light",
        }
        assert api.get("/devices/living_room_light").json()["state"]["on"] is True


def test_shortcuts_and_not_found() -> None:
    with client() as api:
        assert api.post("/devices/living_room_light/on").json()["state"]["on"] is True
        assert api.post("/devices/living_room_light/off").json()["state"]["on"] is False
        assert api.get("/houses/unknown").status_code == 404
        assert api.get("/devices/unknown").status_code == 404
