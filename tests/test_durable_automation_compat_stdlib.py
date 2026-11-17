import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from durable_software import legacy_fingerprints, validate_migrated_metadata


class CompatibilityTests(unittest.TestCase):
    def test_fingerprints_preserve_original_columns(self):
        profile = Mock()
        profile.query.side_effect = [
            "automations\ndevices\nscene_runs\nrooms",
            "a",
            "b",
            "c",
            "d",
        ]
        self.assertEqual(
            legacy_fingerprints(profile),
            dict(automations="a", devices="b", scene_runs="c", rooms="d"),
        )
        queries = [call.args[0] for call in profile.query.call_args_list]
        self.assertIn("actions, created_at, updated_at, revision", queries[1])
        self.assertIn("metadata, online, created_at, updated_at", queries[2])
        self.assertIn("scene_revision, created_at, dispatch_deadline", queries[3])
        self.assertNotIn("authorized_by", queries[1])
        self.assertNotIn("state_observed_at", queries[2])
        self.assertNotIn("source_kind", queries[3])
        self.assertIn('public."rooms"', queries[4])

    def test_defaults_checked_and_nondefault_refused(self):
        good = Mock()
        good.query.return_value = "0"
        validate_migrated_metadata(good)
        self.assertEqual(good.query.call_count, 4)
        queries = [call.args[0] for call in good.query.call_args_list]
        self.assertIn("source_kind IS DISTINCT FROM 'scene'", queries[0])
        self.assertIn("state_observed_at IS NOT NULL", queries[1])
        self.assertIn("authorized_by IS NOT NULL", queries[2])
        for i in range(4):
            profile = Mock()
            profile.query.side_effect = ["0"] * i + ["1"]
            with self.assertRaises(ValueError):
                validate_migrated_metadata(profile)


if __name__ == "__main__":
    unittest.main()
