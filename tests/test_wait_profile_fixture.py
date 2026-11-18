"""The historical CLI cannot contaminate the current restore-policy interpreter."""

import sys
from tools import restore_policy
from wait_profile_acceptance import historical_backup


def test_historical_backup_cli_cannot_leak_module_or_sys_path(tmp_path):
    old_core = tmp_path / "old-core"
    scripts = old_core / "tools"
    scripts.mkdir(parents=True)
    (scripts / "durable_software.py").write_text("""
import sys, types
from pathlib import Path
sys.path.insert(0, "historical-import-poison")
sys.modules["tools.restore_policy"] = types.ModuleType("historical_policy_without_new_verifier")
destination = Path(sys.argv[sys.argv.index("--destination") + 1])
destination.mkdir()
(destination / "executed").write_text("child process")
""")
    paths = list(sys.path)
    current_module = sys.modules["tools.restore_policy"]
    verifier = restore_policy.verified_metadata_migration
    target = tmp_path / "backup"
    historical_backup(old_core, tmp_path / "source", target)
    assert (target / "executed").read_text() == "child process"
    assert sys.path == paths
    assert sys.modules["tools.restore_policy"] is current_module
    assert restore_policy.verified_metadata_migration is verifier
