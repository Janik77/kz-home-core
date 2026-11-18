"""Real process restart with injected schedule clock; SQLite and FakeMQTT only."""

import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize("crash_point", ["after_cursor", "after_publish"])
def test_schedule_process_restart_never_catches_up_or_replays(tmp_path, crash_point):
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
from datetime import datetime, UTC, timedelta
import pytest, httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.core import Settings
from app.models import AutomationORM, HouseORM, DeviceORM, SceneRunORM
from app.transports.mqtt_client import FakeMQTTClient
from app.services.automation_runs import AutomationRunService
from app.services.schedule_planner import SchedulePlanner
from app.services.scene_executor import SceneExecutor
from test_command_lifecycle import fixture, Harness
from test_house_schedules import configure
from test_durable_automations import wait
from test_physical_acceptance import LocalAPI

path=Path(sys.argv[1]); phase=sys.argv[2]; crash_point=sys.argv[3]
async def run():
    if phase == 'crash':
        async with fixture(path, pytest.MonkeyPatch()) as h:
            boundary, rule=await configure(h)
            with Session(h.engine) as db:
                db.get(DeviceORM,'software_relay').state_observed_at=datetime.now(UTC)
                db.commit()
            clock=[boundary-timedelta(seconds=1)]
            factory=lambda:Session(h.engine)
            executor=SceneExecutor(factory,h.gateway)
            planner=SchedulePlanner(factory,h.gateway,executor,now=lambda:clock[0])
            await planner.tick()
            def crash():
                with factory() as db:
                    checkpoint=db.get(AutomationORM,rule['id']).schedule_state
                    cursor=db.get(HouseORM,'fixture_house').schedule_cursor.isoformat()
                    count=db.query(SceneRunORM).filter_by(scene_id=rule['id']).count()
                (path/'metadata.json').write_text(json.dumps(dict(boundary=boundary.isoformat(),rule=rule,checkpoint=checkpoint,cursor=cursor,count=count)))
                os._exit(17)
            if crash_point == 'after_cursor':
                async def crash_before_admission(*args,**kwargs): crash()
                AutomationRunService.admit=crash_before_admission
            clock[0]=boundary+timedelta(seconds=1)
            await planner.tick()
            await wait(h,rule,lambda r:r['actions'] and r['actions'][0]['receipt'])
            assert len(h.wire.published)==1
            crash()
    from app.main import create_app
    metadata=json.loads((path/'metadata.json').read_text())
    boundary=datetime.fromisoformat(metadata['boundary'])
    settings=Settings(database_url=f"sqlite:///{path/'physical.db'}",app_env='test',mqtt_enabled=True,mqtt_host='unused',mqtt_client_id='offline-core',mqtt_software_devices=(("fixture_house","software_relay"),))
    engine=create_engine(settings.database_url)
    factory=lambda:Session(engine)
    wire=FakeMQTTClient(); app=create_app(settings,run_simulator=False,mqtt_client=wire)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            api=LocalAPI(client)
            api.token=(await api.request('POST','/auth/login',{'email':'owner@example.test','password':'fixture-human-password'}))['access_token']
            h=Harness(api,wire,engine)
            clock=[boundary+timedelta(seconds=2)]
            executor=SceneExecutor(factory,h.gateway)
            planner=SchedulePlanner(factory,h.gateway,executor,now=lambda:clock[0])
            with factory() as db:
                assert db.get(AutomationORM,'scheduled').schedule_state==metadata['checkpoint']
                assert db.get(HouseORM,'fixture_house').schedule_cursor.isoformat()==metadata['cursor']
            await planner.tick() # New process starts inside scheduled minute: no catch-up.
            clock[0]+=timedelta(seconds=1)
            await planner.tick()
            if crash_point=='after_publish':
                with factory() as db:
                    await AutomationRunService(db,h.gateway,now=lambda:clock[0]).admit('scheduled',None,executor,occurrence=boundary)
            clock[0]=boundary+timedelta(days=1,seconds=2)
            await planner.tick() # Long outage also cannot claim a missed occurrence.
            await asyncio.sleep(.1)
            with factory() as db:
                assert db.query(SceneRunORM).filter_by(scene_id='scheduled').count()==metadata['count']
                assert db.get(AutomationORM,'scheduled').schedule_state==metadata['checkpoint']
            assert not wire.published
            await executor.stop()
    engine.dispose()
asyncio.run(run())
"""
    child = tmp_path / "schedule_restart_child.py"
    child.write_text(helper, encoding="utf-8")
    for phase, expected in [("crash", 17), ("restart", 0)]:
        result = subprocess.run(
            [sys.executable, str(child), str(tmp_path), phase, crash_point],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == expected, result.stdout + result.stderr
