# ruff: noqa: F811
"""Actual PostgreSQL wait claim serialization; no live service fallback."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from datetime import datetime, UTC, timedelta
from uuid import uuid4
from sqlalchemy.orm import Session
from app.models import (
    SceneORM,
    SceneRunORM,
    SceneRunWaitORM,
    SceneRunActionORM,
    HouseMembershipORM,
)
from app.repositories import UserRepository
from app.schemas.scene_runs import SceneRunCreate
from app.services.auth_service import UserService
from app.services.scene_run_service import SceneRunService
from app.services.durable_wait import resume_waits
from test_definition_postgresql import pg_definitions  # noqa: F401


def test_two_postgres_workers_resume_only_one_never_sent_successor(pg_definitions):
    class Wire:
        connected = True

        async def authorize_command(self, *args):
            pass

    with Session(pg_definitions, expire_on_commit=False) as db:
        user = UserService(UserRepository(db)).create(
            "wait@example.test", "wait-test-password"
        )
        db.add(
            HouseMembershipORM(
                id=str(uuid4()), user_id=user.id, house_id="a", role="owner"
            )
        )
        db.get(SceneORM, "scene").actions = [
            {"type": "device_state", "device_id": "fixture", "state": {"on": True}},
            {"type": "durable_wait", "seconds": 1},
            {"type": "device_state", "device_id": "fixture", "state": {"on": False}},
        ]
        db.commit()
        view = asyncio.run(
            SceneRunService(db, Wire()).create_server(
                "a",
                user.id,
                str(uuid4()),
                SceneRunCreate(scene_id="scene", expected_revision=1),
                str(uuid4()),
            )
        )
        row = db.get(SceneRunORM, view.id)
        now = datetime.now(UTC)
        row.wait_position = 1
        row.executor_token = None
        row.lease_until = None
        row.wait_checked_at = now - timedelta(seconds=2)
        wait = db.get(SceneRunWaitORM, (row.id, 1))
        wait.started_at = now - timedelta(seconds=2)
        wait.due_at = now - timedelta(seconds=1)
        db.commit()
    barrier = Barrier(2)
    lock = Lock()
    starts = []

    class Executor:
        def start(self, *args):
            with lock:
                starts.append(args)

    def claim(_):
        barrier.wait(timeout=10)
        asyncio.run(
            resume_waits(
                lambda: Session(pg_definitions),
                Wire(),
                Executor(),
                now=lambda: datetime.now(UTC),
            )
        )

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(claim, range(2)))
    assert len(starts) == 1
    with Session(pg_definitions) as db:
        row = db.get(SceneRunORM, view.id)
        assert row.wait_position is None and row.executor_token == starts[0][2]
        assert db.get(SceneRunWaitORM, (row.id, 1)).completed_at is not None
        assert not db.get(SceneRunActionORM, (row.id, 2)).claimed
    asyncio.run(
        resume_waits(
            lambda: Session(pg_definitions),
            Wire(),
            Executor(),
            now=lambda: datetime.now(UTC),
        )
    )
    assert len(starts) == 1
