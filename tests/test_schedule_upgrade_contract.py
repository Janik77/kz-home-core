"""Pure contract tests; real PG/TLS preservation belongs to the CI source matrix."""

from unittest.mock import Mock
from tools import durable_software as manager


def test_source52_exact_contract_and_schedule_pins():
    assert manager.SUPPORTED_UPGRADE_SOURCES[manager.SOURCE52_TOOL_SHA] == (
        "1ae52afa22431943064b5e85313f671b073b9458",
        "74b7238f10a9e56cbc600f9cac21b29a5c15b46e",
        "0014_numeric_thresholds",
    )
    assert len(manager.SUPPORTED_UPGRADE_SOURCES) == 5
    assert manager.CORE_SHA == "fc232035f78c99171fd72ac82c78eb1b5bb657fa"
    assert manager.WEB_SHA == "31dd1029173af6c7ff4ed4e9bf0c80679c185b3b"
    assert manager.EXPECTED_MIGRATION == "0017_metadata_cas"


def test_source52_fingerprint_preserves_numeric_fields_and_latch():
    profile = Mock()
    profile.query.side_effect = [
        "automations\ndevices",
        "id\nthreshold_state",
        "a",
        "id\nstate\nnumeric_observed_at\nnumeric_watermarks",
        "b",
    ]
    assert manager.legacy_fingerprints(profile, "0014_numeric_thresholds") == {
        "automations": "a",
        "devices": "b",
    }
    queries = [call.args[0] for call in profile.query.call_args_list]
    assert "threshold_state" not in queries[1]
    assert '"threshold_state"' in queries[2]
    assert "numeric_watermarks" not in queries[3]
    assert (
        '"numeric_observed_at"' in queries[4] and '"numeric_watermarks"' in queries[4]
    )
    assert "schedule_state" in queries[1] and "timezone_revision" in queries[3]


def test_source42_house_fingerprint_uses_only_original_columns():
    profile = Mock()
    profile.query.side_effect = ["houses", "kept"]
    assert manager.legacy_fingerprints(profile) == {"houses": "kept"}
    query = profile.query.call_args_list[1].args[0]
    assert "id, name, created_at, updated_at" in query
    assert "timezone" not in query


def test_source54_preserves_schedule_columns_and_exact_contract():
    assert manager.SUPPORTED_UPGRADE_SOURCES[manager.SOURCE54_TOOL_SHA] == (
        "45b63628829580bc564ff2490af645951926e82e",
        "96c953f40f3a8eff464e2b57b5e7817456569206",
        "0015_house_schedules",
    )
    profile = Mock()
    profile.query.side_effect = [
        "houses\nautomations",
        "id\ntimezone\ntimezone_revision\nschedule_cursor",
        "a",
        "id\nschedule_state\nthreshold_state",
        "b",
    ]
    assert manager.legacy_fingerprints(profile, "0015_house_schedules") == {
        "houses": "a",
        "automations": "b",
    }
    queries = [call.args[0] for call in profile.query.call_args_list]
    assert "timezone_revision" not in queries[1] and "schedule_state" not in queries[3]
    assert '"schedule_cursor"' in queries[2] and '"threshold_state"' in queries[4]
    assert "dispatch_before" in queries[1] and "scene_run_waits" in queries[0]
