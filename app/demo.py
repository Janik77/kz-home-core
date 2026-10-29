"""Explicit demo tooling; never part of API startup. Reuses existing E2E tools."""

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.request


def bootstrap_demo(engine, email, password):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.bootstrap_e2e import bootstrap, BootstrapError
    from app.models import UserORM, HouseORM
    from app.repositories import (
        DeviceRepository,
        RoomRepository,
        SceneRepository,
        HouseRepository,
        AutomationRepository,
    )
    from app.schemas import RoomCreate, DeviceCreate, DeviceUpdate, SceneCreate
    from app.services.device_service import DeviceService
    from app.services.scene_service import SceneService
    from app.services.automation_service import AutomationService
    from app.events import EventBus
    from simulator.e2e_automation import motion_definition, rule_definition

    if not email.startswith("demo-") or not email.endswith("@example.invalid"):
        raise BootstrapError("Only a generated demo account is allowed")
    with Session(engine) as session:
        users = list(session.scalars(select(UserORM)))
        houses = list(session.scalars(select(HouseORM)))
        if any(u.email != email for u in users) or any(
            h.id != "e2e_house" for h in houses
        ):
            raise BootstrapError("Refusing a database with non-demo users or houses")
    bootstrap(engine, email, password)  # Actual atomic UserService/owner/E2E graph.
    # Second atomic stage is resumable; never resets observed state on repeat.
    with engine.begin() as connection:
        with Session(bind=connection, join_transaction_mode="rollback_only") as session:
            rooms = RoomRepository(session)
            devices = DeviceRepository(session)
            houses = HouseRepository(session)
            bus = EventBus()
            service = DeviceService(devices, rooms, bus)
            if houses.get("e2e_house").name == "E2E acceptance":
                houses.update("e2e_house", {"name": "DEMO · виртуальный дом"})
            if rooms.get("e2e_room").name == "E2E room":
                rooms.update("e2e_room", {"name": "SIM · гостиная"})
            if not rooms.exists("demo_room"):
                rooms.create(
                    RoomCreate(
                        id="demo_room", floor_id="e2e_floor", name="SIM · спальня"
                    ).model_dump()
                )
            elif rooms.get("demo_room").floor_id != "e2e_floor":
                raise BootstrapError("Conflicting demo room")
            relay = devices.get("e2e_relay")
            if relay.name == "E2E relay" and not relay.metadata_:
                service.update(
                    "e2e_relay",
                    DeviceUpdate(name="SIM · реле", metadata={"simulated": True}),
                )
            definitions = [
                motion_definition(),
                DeviceCreate(
                    id="demo_temperature",
                    name="SIM · температура (снимок)",
                    room_id="demo_room",
                    type="temperature_sensor",
                    capabilities=["temperature"],
                    state={"temperature": 22.5},
                    metadata={"simulated": True, "static_snapshot": True},
                    online=False,
                ),
            ]
            definitions[0].name = "SIM · движение"
            definitions[0].metadata = {"simulated": True}
            for definition in definitions:
                if devices.exists(definition.id):
                    current = service.get(definition.id)
                    if any(
                        getattr(current, key) != getattr(definition, key)
                        for key in ("room_id", "type", "capabilities")
                    ):
                        raise BootstrapError("Conflicting demo device")
                else:
                    service.create(definition)
            scenes = SceneRepository(session)
            scene_service = SceneService(scenes, houses, service, bus)
            for suffix, value in (("on", True), ("off", False)):
                definition = SceneCreate(
                    id="demo_" + suffix,
                    name="SIM · реле " + suffix,
                    house_id="e2e_house",
                    actions=[
                        {
                            "type": "device_state",
                            "device_id": "e2e_relay",
                            "state": {"on": value},
                        }
                    ],
                )
                if scenes.exists(definition.id):
                    current = scene_service.get(definition.id)
                    if (
                        current.house_id != definition.house_id
                        or current.actions != definition.actions
                    ):
                        raise BootstrapError("Conflicting demo scene")
                else:
                    scene_service.create(definition)
            rules = AutomationRepository(session)
            rule = rule_definition()
            rule.name = "SIM · движение включает реле"
            if rules.exists(rule.id):
                current = rules.get(rule.id)
                if any(
                    getattr(current, key) != rule.model_dump()[key]
                    for key in ("house_id", "trigger", "conditions", "actions")
                ):
                    raise BootstrapError("Conflicting demo automation")
            else:
                AutomationService(rules, houses, service, bus).create(rule)


async def simulate(credentials, configuration, scenario):
    from simulator.e2e_acceptance import API, run as relay_run
    from simulator.e2e_automation import run as automation_run
    from simulator.mqtt_relay import Config
    from simulator.mqtt_motion import MotionConfig

    class DemoAPI(API):
        refresh = None

        async def request(self, method, path, body=None, **kwargs):
            result = await super().request(method, path, body, **kwargs)
            if path == "/auth/login":
                self.refresh = result["refresh_token"]
            return result

        def close(self):
            if self.refresh:
                request = urllib.request.Request(
                    self.base + "/auth/logout",
                    data=json.dumps({"refresh_token": self.refresh}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.opener.open(request, timeout=5) as response:
                    if response.status != 204:
                        raise RuntimeError("Demo logout failed")

    relay = Config(
        host="mosquitto",
        port=8883,
        username="relay-simulator",
        password=configuration["relay"],
        ca_file="/run/mqtt/ca.crt",
        house_id="e2e_house",
        device_id="e2e_relay",
        client_id="demo-relay-simulator",
    )
    motion = MotionConfig(
        host="mosquitto",
        port=8883,
        username="motion-simulator",
        password=configuration["motion"],
        ca_file="/run/mqtt/ca.crt",
        client_id="demo-motion-simulator",
    )
    api = DemoAPI("http://core:8000")
    try:
        if scenario == "relay":
            await relay_run(
                relay, api, credentials["email"], credentials["password"], 30
            )
        else:
            await automation_run(
                relay, motion, api, credentials["email"], credentials["password"], 30
            )
    finally:
        await asyncio.to_thread(api.close)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "relay", "automation"))
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        if os.environ.get("KZHOME_DEMO_ENABLED") != "1":
            raise ValueError("Explicit demo opt-in required")
        credentials = json.load(sys.stdin)  # Operator pipe, never CLI args/logs.
        if args.action == "bootstrap":
            from app.core import Settings
            from app.db import create_db_engine
            from app.services.readiness_service import ReadinessService

            engine = create_db_engine(Settings.from_env())
            try:
                if ReadinessService(engine).check(True):
                    raise ValueError("Migrations required")
                bootstrap_demo(engine, credentials["email"], credentials["password"])
            finally:
                engine.dispose()
        else:
            configuration = credentials["simulation"]
            asyncio.run(simulate(credentials, configuration, args.action))
    except Exception:
        print(
            "FAIL: demo prerequisites, conflicting data, credentials or protocol evidence; details withheld"
        )
        raise SystemExit(1) from None
    print(
        "PASS: demo "
        + args.action
        + "; software simulation only, not physical hardware"
    )


if __name__ == "__main__":
    main()
