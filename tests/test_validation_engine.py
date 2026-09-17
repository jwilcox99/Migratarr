"""Characterize the existing planner, then compare the standalone port.

Only function definitions from build_move_plan.py are loaded. Its module-level
Docker calls, NAS reads, and CSV writes are never executed in this test suite.
"""

import ast
import csv
import tempfile
import unittest
from pathlib import Path

from migratarr_validation import MoveRequest, ValidationEngine, ValidationPolicy
from migratarr_validation.engine import apply_override
from migratarr_validation.parity import _csv_requests, load_legacy, load_overrides, run_legacy


ROOT = Path(__file__).resolve().parents[1]
PLANNER = ROOT / "build_move_plan.py"
LEGACY_FUNCTIONS = {
    "dir_size_bytes", "gb", "target_path_for", "projected_free_for",
    "choose_destination_root", "evaluate_move", "apply_override",
}


def isolated_legacy():
    tree = ast.parse(PLANNER.read_text(encoding="utf-8"), filename=str(PLANNER))
    definitions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in LEGACY_FUNCTIONS
    ]
    namespace = {"Path": Path, "os": __import__("os")}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(PLANNER), "exec"), namespace)
    namespace.update(
        MIN_FREE_AFTER_GB=50,
        OVERRIDE_TAGS={
            "migratarr-common": "Common", "migratarr-current": "Current",
            "migratarr-library": "Library", "migratarr-rare": "Rare",
            "migratarr-archive": "Archive",
        },
        LOCK_TAG="migratarr-lock",
        PROJECTED_FREE={},
        ARR_OVERRIDES={},
    )
    return namespace


class PlannerCharacterization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.disks = tuple(base / f"media0{n}" for n in range(1, 5))
        self.source = self.disks[0] / "Movies" / "Common" / "Film"
        self.source.mkdir(parents=True)
        (self.source / "film.mkv").write_bytes(b"x" * 1024)
        self.roots = {
            "Movie": {
                "Common": (self.disks[0] / "Movies" / "Common",),
                "Rare": (self.disks[0] / "Movies" / "Rare",),
                "Library": (
                    self.disks[1] / "Movies" / "Library",
                    self.disks[2] / "Movies" / "Library",
                ),
                "Archive": (self.disks[2] / "Movies" / "Archive",),
            },
            "TV": {"Library": (self.disks[1] / "TV" / "Library",)},
        }
        for groups in self.roots.values():
            for paths in groups.values():
                for path in paths:
                    path.mkdir(parents=True, exist_ok=True)
        self.free = {self.disks[0].name: 100 * 1024**3,
                     self.disks[1].name: 200 * 1024**3,
                     self.disks[2].name: 150 * 1024**3,
                     self.disks[3].name: 120 * 1024**3}

    def disk(self, path):
        return next((root.name for root in self.disks if path == root or root in path.parents), "")

    def free_bytes(self, path):
        return self.free.get(self.disk(path) or path.name)

    def setup_engines(self, overrides=None):
        overrides = overrides or {}
        old = isolated_legacy()
        old.update(
            DESTINATION_ROOTS=self.roots,
            ARR_OVERRIDES=overrides,
            physical_disk=self.disk,
            resolve_host_source=lambda media_type, arr_path, current: self.source,
            free_bytes=self.free_bytes,
        )
        new = ValidationEngine(
            ValidationPolicy(self.roots, self.disks),
            exists=lambda path: path.exists(),
            size_bytes=old["dir_size_bytes"],
            free_bytes=self.free_bytes,
            overrides=overrides,
        )
        return old, new

    def compare_move(self, recommended="Library", confidence="high", overrides=None):
        old, new = self.setup_engines(overrides)
        args = ("Movie", 7, "Film (2020)", str(self.source), "Common",
                recommended, "81", "yes", confidence, "score")
        expected = old["evaluate_move"](*args)
        actual = new.evaluate(MoveRequest(*args))
        self.assertEqual(actual, expected)
        self.assertEqual(new.projected_free, old["PROJECTED_FREE"])
        return actual

    def test_cross_disk_chooses_most_free_root_and_reserves_space(self):
        plan = self.compare_move()
        self.assertEqual(plan["target_disk"], "media02")
        self.assertEqual(plan["status"], "READY_FOR_REVIEW")

    def test_successive_moves_use_projected_free_space(self):
        old, new = self.setup_engines()
        old["dir_size_bytes"] = lambda path: 60 * 1024**3
        new.size_bytes = old["dir_size_bytes"]
        args = ("Movie", 7, "Film", str(self.source), "Common", "Library",
                "81", "yes", "high", "score")
        old_plans = [old["evaluate_move"](*args) for _ in range(2)]
        new_plans = [new.evaluate(MoveRequest(*args)) for _ in range(2)]
        self.assertEqual(new_plans, old_plans)
        self.assertEqual([plan["target_disk"] for plan in new_plans],
                         ["media02", "media03"])
        self.assertEqual(new.projected_free, old["PROJECTED_FREE"])

    def test_same_disk_rename_and_review_warnings(self):
        plan = self.compare_move(recommended="Rare", confidence="low")
        self.assertEqual(plan["transfer_type"], "SAME_DISK_RENAME")
        self.assertEqual(
            plan["warnings"],
            "LOW_OR_UNKNOWN_REPLACEMENT_CONFIDENCE;RARE_PROMOTION_REVIEW",
        )

    def test_manual_category_override_and_conflict(self):
        override = {"Movie": {7: {"migratarr-rare"}}}
        plan = self.compare_move(overrides=override)
        self.assertEqual(plan["recommended"], "Rare")
        self.assertEqual(plan["warnings"],
                         "MANUAL_CATEGORY_OVERRIDE;RARE_PROMOTION_REVIEW")
        conflict = {"Movie": {7: {"migratarr-rare", "migratarr-archive"}}}
        plan = self.compare_move(overrides=conflict)
        self.assertEqual(plan["blockers"], "CONFLICTING_MANUAL_OVERRIDES")

    def test_lock_precedes_conflicting_categories(self):
        overrides = {"Movie": {7: {"migratarr-lock", "migratarr-rare", "migratarr-archive"}}}
        old, new = self.setup_engines(overrides)
        expected = old["apply_override"]("Movie", 7, "Common", "Library", overrides)
        actual = apply_override("Movie", 7, "Common", "Library", overrides)
        self.assertEqual(actual, expected)
        self.assertEqual(actual, ("Common", "LOCK", "migratarr-lock"))
        request = MoveRequest("Movie", 7, "Film", str(self.source), "Common", "Library")
        self.assertEqual(new.evaluate_candidates([request]), [])

    def test_insufficient_destination_space(self):
        self.free["media02"] = 49 * 1024**3
        self.free["media03"] = 48 * 1024**3
        plan = self.compare_move()
        self.assertEqual(plan["blockers"], "INSUFFICIENT_DESTINATION_SPACE")

    def test_destination_collision(self):
        target = self.roots["Movie"]["Library"][0] / self.source.name
        target.mkdir()
        plan = self.compare_move()
        self.assertEqual(plan["blockers"], "DESTINATION_COLLISION")

    def test_missing_source(self):
        (self.source / "film.mkv").unlink()
        self.source.rmdir()
        plan = self.compare_move()
        self.assertEqual(plan["blockers"], "SOURCE_MISSING")

    def test_cumulative_capacity_marks_cross_disk_rows(self):
        _, engine = self.setup_engines()
        self.free["media02"] = 49 * 1024**3
        plans = [{"status": "READY_FOR_REVIEW", "size_gb": 1.0,
                  "source_disk": "media01", "target_disk": "media02", "blockers": ""},
                 {"status": "BLOCKED", "size_gb": "",
                  "source_disk": "media01", "target_disk": "media02",
                  "blockers": "SOURCE_MISSING"}]
        self.assertEqual(engine.apply_cumulative_capacity(plans), {"media02"})
        self.assertEqual(plans[0]["blockers"], "CUMULATIVE_DESTINATION_SPACE")
        # The original final loop appends the cumulative blocker even to
        # rows that were already blocked for another reason.
        self.assertEqual(plans[1]["blockers"],
                         "SOURCE_MISSING;CUMULATIVE_DESTINATION_SPACE")

    def test_pre_override_filters_match_planner_loop(self):
        _, engine = self.setup_engines()
        requests = [
            MoveRequest("Movie", 7, "Film", str(self.source), "Common", "HOLD"),
            MoveRequest("Movie", 7, "Film", str(self.source), "Common", "Common"),
            MoveRequest("Movie", 7, "Film", str(self.source), "Common", "unknown"),
            MoveRequest("Movie", 7, "Film", str(self.source), "Common", "Library"),
        ]
        self.assertEqual(len(engine.evaluate_candidates(requests)), 1)

    def test_missing_destination_is_blocked_in_new_engine(self):
        old, engine = self.setup_engines()
        for root in self.roots["Movie"]["Library"]:
            root.rmdir()
        plan = engine.evaluate(MoveRequest("Movie", 7, "Film", str(self.source),
                                           "Common", "Library"))
        self.assertEqual(plan["status"], "BLOCKED")
        self.assertEqual(plan["blockers"],
                         "NO_ELIGIBLE_DESTINATION;DESTINATION_ROOT_MISSING")
        with self.assertRaises(AttributeError):
            old["evaluate_move"]("Movie", 7, "Film", str(self.source),
                                 "Common", "Library", "", "", "high", "")

    def test_parity_harness_runs_original_loops_without_output_writes(self):
        movie_csv = Path(self.tmp.name) / "movie_dry_run.csv"
        tv_csv = Path(self.tmp.name) / "tv_dry_run.csv"
        fields = ["radarr_id", "title", "year", "path", "current",
                  "recommended", "final_score", "replacement",
                  "replacement_confidence", "decision_reason"]
        with movie_csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow({"radarr_id": 7, "title": "Film", "year": 2020,
                             "path": str(self.source), "current": "Common",
                             "recommended": "Library", "final_score": 81,
                             "replacement": "yes",
                             "replacement_confidence": "high",
                             "decision_reason": "score"})
            writer.writerow({"radarr_id": "not-an-id", "current": "Common",
                             "recommended": "HOLD"})
        tv_csv.write_text("sonarr_id,title,path,current,recommended,final_score,"
                          "replacement,replacement_confidence,decision_reason\n")
        tree, old = load_legacy()
        old.update(DESTINATION_ROOTS=self.roots,
                   physical_disk=self.disk,
                   resolve_host_source=lambda media_type, arr_path, current: self.source,
                   free_bytes=self.free_bytes)
        legacy = run_legacy(tree, old, movie_csv, tv_csv, {})
        engine = ValidationEngine(
            ValidationPolicy(self.roots, self.disks),
            exists=lambda path: path.exists(),
            size_bytes=old["dir_size_bytes"],
            free_bytes=self.free_bytes,
        )
        current = engine.evaluate_candidates(
            _csv_requests(movie_csv, tv_csv, self.roots)
        )
        engine.apply_cumulative_capacity(current)
        self.assertEqual(current, legacy)
        self.assertEqual(len(current), 1)
        self.assertFalse((Path(self.tmp.name) / "move_plan.csv").exists())

    def test_override_snapshot_uses_integer_arr_ids(self):
        snapshot = Path(self.tmp.name) / "overrides.json"
        snapshot.write_text('{"Movie":{"7":["migratarr-lock"]},"TV":{}}')
        self.assertEqual(load_overrides(snapshot),
                         {"Movie": {7: {"migratarr-lock"}}, "TV": {}})
        snapshot.write_text('{"Movie":{"7":"migratarr-lock"}}')
        with self.assertRaises(ValueError):
            load_overrides(snapshot)


if __name__ == "__main__":
    unittest.main()
