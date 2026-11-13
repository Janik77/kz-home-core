# ruff: noqa: F811
"""HTTP contract checks; SQLite here, real concurrent SQL in PostgreSQL suite."""

from unittest.mock import AsyncMock

import pytest

from app.services.device_service import DeviceService
from test_api import db_settings, seeded_client  # noqa: F401


@pytest.mark.parametrize(
    "path", ["/scenes/leave_home", "/automations/hall_motion_light"]
)
def test_conditional_patch_preserves_definition_and_rejects_stale(
    seeded_client, monkeypatch, path
):
    control = AsyncMock()
    monkeypatch.setattr(DeviceService, "update_state", control)
    original = seeded_client.get(path).json()
    assert original["revision"] == 1
    response = seeded_client.patch(
        path, json={"name": "New name", "expected_revision": 1}
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["revision"] == 2 and updated["name"] == "New name"
    for key in original.keys() - {"revision", "name", "updated_at"}:
        assert updated[key] == original[key]
    for patch in ({"name": "Stale"}, {"name": "New name"}, {}):
        conflict = seeded_client.patch(path, json={**patch, "expected_revision": 1})
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "definition_revision_conflict"
    assert seeded_client.get(path).json() == updated
    control.assert_not_called()


@pytest.mark.parametrize(
    "path", ["/scenes/leave_home", "/automations/hall_motion_light"]
)
def test_legacy_and_noop_edits_invalidate_drafts(seeded_client, path):
    original = seeded_client.get(path).json()
    legacy = seeded_client.patch(path, json={"name": "Legacy"})
    assert legacy.status_code == 200 and legacy.json()["revision"] == 2
    assert (
        seeded_client.patch(
            path, json={"name": "Old", "expected_revision": 1}
        ).status_code
        == 409
    )
    noop = seeded_client.patch(path, json={"expected_revision": 2})
    assert noop.status_code == 200 and noop.json()["revision"] == 3
    assert noop.json()["actions"] == original["actions"]


def test_enable_disable_invalidates_automation_draft(seeded_client):
    path = "/automations/hall_motion_light"
    for action, revision in (("disable", 2), ("enable", 3)):
        response = seeded_client.post(path + "/" + action)
        assert response.status_code == 200 and response.json()["revision"] == revision
        assert (
            seeded_client.patch(
                path, json={"name": "Stale", "expected_revision": revision - 1}
            ).status_code
            == 409
        )


@pytest.mark.parametrize("value", [0, -1, True, "1", 1.5])
@pytest.mark.parametrize(
    "path", ["/scenes/leave_home", "/automations/hall_motion_light"]
)
def test_revision_is_a_strict_positive_integer(seeded_client, path, value):
    assert (
        seeded_client.patch(
            path, json={"name": "Invalid", "expected_revision": value}
        ).status_code
        == 422
    )
    assert seeded_client.get(path).json()["revision"] == 1
