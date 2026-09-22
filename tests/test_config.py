"""Configuration schema and exact legacy-layout characterization."""

import ast
import copy
import json
import unittest
from pathlib import Path

from migratarr_validation import MoveRequest, ValidationEngine
from migratarr_validation.config import load_policy, parse_policy
from migratarr_validation.parity import load_legacy


CONFIG = Path(__file__).resolve().parents[1] / "config" / "legacy-storage.json"


class PolicyConfigTests(unittest.TestCase):
    def test_legacy_storage_matches_planner_constants(self):
        policy = load_policy(CONFIG)
        tree, namespace = load_legacy()
        disk_assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "disk_roots"
                    for target in node.targets)
        )
        exec(compile(ast.Module(body=[disk_assignment], type_ignores=[]),
                     "build_move_plan.py", "exec"), namespace)
        self.assertEqual(policy.source_roots,
                         tuple(namespace["disk_roots"].values()))
        self.assertEqual(policy.min_free_after_bytes,
                         namespace["minimum_free_bytes"]('media01'))
        self.assertEqual(
            policy.destination_roots,
            {media: {category: tuple(paths) for category, paths in groups.items()}
             for media, groups in namespace["DESTINATION_ROOTS"].items()},
        )
        for media, prefix in (("Movie", "Movies"), ("TV", "TV")):
            for category, path in policy.category_paths[media].items():
                self.assertEqual(path, Path(prefix) / category)

    def test_rejects_invalid_or_unsafe_config(self):
        valid = json.loads(CONFIG.read_text())
        mutations = [
            ("unknown field", lambda d: d.update(extra=True)),
            ("wrong version", lambda d: d.update(schema_version=2)),
            ("boolean reserve", lambda d: d.update(min_free_after_gb=True)),
            ("negative reserve", lambda d: d.update(min_free_after_gb=-1)),
            ("duplicate disk", lambda d: d["source_roots"].append("/elsewhere/media01")),
            ("unknown destination disk", lambda d: d["destination_disks"]["Movie"]
             ["Rare"].append("missing")),
            ("path traversal", lambda d: d["category_paths"]["Movie"]
             .__setitem__("Rare", "../other")),
            ("duplicate category path", lambda d: d["category_paths"]["Movie"]
             .__setitem__("Rare", "Movies/Common")),
            ("absolute category path", lambda d: d["category_paths"]["TV"]
             .__setitem__("Rare", "/tmp/other")),
            ("reserved category", lambda d: d["category_paths"]["Movie"]
             .__setitem__("HOLD", "Movies/HOLD")),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                data = copy.deepcopy(valid)
                mutate(data)
                with self.assertRaises(ValueError):
                    parse_policy(data)

    def test_configured_category_path_changes_source_lookup(self):
        data = json.loads(CONFIG.read_text())
        data["source_roots"] = ["/virtual/disk1", "/virtual/disk2"]
        data["category_paths"] = {
            "Movie": {"Common": "Films/Common", "Library": "Films/Library"},
            "TV": {"Current": "Series/Current", "Library": "Series/Library"},
        }
        data["destination_disks"] = {
            "Movie": {"Library": ["disk2"]},
            "TV": {"Library": ["disk2"]},
        }
        data["min_free_after_gb"] = 0
        policy = parse_policy(data)
        source = Path("/virtual/disk1/Films/Common/Film")
        destination = Path("/virtual/disk2/Films/Library")
        engine = ValidationEngine(
            policy, lambda path: path in {source, destination},
            lambda _: 1024, lambda _: 100 * 1024**3,
        )
        request = MoveRequest("Movie", 1, "Film", "/logical/Film",
                              "Common", "Library", confidence="high")
        plan = engine.evaluate(request)
        self.assertEqual(plan["source_path"], str(source))
        self.assertEqual(plan["target_path"], str(destination / "Film"))
        self.assertEqual(plan["status"], "READY_FOR_REVIEW")
        data["min_free_after_gb"] = 101
        strict_engine = ValidationEngine(
            parse_policy(data), lambda path: path in {source, destination},
            lambda _: 1024, lambda _: 100 * 1024**3,
        )
        self.assertEqual(strict_engine.evaluate(request)["blockers"],
                         "INSUFFICIENT_DESTINATION_SPACE")


if __name__ == "__main__":
    unittest.main()
