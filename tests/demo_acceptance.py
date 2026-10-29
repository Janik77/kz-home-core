"""Explicit real HTTP/DB/restart/ACL acceptance for a uniquely named demo fixture."""

import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.demo import Demo, command  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    args = parser.parse_args()
    if not args.project.startswith("kzhome-demo-check-"):
        raise ValueError("Only a disposable check project is permitted")
    demo = Demo(args.project)
    demo.load()
    demo.guard()
    credentials = json.loads((demo.path / "user.json").read_text())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = "http://127.0.0.1:" + demo.state["env"]["DEMO_WEB_PORT"] + "/core-api"

    def api(path, body=None, token=None, expected=200):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            base + path,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with opener.open(request, timeout=10) as response:
                assert response.status == expected
                return None if expected == 204 else json.load(response)
        except urllib.error.HTTPError as error:
            assert error.code == expected
            return None

    def read_and_logout():
        api("/auth/me", expected=401)
        pair = api("/auth/login", credentials)
        assert (
            api("/auth/me", token=pair["access_token"])["email"] == credentials["email"]
        )
        rotated = api("/auth/refresh", {"refresh_token": pair["refresh_token"]})
        api("/auth/refresh", {"refresh_token": pair["refresh_token"]}, expected=401)
        token = rotated["access_token"]
        assert len(api("/houses", token=token)) == 1
        assert len(api("/floors", token=token)) == 1
        assert len(api("/rooms", token=token)) == 2
        devices = api("/devices?house_id=e2e_house", token=token)
        assert len(devices) == 3
        assert len(api("/scenes", token=token)) == 2
        assert len(api("/automations", token=token)) == 1
        api("/auth/logout", {"refresh_token": rotated["refresh_token"]}, expected=204)
        api("/auth/refresh", {"refresh_token": rotated["refresh_token"]}, expected=401)
        return {d["id"]: d["state"] for d in devices}

    before = read_and_logout()
    demo.bootstrap()
    demo.bootstrap()
    demo.dc("stop")
    demo.start()
    after = read_and_logout()
    assert before == after, "Reported state must survive repeated bootstrap/restart"
    sql = "SELECT count(*) FROM users; SELECT count(*) FROM houses; SELECT count(*) FROM rooms; SELECT count(*) FROM devices; SELECT count(*) FROM scenes; SELECT state->>'on' FROM devices WHERE id='e2e_relay'; SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname='kzhome';"
    stored = demo.dc(
        "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "kzhome", "-Atc", sql
    ).splitlines()
    assert stored[:5] == ["1", "1", "2", "3", "2"]
    assert stored[5] == str(after["e2e_relay"]["on"]).lower()
    assert stored[6] == "f|f|f"
    print(
        "PASS: real login/me/refresh/logout/read paths; repeat bootstrap; persisted state after full stop/start; independent PostgreSQL counts/state/non-superuser role."
    )

    # Existing v1 broker ACL probe; no valid command or physical ACK is fabricated.
    code = """import json,sys
from simulator.mqtt_relay import Config
from simulator.commissioning_probe import probe
d=json.load(sys.stdin)
c=Config('mosquitto',8883,'relay-simulator',d['relay'],'/run/mqtt/ca.crt','e2e_house','e2e_relay','demo-acl-check')
probe(c,d['observer'])
print('PASS: verified TLS; anonymous/wrong-password rejection; directional and cross-identity ACL isolation.')
"""
    payload = json.loads((demo.path / "simulation.json").read_text())
    payload["observer"] = demo.state["env"]["MQTT_PASSWORD"]
    print(
        command(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                demo.project + "_backend",
                "--mount",
                f"type=bind,source={demo.path / 'mqtt/ca.crt'},target=/run/mqtt/ca.crt,readonly",
                "--entrypoint",
                "python",
                demo.state["env"]["DEMO_CORE_IMAGE"],
                "-c",
                code,
            ],
            input_text=json.dumps(payload),
            timeout=60,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FAIL: isolated demo acceptance; sensitive details withheld")
        raise SystemExit(1) from None
