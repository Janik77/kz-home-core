"""Actual Python process crash/restart; migrated SQLite, real Core, fake MQTT wire."""

import os
from pathlib import Path
import subprocess
import sys


def test_numeric_checkpoint_survives_process_crash(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": "sqlite:///:memory:",
        "PYTHONPATH": os.pathsep.join((str(root), str(root / "tests"))),
    }
    helper = r"""
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, UTC
import pytest, httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.core import Settings
from app.models import AutomationORM, DeviceORM, SceneRunORM
from app.transports.mqtt_client import FakeMQTTClient
from test_command_lifecycle import fixture, Harness
from test_numeric_thresholds import sensor, numeric_rule, sample
from test_durable_automations import wait, runs
from test_physical_acceptance import LocalAPI

path=Path(sys.argv[1]); phase=sys.argv[2]
async def run():
    if phase == 'crash':
        async with fixture(path, pytest.MonkeyPatch(), extra_software_devices=(("fixture_house","numeric_sensor"),)) as h:
            await sensor(h)
            rule=await numeric_rule(h)
            old=await sample(h,120)
            stamp=await sample(h,90)
            await wait(h,rule,lambda r:r['actions'] and r['actions'][0]['receipt'])
            with Session(h.engine) as db:
                assert not db.get(AutomationORM,rule['id']).threshold_state['armed']
                checkpoint=db.get(AutomationORM,rule['id']).threshold_state
                marks=db.get(DeviceORM,'numeric_sensor').numeric_watermarks
            (path/'metadata.json').write_text(json.dumps(dict(rule=rule,old=old,stamp=stamp,checkpoint=checkpoint,marks=marks)))
            os._exit(17)  # No lifespan cleanup: pending command must not be replayed.
    from app.main import create_app
    metadata=json.loads((path/'metadata.json').read_text())
    settings=Settings(database_url=f"sqlite:///{path/'physical.db'}",app_env='test',mqtt_enabled=True,mqtt_host='unused',mqtt_client_id='offline-core',mqtt_software_devices=(("fixture_house","software_relay"),("fixture_house","numeric_sensor")))
    engine=create_engine(settings.database_url)
    wire=FakeMQTTClient(); app=create_app(settings,run_simulator=False,mqtt_client=wire)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            api=LocalAPI(client)
            api.token=(await api.request('POST','/auth/login',{'email':'owner@example.test','password':'fixture-human-password'}))['access_token']
            h=Harness(api,wire,engine)
            await asyncio.sleep(.15)
            with Session(engine) as db:
                assert db.get(AutomationORM,metadata['rule']['id']).threshold_state==metadata['checkpoint']
                assert db.get(DeviceORM,'numeric_sensor').numeric_watermarks==metadata['marks']
            assert not wire.published
            await sample(h,120,stamp=metadata['old'])
            await sample(h,90,stamp=metadata['stamp'])
            await sample(h,80)
            assert len(await runs(h,metadata['rule']))==1
            assert not wire.published
            with Session(engine) as db:
                assert not db.get(AutomationORM,metadata['rule']['id']).threshold_state['armed']
                assert db.query(SceneRunORM).filter_by(scene_id=metadata['rule']['id']).count()==1
    engine.dispose()
asyncio.run(run())
"""
    child = tmp_path / "restart_child.py"
    child.write_text(helper, encoding="utf-8")
    first = subprocess.run(
        [sys.executable, str(child), str(tmp_path), "crash"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert first.returncode == 17, first.stdout + first.stderr
    second = subprocess.run(
        [sys.executable, str(child), str(tmp_path), "restart"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert second.returncode == 0, second.stdout + second.stderr
