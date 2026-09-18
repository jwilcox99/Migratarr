"""Readiness audit counts each plan and blocker without changing plans."""

import unittest

from migratarr_validation.audit import summarize
from migratarr_validation.impact import InputRow


class AuditTests(unittest.TestCase):
    def test_summary_counts_and_input_identity(self):
        rows = [InputRow("Movie", 2, {}), InputRow("Movie", 3, {}),
                InputRow("TV", 2, {})]
        plans = {
            ("Movie", 2): {"status": "BLOCKED", "blockers": "SOURCE_MISSING;DESTINATION_COLLISION",
                           "warnings": "ARCHIVE_MOVE_REVIEW", "title": "Film"},
            ("TV", 2): {"status": "READY_FOR_REVIEW", "blockers": "",
                        "warnings": "LOW_CONFIDENCE", "title": "Series"},
        }
        before = {key: value.copy() for key, value in plans.items()}
        result = summarize(rows, plans)
        self.assertEqual(result["input_rows"], {"Movie": 2, "TV": 1})
        self.assertEqual(result["plan_count"], 2)
        self.assertEqual(result["status_counts"], {"BLOCKED": 1, "READY_FOR_REVIEW": 1})
        self.assertEqual(result["blocker_counts"],
                         {"DESTINATION_COLLISION": 1, "SOURCE_MISSING": 1})
        self.assertEqual(result["blocked_items"], [{"media_type": "Movie", "csv_row": 2,
                                                   "title": "Film", "blockers":
                                                   ["DESTINATION_COLLISION", "SOURCE_MISSING"]}])
        self.assertEqual(plans, before)


if __name__ == "__main__":
    unittest.main()
