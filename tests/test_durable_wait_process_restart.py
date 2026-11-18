"""Actual process crashes/restarts, migrated SQLite and fake MQTT; no services."""

import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize("point", ["parked", "claimed", "published"])
def test_durable_wait_process_restart_only_resumes_parked(tmp_path, point):
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
from datetime import UTC, datetime
import pytest, httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.core import Settings
from app.models import SceneRunORM, SceneRunActionORM, DeviceCommandORM
from app.transports.mqtt_client import FakeMQTTClient
from app.services.scene_executor import SceneExecutor
from test_command_lifecycle import fixture, Harness
from test_durable_wait import actions, confirmed_first
from test_server_scene_execution import start, wait_action, wait_status
from test_scene_runs import read
from test_physical_acceptance import LocalAPI
path=Path(sys.argv[1]); phase=sys.argv[2]; point=sys.argv[3]
def save(h, run):
    with Session(h.engine) as db:
        row=db.get(SceneRunORM,run['id'])
        meta={'run':run, 'hash':row.idempotency_hash,
              'commands':[c.command_id for c in db.query(DeviceCommandORM).all()]}
    (path/'wait-meta.json').write_text(json.dumps(meta))
    os._exit(17)
async def check():
    if phase=='crash':
        async with fixture(path,pytest.MonkeyPatch()) as h:
            response,_,_=await start(h,actions(4))
            assert response.status_code==201,response.text
            run=response.json()
            await confirmed_first(h,run)
            if point=='parked': save(h,run)
            if point=='claimed':
                def lost_start(self,house,run_id,token):
                    assert run_id==run['id']
                    with Session(h.engine) as db:
                        row=db.get(SceneRunORM,run_id)
                        assert row.wait_position is None and row.executor_token==token
                        assert not db.get(SceneRunActionORM,(run_id,2)).claimed
                    save(h,run)
                SceneExecutor.start=lost_start
                await asyncio.sleep(10)
                raise AssertionError('Resume was not claimed')
            await wait_action(h,run,2)
            assert len(h.wire.published)==2
            save(h,run)
    from app.main import create_app
    meta=json.loads((path/'wait-meta.json').read_text()); run=meta['run']
    settings=Settings(database_url=f"sqlite:///{path/'physical.db'}",app_env='test',mqtt_enabled=True,mqtt_host='unused',mqtt_client_id='offline-core',mqtt_software_devices=(("fixture_house","software_relay"),))
    engine=create_engine(settings.database_url); wire=FakeMQTTClient()
    app=create_app(settings,run_simulator=False,mqtt_client=wire)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            api=LocalAPI(client)
            api.token=(await api.request('POST','/auth/login',{'email':'owner@example.test','password':'fixture-human-password'}))['access_token']
            h=Harness(api,wire,engine)
            await h.message('status',{'status':'online','last_seen':datetime.now(UTC).isoformat()})
            if point=='parked':
                second=await wait_action(h,run,2)
                assert second['command_id'] not in meta['commands']
                assert len(wire.published)==1
                assert datetime.fromisoformat(second['expires_at'])-datetime.fromisoformat(second['created_at']) <= __import__('datetime').timedelta(seconds=30)
                await h.ack(second); await h.state(second,False)
                await wait_status(h,run,'succeeded')
            else:
                await asyncio.sleep(2)
                assert wire.published==[]
            view=await read(h,run)
            assert view['id']==run['id'] and view['dispatch_deadline']==run['dispatch_deadline']
            with Session(engine) as db:
                row=db.get(SceneRunORM,run['id'])
                assert row.idempotency_hash==meta['hash']
                ids=[c.command_id for c in db.query(DeviceCommandORM).all()]
                assert set(meta['commands'])<=set(ids)
                assert len(ids)==len(meta['commands'])+(point=='parked')
            await asyncio.sleep(1.2)
            assert len(wire.published)==(1 if point=='parked' else 0)
    engine.dispose()
asyncio.run(check())
"""
    child = tmp_path / "wait_child.py"
    child.write_text(helper, encoding="utf-8")
    for phase, expected in [("crash", 17), ("restart", 0)]:
        result = subprocess.run(
            [sys.executable, str(child), str(tmp_path), phase, point],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=35,
        )
        assert result.returncode == expected, result.stdout + result.stderr


def test_resumed_scene_revision_change_during_authorization_blocks_commit(
    tmp_path, monkeypatch
):
    import asyncio
    from sqlalchemy.orm import Session
    from app.models import SceneORM, DeviceCommandORM
    from test_command_lifecycle import fixture
    from test_durable_wait import actions, confirmed_first
    from test_server_scene_execution import start
    from test_scene_runs import read

    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _, body = await start(h, actions(2))
            run = response.json()
            await confirmed_first(h, run)
            original = h.gateway.authorize_command
            changed = False

            async def authorization(*args, **kwargs):
                nonlocal changed
                await original(*args, **kwargs)
                if not changed:
                    changed = True
                    with Session(h.engine) as db:
                        db.get(SceneORM, body["scene_id"]).revision += 1
                        db.commit()
                    await asyncio.sleep(0)

            monkeypatch.setattr(h.gateway, "authorize_command", authorization)
            for _ in range(80):
                await asyncio.sleep(0.1)
                view = await read(h, run)
                if changed and view["status"] not in {"running", "waiting"}:
                    break
            assert changed, "Authorization boundary never reached"
            assert len(h.wire.published) == 1
            with Session(h.engine) as db:
                assert db.query(DeviceCommandORM).count() == 1
            assert view["actions"][2]["status"] != "applied"

    asyncio.run(check())


def test_ordinary_scene_wait_does_not_depend_on_house_timezone(tmp_path, monkeypatch):
    import asyncio
    from test_command_lifecycle import fixture
    from test_durable_wait import actions, confirmed_first
    from test_server_scene_execution import start, wait_action, wait_status
    from test_scene_runs import request

    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _, _ = await start(h, actions(2))
            run = response.json()
            await confirmed_first(h, run)
            updated = await request(
                h,
                "PATCH",
                "/houses/fixture_house",
                json={"timezone": "Asia/Almaty", "expected_timezone_revision": 1},
            )
            assert updated.status_code == 200, updated.text
            second = await wait_action(h, run, 2)
            await h.ack(second)
            await h.state(second, False)
            assert (await wait_status(h, run, "succeeded"))["id"] == run["id"]
            assert len(h.wire.published) == 2

    asyncio.run(check())
