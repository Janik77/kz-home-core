# ruff: noqa: F811
"""Real concurrent inserts/activation CAS; dedicated PostgreSQL CI only."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.core.errors import ConflictError
from app.models import SceneORM, SceneRunORM, DeviceCommandORM
from app.repositories import (
    SceneRepository,
    HouseRepository,
    DeviceRepository,
    RoomRepository,
)
from app.repositories.physical_device_repository import PhysicalDeviceRepository
from app.schemas import DefinitionDuplicate, SceneActivate
from app.services.scene_service import SceneService
from app.services.device_service import DeviceService
from app.services.definition_copy import duplicate, activate
from app.events import EventBus
from test_definition_postgresql import pg_definitions  # noqa: F401


def service(session):
    bus = EventBus()
    return SceneService(
        SceneRepository(session),
        HouseRepository(session),
        DeviceService(DeviceRepository(session), RoomRepository(session), bus, None),
        bus,
    )


@pytest.mark.parametrize("operation", ["duplicate", "activate"])
def test_same_copy_identity_or_activation_has_one_atomic_winner(
    pg_definitions, operation
):
    with Session(pg_definitions) as s:
        row = s.get(SceneORM, "scene")
        row.actions = [
            {"type": "device_state", "device_id": "fixture", "state": {"on": True}}
        ]
        row.is_draft = operation == "activate"
        s.commit()
    barrier = Barrier(2)

    def work(i):
        with Session(pg_definitions) as s:
            s.execute(text("SET lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            PhysicalDeviceRepository(s).lock_houses(["a"])
            try:
                if operation == "duplicate":
                    row = duplicate(
                        service(s),
                        "scene",
                        DefinitionDuplicate(
                            id="same-copy", name="Copy", expected_revision=1
                        ),
                        "a",
                    )
                else:
                    row = activate(
                        service(s), "scene", SceneActivate(expected_revision=1), "a"
                    )
                return "saved", row.revision
            except ConflictError:
                s.rollback()
                return "conflict", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, [0, 1]))
    assert sorted(a for a, b in results) == ["conflict", "saved"]
    with Session(pg_definitions) as s:
        if operation == "duplicate":
            assert s.get(SceneORM, "same-copy").is_draft
            assert s.get(SceneORM, "scene").revision == 1
        else:
            assert s.get(SceneORM, "scene").revision == 2
            assert not s.get(SceneORM, "scene").is_draft
        assert not s.scalars(select(SceneRunORM)).all()
        assert not s.scalars(select(DeviceCommandORM)).all()
