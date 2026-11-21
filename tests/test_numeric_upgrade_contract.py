"""Read-only unit contract checks; real restore is covered by CI matrix."""

from unittest.mock import Mock
import pytest
from tools import durable_software as manager


def test_supported_sources_include_unchanged_42_and_explicit_50():
    assert manager.SUPPORTED_UPGRADE_SOURCES[manager.SOURCE_TOOL_SHA] == (
        manager.SOURCE_RUNTIME_SHA,
        manager.SOURCE_WEB_SHA,
        "0010_scene_runs",
    )
    assert manager.SUPPORTED_UPGRADE_SOURCES[manager.SOURCE50_TOOL_SHA] == (
        "f6a0826929c87b39dec7e81d22280f1218bf18f1",
        "6f8f72f86f668b53f85c13054644c80427fe9eab",
        "0013_device_automations",
    )
    assert manager.CORE_SHA == "b897b93726ef4d02e596e0b83c568df269e29bcc"
    assert manager.EXPECTED_MIGRATION == "0018_scene_drafts"


@pytest.mark.parametrize(
    "sha,dirty", [("unknown", ""), (manager.SOURCE50_TOOL_SHA, " M app/main.py")]
)
def test_source_checkout_guard_refuses_before_restore(
    monkeypatch, tmp_path, sha, dirty
):
    monkeypatch.setattr(manager, "validate_core", lambda: None)
    monkeypatch.setattr(
        manager.subprocess, "check_output", Mock(side_effect=[sha, dirty])
    )
    restore = Mock(side_effect=AssertionError("Restore must not begin"))
    monkeypatch.setattr(manager, "_restore", restore)
    with pytest.raises(ValueError, match="exact clean supported"):
        manager.upgrade(tmp_path / "backup", tmp_path / "target", tmp_path / "checkout")
    restore.assert_not_called()
    assert not (tmp_path / "target").exists()


def test_source50_fingerprint_preserves_authority_and_favorites():
    profile = Mock()
    profile.query.side_effect = [
        "automations\ndevice_favorites\ndevices\nscene_runs",
        "id\nauthorized_by\nrevision",
        "a",
        "user_id\nhouse_id\ndevice_id",
        "b",
        "id\nstate_observed_at\nstate",
        "c",
        "id\nsource_kind\nexecution_error\nexecutor_token",
        "d",
    ]
    assert manager.legacy_fingerprints(profile, "0013_device_automations") == dict(
        automations="a", device_favorites="b", devices="c", scene_runs="d"
    )
    queries = [call.args[0] for call in profile.query.call_args_list]
    assert '"authorized_by"' in queries[2]
    assert '"user_id"' in queries[4]
    assert '"state_observed_at"' in queries[6]
    assert '"executor_token"' in queries[8]
    assert (
        "threshold_state" in queries[1]
    )  # Explicit exclusion is only new0014 columns.
