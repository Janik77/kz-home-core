"""Deterministic stalls at admission, authorization and persisted publication."""

import asyncio
from datetime import UTC, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from app.models import (
    SceneRunORM,
    SceneRunActionORM,
    SceneRunReceiptORM,
    DeviceCommandORM,
)
from app.services.scene_run_service import SceneRunService
from app.repositories.command_repository import CommandRepository
from test_command_lifecycle import fixture
from test_scene_runs import create


@pytest.mark.parametrize(
    "boundary", ["before_claim", "authorize", "after_commit", "gateway"]
)
def test_scene_deadline_stall_cannot_publish(tmp_path, monkeypatch, boundary):
    async def check():
        async with fixture(tmp_path, monkeypatch) as h:
            response, _ = await create(h)
            run_id = response.json()["id"]
            before = len(h.wire.published)
            with Session(h.engine, expire_on_commit=False) as session:
                run = session.get(SceneRunORM, run_id)
                run.dispatch_deadline = run.created_at + timedelta(seconds=5)
                session.commit()
                clock = [run.created_at.replace(tzinfo=UTC)]
                deadline = run.dispatch_deadline.replace(tzinfo=UTC)
                late = deadline + timedelta(milliseconds=165)
                monkeypatch.setattr(
                    "app.transports.mqtt.command_clock",
                    SimpleNamespace(now=lambda: clock[0]),
                )
                service = SceneRunService(session, h.gateway, now=lambda: clock[0])
                if boundary == "before_claim":
                    get = service.get

                    def stall(*args):
                        result = get(*args)
                        clock[0] = late
                        return result

                    monkeypatch.setattr(service, "get", stall)
                elif boundary in {"authorize", "gateway"}:
                    authorize = h.gateway.authorize_command
                    calls = []

                    async def stall(*args):
                        await authorize(*args)
                        calls.append(True)
                        if boundary == "authorize" or len(calls) == 2:
                            clock[0] = late

                    monkeypatch.setattr(h.gateway, "authorize_command", stall)
                else:
                    save = CommandRepository.save

                    def stall(repo):
                        has_link = any(
                            isinstance(row, SceneRunReceiptORM)
                            for row in repo.session.new
                        )
                        save(repo)
                        if has_link:
                            clock[0] = late

                    monkeypatch.setattr(CommandRepository, "save", stall)
                result = await service.dispatch(
                    "fixture_house", run_id, 0, run.requested_by
                )
                assert len(h.wire.published) == before
                assert result.status != "succeeded"
            with Session(h.engine) as inspect:
                action = inspect.get(SceneRunActionORM, (run_id, 0))
                assert action.claimed is (boundary != "before_claim")
                rows = inspect.query(DeviceCommandORM).all()
                if boundary in {"after_commit", "gateway"}:
                    assert len(rows) == 1 and rows[0].status == "timed_out"
                    assert (
                        rows[0].expires_at == run.dispatch_deadline
                        or rows[0].expires_at < run.dispatch_deadline
                    )
                else:
                    assert rows == []

    asyncio.run(check())
