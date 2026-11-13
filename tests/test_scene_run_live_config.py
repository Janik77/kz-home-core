import json
from pathlib import Path
import stat
import pytest


def test_lab_config_is_private_exclusive_and_roundtrips(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    from scene_run_live import write_configuration

    path = tmp_path / "fixture.json"
    write_configuration(path, {"fixture": "synthetic-test-value"})
    assert json.loads(path.read_text()) == {"fixture": "synthetic-test-value"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_configuration(path, {"replacement": True})
    assert json.loads(path.read_text()) == {"fixture": "synthetic-test-value"}
