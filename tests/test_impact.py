"""Policy impact reports match items by input row and expose exact changes."""

import unittest
from dataclasses import replace
from pathlib import Path

from migratarr_validation import RulePolicy, ValidationPolicy
from migratarr_validation.impact import (
    InputRow, ReadSnapshot, compare_policies, requires_candidate_overrides,
)
from migratarr_validation.rules import LEGACY_ACTIONS


GIB = 1024**3


def movie_row(number, item_id, title):
    return InputRow("Movie", number, {
        "radarr_id": str(item_id), "title": title, "year": "2020",
        "path": f"/logical/{title}", "current": "Common",
        "recommended": "Archive", "final_score": "10",
        "replacement": "yes", "replacement_confidence": "high",
        "decision_reason": "score",
    })


class ImpactReportTests(unittest.TestCase):
    def setUp(self):
        self.disk1 = Path("/virtual/disk1")
        self.disk2 = Path("/virtual/disk2")
        self.source_a = self.disk1 / "Movies/Common/Film"
        self.source_b = self.disk1 / "Movies/Common/Other"
        self.common = self.disk1 / "Movies/Common"
        self.archive1 = self.disk1 / "Movies/Archive"
        self.archive2 = self.disk2 / "Movies/Archive"
        self.existing = {self.source_a, self.source_b, self.common, self.archive1,
                         self.archive2}
        self.rows = [movie_row(2, 7, "Film"), movie_row(3, 8, "Other")]
        self.policy = ValidationPolicy(
            {"Movie": {"Common": (self.common,),
                       "Archive": (self.archive1,)}},
            (self.disk1, self.disk2),
        )

    def reads(self):
        return ReadSnapshot(lambda path: path in self.existing,
                            lambda _: GIB, lambda _: 100 * GIB)

    def test_identical_policies_have_no_impact(self):
        result = compare_policies(self.rows, self.policy, self.policy,
                                  {}, {}, self.reads())
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["summary"]["unchanged"], 2)

    def test_warning_promoted_to_blocker_is_reported(self):
        actions = {**LEGACY_ACTIONS, "ARCHIVE_MOVE_REVIEW": "BLOCK"}
        candidate = replace(self.policy, rules=replace(
            RulePolicy(), actions=actions,
        ))
        result = compare_policies(self.rows[:1], self.policy, candidate,
                                  {}, {}, self.reads())
        self.assertEqual(result["summary"]["status_changes"], 1)
        self.assertEqual(result["summary"]["changed"], 1)
        self.assertEqual(result["summary"]["blocker_changes"], 1)
        self.assertEqual(result["summary"]["warning_changes"], 1)
        change = result["changes"][0]
        self.assertEqual(change["csv_row"], 2)
        self.assertEqual(change["kind"], "changed")
        self.assertEqual(change["blockers_added"], ["ARCHIVE_MOVE_REVIEW"])
        self.assertEqual(change["warnings_removed"], ["ARCHIVE_MOVE_REVIEW"])
        self.assertEqual(change["field_changes"]["status"],
                         {"baseline": "READY_FOR_REVIEW", "candidate": "BLOCKED"})

    def test_removed_plan_does_not_shift_later_item(self):
        candidate_overrides = {"Movie": {7: {"migratarr-lock"}}}
        result = compare_policies(self.rows, self.policy, self.policy,
                                  {}, candidate_overrides, self.reads())
        self.assertEqual(result["summary"]["removed"], 1)
        self.assertEqual(result["summary"]["unchanged"], 1)
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(result["changes"][0]["item_id"], "7")
        self.assertIsNone(result["changes"][0]["candidate"])

    def test_newly_eligible_plan_is_added(self):
        baseline = replace(self.policy, destination_roots={
            "Movie": {"Common": (self.common,)}
        })
        result = compare_policies(self.rows[:1], baseline, self.policy,
                                  {}, {}, self.reads())
        self.assertEqual(result["summary"]["added"], 1)
        self.assertEqual(result["changes"][0]["kind"], "added")
        self.assertIsNone(result["changes"][0]["baseline"])
        self.assertEqual(result["changes"][0]["candidate"]["status"],
                         "READY_FOR_REVIEW")

    def test_destination_change_keeps_status_but_reports_target(self):
        candidate = replace(self.policy, destination_roots={
            "Movie": {"Archive": (self.archive2,)}
        })
        result = compare_policies(self.rows[:1], self.policy, candidate,
                                  {}, {}, self.reads())
        self.assertEqual(result["summary"]["destination_changes"], 1)
        self.assertEqual(result["summary"]["status_changes"], 0)
        target = result["changes"][0]["field_changes"]["target_path"]
        self.assertEqual(target["baseline"], str(self.archive1 / "Film"))
        self.assertEqual(target["candidate"], str(self.archive2 / "Film"))

    def test_changed_tag_names_require_a_candidate_snapshot(self):
        changed_rules = replace(RulePolicy(), lock_tag="custom-lock")
        candidate = replace(self.policy, rules=changed_rules)
        self.assertTrue(requires_candidate_overrides(self.policy, candidate))
        self.assertFalse(requires_candidate_overrides(self.policy, self.policy))

    def test_read_snapshot_reuses_samples_for_both_policies(self):
        calls = {"exists": 0, "size": 0, "free": 0}

        def exists(path):
            calls["exists"] += 1
            return path in self.existing

        def size(path):
            calls["size"] += 1
            return GIB

        def free(path):
            calls["free"] += 1
            return 100 * GIB

        reads = ReadSnapshot(exists, size, free)
        compare_policies(self.rows[:1], self.policy, self.policy,
                         {}, {}, reads)
        sampled = dict(calls)
        compare_policies(self.rows[:1], self.policy, self.policy,
                         {}, {}, reads)
        self.assertEqual(calls, sampled)


if __name__ == "__main__":
    unittest.main()
