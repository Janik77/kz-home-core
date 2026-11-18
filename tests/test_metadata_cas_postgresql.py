# ruff: noqa: F811
"""Actual SQL CAS contention; dedicated CI PostgreSQL only."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.core.errors import MetadataRevisionConflict
from app.models import HouseORM, FloorORM, RoomORM, DeviceORM
from app.repositories import (
    HouseRepository,
    FloorRepository,
    RoomRepository,
    DeviceRepository,
)
from test_definition_postgresql import pg_definitions  # noqa: F401


@pytest.mark.parametrize(
    "repository,model,identifier",
    [
        (HouseRepository, HouseORM, "a"),
        (FloorRepository, FloorORM, "floor"),
        (RoomRepository, RoomORM, "room"),
        (DeviceRepository, DeviceORM, "fixture"),
    ],
)
def test_two_metadata_editors_have_one_atomic_winner(
    pg_definitions, repository, model, identifier
):
    barrier = Barrier(2)

    def edit(index):
        with Session(pg_definitions, expire_on_commit=False) as session:
            session.execute(text("SET lock_timeout = '5s'"))
            repo = repository(session)
            original = repo.get(identifier)
            assert original.metadata_revision == 1
            barrier.wait(timeout=5)
            try:
                saved = repo.update(
                    identifier,
                    {"name": f"Editor {index}", "expected_metadata_revision": 1},
                )
                return "saved", saved.name, saved.metadata_revision
            except MetadataRevisionConflict:
                return "conflict", None, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, [0, 1]))
    assert sorted(x[0] for x in results) == ["conflict", "saved"]
    winner = next(x for x in results if x[0] == "saved")
    assert winner[2] == 2
    with Session(pg_definitions) as session:
        current = session.get(model, identifier)
        assert current.metadata_revision == 2 and current.name == winner[1]
