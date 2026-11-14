# ruff: noqa: F811
"""Real PostgreSQL creation/claim races; transport is explicitly a software recorder."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from uuid import uuid4

from alembic import command
from sqlalchemy.orm import Session
from sqlalchemy import inspect
import pytest

from app.models import SceneORM, SceneRunORM, SceneRunReceiptORM
from app.repositories import UserRepository
from app.schemas.scene_runs import SceneRunCreate
from app.services.auth_service import UserService
from app.services.scene_run_service import SceneRunService
from test_definition_postgresql import pg_definitions  # noqa: F401
from test_schema import migration_config


def test_postgresql_same_key_creation_and_dispatch_have_one_durable_winner(
    pg_definitions,
):
    with Session(pg_definitions) as session:
        user = UserService(UserRepository(session)).create(
            "runner@example.test", "scene-test-password"
        )
        user_id = user.id
        session.get(SceneORM, "scene").actions = [
            {"device_id": "fixture", "state": {"on": True}}
        ]
        session.commit()
    barrier = Barrier(2)
    key = str(uuid4())
    data = SceneRunCreate(scene_id="scene", expected_revision=1)

    def create(_):
        with Session(pg_definitions, expire_on_commit=False) as session:
            barrier.wait(timeout=10)
            return SceneRunService(session).create("a", user_id, key, data).id

    with ThreadPoolExecutor(2) as workers:
        ids = list(workers.map(create, range(2)))
    assert ids[0] == ids[1]
    run_id = ids[0]
    calls = []
    lock = Lock()

    class Wire:
        connected = True

        async def authorize_command(self, *args):
            pass

        async def send_command(self, *args, **kwargs):
            with lock:
                calls.append(kwargs["command_id"])
            # Verify the receipt and link are committed and visible BEFORE publish.
            with Session(pg_definitions) as observer:
                assert (
                    observer.get(SceneRunReceiptORM, (run_id, 0)).command_id == calls[0]
                )

    wire = Wire()
    barrier = Barrier(2)

    def dispatch(_):
        with Session(pg_definitions, expire_on_commit=False) as session:
            barrier.wait(timeout=10)
            return asyncio.run(
                SceneRunService(session, wire).dispatch("a", run_id, 0, user_id)
            )

    with ThreadPoolExecutor(2) as workers:
        list(workers.map(dispatch, range(2)))
    assert len(calls) == 1
    with Session(pg_definitions) as session:
        recovered = SceneRunService(session).get("a", run_id)
        assert recovered.actions[0].receipt.command_id == calls[0]
        assert recovered.status == "running"
        assert session.query(SceneRunORM).count() == 1
    # Nonempty history is protected against destructive schema rollback.
    with pg_definitions.begin() as connection:
        schema = pg_definitions.get_execution_options()["schema_translate_map"][None]
        from sqlalchemy import text

        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        with pytest.raises(RuntimeError, match="history is not empty"):
            command.downgrade(migration_config(connection), "0009_definition_revisions")
        assert "scene_runs" in inspect(connection).get_table_names(schema=schema)


def test_postgresql_action_lock_wait_rechecks_live_database_deadline(pg_definitions):
    """Freeze application time; real database time expires while waiting for lock."""
    from threading import Event
    from time import monotonic, sleep
    from sqlalchemy import func, select, text
    from app.models import SceneRunActionORM, DeviceCommandORM
    from app.services.command_service import utc

    with Session(pg_definitions, expire_on_commit=False) as session:
        user = UserService(UserRepository(session)).create(
            "deadline@example.test", "deadline-test-password"
        )
        user_id = user.id
        session.get(SceneORM, "scene").actions = [
            {"device_id": "fixture", "state": {"on": True}}
        ]
        session.commit()
        run = SceneRunService(session).create(
            "a",
            user_id,
            str(uuid4()),
            SceneRunCreate(scene_id="scene", expected_revision=1),
        )
    ready = Event()
    worker_pid = []

    def dispatch():
        with Session(pg_definitions, expire_on_commit=False) as session:
            worker_pid.append(session.scalar(text("SELECT pg_backend_pid()")))
            service = SceneRunService(session, now=lambda: utc(run.created_at))
            original = service.get

            def signal_ready(*args):
                result = original(*args)
                ready.set()
                return result

            service.get = signal_ready
            return asyncio.run(service.dispatch("a", run.id, 0, user_id))

    with Session(pg_definitions) as locker, ThreadPoolExecutor(1) as workers:
        locker.scalar(
            select(SceneRunActionORM)
            .where(SceneRunActionORM.run_id == run.id)
            .with_for_update()
        )
        future = workers.submit(dispatch)
        try:
            assert ready.wait(10)
            limit = monotonic() + 10
            while True:
                with Session(pg_definitions) as observer:
                    waiting = observer.scalar(
                        text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"
                        ),
                        {"pid": worker_pid[0]},
                    )
                if waiting:
                    break
                assert monotonic() < limit, "worker did not block at action row lock"
                sleep(0.01)
            # Controlled fixture deadline elapses under the proven real row lock.
            with Session(pg_definitions) as observer:
                observer.execute(
                    SceneRunORM.__table__.update()
                    .where(SceneRunORM.id == run.id)
                    .values(dispatch_deadline=func.clock_timestamp())
                )
                observer.commit()
        finally:
            locker.rollback()
        result = future.result(timeout=10)
    assert result.status == "expired"
    with Session(pg_definitions) as session:
        assert not session.get(SceneRunActionORM, (run.id, 0)).claimed
        assert session.query(DeviceCommandORM).count() == 0
        assert session.query(SceneRunReceiptORM).count() == 0


def test_postgresql_server_distinct_keys_have_one_active_scene(pg_definitions):
    """Two concurrent browser intentions must not create parallel executors."""
    from app.core.errors import ConflictError
    from app.models import HouseMembershipORM

    with Session(pg_definitions) as session:
        user = UserService(UserRepository(session)).create(
            "server-race@example.test", "scene-race-test-password"
        )
        user_id = user.id
        session.add(
            HouseMembershipORM(
                id=str(uuid4()), user_id=user_id, house_id="a", role="owner"
            )
        )
        session.get(SceneORM, "scene").actions = [
            {"device_id": "fixture", "state": {"on": True}}
        ]
        session.commit()
    barrier = Barrier(2)

    class Wire:
        connected = True

        async def authorize_command(self, *args):
            pass

    def create(_):
        with Session(pg_definitions, expire_on_commit=False) as session:
            barrier.wait(timeout=10)
            try:
                result = asyncio.run(
                    SceneRunService(session, Wire()).create_server(
                        "a",
                        user_id,
                        str(uuid4()),
                        SceneRunCreate(scene_id="scene", expected_revision=1),
                        str(uuid4()),
                    )
                )
                return result.id
            except ConflictError:
                return "conflict"

    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(create, range(2)))
    assert results.count("conflict") == 1
    with Session(pg_definitions) as session:
        assert session.query(SceneRunORM).count() == 1
