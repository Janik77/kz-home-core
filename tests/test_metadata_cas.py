# ruff: noqa: F811
"""Atomic metadata edits, independent from device telemetry and house schedules."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tests.test_api import client, db_settings, seeded_client  # noqa: F401


@pytest.mark.parametrize(
    "kind,entity",
    [
        ("houses", "home1"),
        ("floors", "floor1"),
        ("rooms", "living"),
        ("devices", "relay1"),
    ],
)
def test_metadata_cas_aba_and_legacy_refusal(seeded_client, kind, entity):
    # Seed names vary; select actual existing resource rather than inventing IDs.
    original = seeded_client.get(f"/{kind}").json()[0]
    url = f"/{kind}/{original['id']}"
    version = original["metadata_revision"]
    assert seeded_client.patch(url, json={"name": "legacy"}).status_code == 422
    for invalid in (True, 0, "1", 1.5, 10**100):
        assert (
            seeded_client.patch(
                url, json={"name": "bad", "expected_metadata_revision": invalid}
            ).status_code
            == 422
        )
    first = seeded_client.patch(
        url, json={"name": "renamed", "expected_metadata_revision": version}
    )
    assert first.status_code == 200, first.text
    assert first.json()["metadata_revision"] == version + 1
    stale = seeded_client.patch(
        url, json={"name": "overwrite", "expected_metadata_revision": version}
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "metadata_revision_conflict"
    back = seeded_client.patch(
        url, json={"name": original["name"], "expected_metadata_revision": version + 1}
    )
    assert back.status_code == 200
    assert back.json()["metadata_revision"] == version + 2
    assert (
        seeded_client.patch(
            url, json={"name": "ABA", "expected_metadata_revision": version}
        ).status_code
        == 409
    )
    noop = seeded_client.patch(
        url, json={"name": original["name"], "expected_metadata_revision": version + 2}
    )
    assert noop.json()["metadata_revision"] == version + 3


def test_house_timezone_independent_and_combined_cas(seeded_client):
    h = seeded_client.get("/houses").json()[0]
    url = "/houses/" + h["id"]
    tz = seeded_client.patch(
        url,
        json={"timezone": "UTC", "expected_timezone_revision": h["timezone_revision"]},
    )
    assert tz.status_code == 200
    assert tz.json()["metadata_revision"] == h["metadata_revision"]
    combined = {
        "name": "combined",
        "timezone": "Asia/Almaty",
        "expected_timezone_revision": tz.json()["timezone_revision"],
    }
    assert seeded_client.patch(url, json=combined).status_code == 422
    combined["expected_metadata_revision"] = h["metadata_revision"]
    result = seeded_client.patch(url, json=combined)
    assert result.status_code == 200, result.text
    assert result.json()["metadata_revision"] == h["metadata_revision"] + 1
    assert result.json()["timezone_revision"] == tz.json()["timezone_revision"] + 1


def test_metadata_does_not_mutate_commands_or_state(seeded_client, db_settings):
    d = seeded_client.get("/devices").json()[0]
    result = seeded_client.patch(
        "/devices/" + d["id"],
        json={
            "name": "only name",
            "expected_metadata_revision": d["metadata_revision"],
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["state"] == d["state"]
    assert result.json()["online"] == d["online"]
    engine = create_engine(db_settings.database_url)
    from app.models import DeviceCommandORM

    with Session(engine) as session:
        assert session.scalar(select(DeviceCommandORM)) is None
    engine.dispose()


def test_unrelated_parent_revision_does_not_change_device_revision(seeded_client):
    d = seeded_client.get("/devices").json()[0]
    room = seeded_client.get("/rooms/" + d["room_id"]).json()
    result = seeded_client.patch(
        "/rooms/" + room["id"],
        json={
            "name": "room renamed",
            "expected_metadata_revision": room["metadata_revision"],
        },
    )
    assert result.status_code == 200
    assert (
        seeded_client.get("/devices/" + d["id"]).json()["metadata_revision"]
        == d["metadata_revision"]
    )


def test_heartbeat_refreshes_stale_identity_map_without_lost_metadata(
    seeded_client, db_settings
):
    import asyncio
    from datetime import datetime, UTC
    from app.events import EventBus
    from app.repositories import DeviceRepository, RoomRepository
    from app.services.device_service import DeviceService

    device = seeded_client.get("/devices").json()[0]
    engine = create_engine(db_settings.database_url)
    with Session(engine, expire_on_commit=False) as stale_session:
        repo = DeviceRepository(stale_session)
        cached = repo.get(device["id"])
        old_revision = cached.metadata_revision
        result = seeded_client.patch(
            "/devices/" + device["id"],
            json={
                "metadata": {"custom": "must survive"},
                "expected_metadata_revision": old_revision,
            },
        )
        assert result.status_code == 200
        assert cached.metadata_revision == old_revision
        service = DeviceService(repo, RoomRepository(stale_session), EventBus())
        asyncio.run(
            service.update_status(
                repo.house_id(device["id"]), device["id"], False, datetime.now(UTC)
            )
        )
        actual = service.get(device["id"])
        assert actual.metadata["custom"] == "must survive"
        assert actual.metadata["last_seen"]
        assert actual.metadata_revision == old_revision + 1
        assert actual.numeric_observed_at == {}
    engine.dispose()


def test_internal_writer_cannot_bypass_metadata_revision(seeded_client, db_settings):
    from app.repositories import HouseRepository
    from app.core.errors import InvalidReferenceError

    engine = create_engine(db_settings.database_url)
    with Session(engine) as session:
        repo = HouseRepository(session)
        h = repo.list()[0]
        with pytest.raises(InvalidReferenceError, match="expected_metadata_revision"):
            repo.update(h.id, {"name": "silent legacy bypass"})
        assert repo.get(h.id).metadata_revision == 1
    engine.dispose()
