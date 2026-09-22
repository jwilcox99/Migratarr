import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from runtime_config import load_config
from storage_targets import load_targets, parse_targets
from migratarr_validation.manifest_parity import compare_manifests


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/manifest_before_target_ids.py"


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ManifestParityTests(unittest.TestCase):
    def setUp(self):
        self.runtime = load_config(ROOT / "config/runtime.example.json", environ={})
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name) / "20260101T000000Z"
        self.run_dir.mkdir()

        fields = ["media_type", "title", "current", "recommended", "status",
                  "transfer_type", "source_disk", "target_disk", "size_gb"]
        rows = [
            dict(media_type="Movie", title="Film", current="Common", recommended="Archive",
                 status="READY_FOR_REVIEW", transfer_type="same-disk",
                 source_disk="media01", target_disk="media05", size_gb="10"),
            dict(media_type="TV", title="Show", current="Current", recommended="Rare",
                 status="BLOCKED", transfer_type="same-disk",
                 source_disk="media01", target_disk="media02", size_gb="5"),
        ]
        move_plan = self.run_dir / "move_plan.csv"
        with move_plan.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

        metadata = self.run_dir / "metadata.json"
        metadata.write_text(json.dumps({"git": {"commit": "deadbeef"}}))

        sums = self.run_dir / "SHA256SUMS"
        sums.write_text(
            f"{_sha256(move_plan)}  move_plan.csv\n"
            f"{_sha256(metadata)}  metadata.json\n"
        )

        data = json.loads((ROOT / "config/storage-targets.example.json").read_text())
        data["targets"].append(dict(id="media05", name="Fifth", path="/mnt/nas/media05",
                                    enabled=True, media_types=["Movie"],
                                    minimum_free_space_gb=50))
        self.targets = parse_targets(data)

    def test_manifest_bytes_are_unchanged(self):
        result = compare_manifests(BASELINE, self.run_dir, self.runtime, self.targets)
        self.assertTrue(result["byte_identical"])
        self.assertEqual(result["baseline_csv_sha256"], result["candidate_csv_sha256"])
        self.assertEqual(result["baseline_metadata_sha256"], result["candidate_metadata_sha256"])

    def test_candidate_report_includes_fifth_target_the_baseline_cannot_see(self):
        result = compare_manifests(BASELINE, self.run_dir, self.runtime, self.targets)
        self.assertIn("media05", result["candidate_report"])
        self.assertNotIn("media05", result["baseline_report"])


if __name__ == "__main__":
    unittest.main()
