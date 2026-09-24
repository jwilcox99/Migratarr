"""Explicit rules preserve legacy defaults and validate safe customization."""

import copy
import json
import unittest
from pathlib import Path

from migratarr_validation import MoveRequest, RulePolicy, ValidationEngine, ValidationPolicy
from migratarr_validation.engine import apply_override
from migratarr_validation.parity import _load_custom_overrides, load_legacy
from migratarr_validation.rules import (
    LEGACY_ACTIONS, load_rule_policy, parse_rule_policy, validate_rule_references,
)


RULE_FILE = Path(__file__).resolve().parents[1] / "config" / "legacy-rules.json"
EXAMPLE_RULE_FILE = RULE_FILE.parent / "examples" / "archive-review-block.json"


class RulePolicyTests(unittest.TestCase):
    def test_legacy_rule_file_matches_planner_tags_and_default_actions(self):
        rules = load_rule_policy(RULE_FILE)
        _, legacy = load_legacy()
        self.assertEqual(rules.lock_tag, legacy["LOCK_TAG"])
        self.assertEqual(rules.category_override_tags, legacy["OVERRIDE_TAGS"])
        self.assertEqual(rules.actions, LEGACY_ACTIONS)
        self.assertEqual(rules, RulePolicy())

    def test_example_changes_only_archive_review_action(self):
        baseline = load_rule_policy(RULE_FILE)
        example = load_rule_policy(EXAMPLE_RULE_FILE)
        self.assertEqual(example.lock_tag, baseline.lock_tag)
        self.assertEqual(example.category_override_tags,
                         baseline.category_override_tags)
        self.assertEqual(example.rare_category, baseline.rare_category)
        self.assertEqual(example.archive_category, baseline.archive_category)
        self.assertEqual({key for key in baseline.actions
                          if baseline.actions[key] != example.actions[key]},
                         {"ARCHIVE_MOVE_REVIEW"})
        self.assertEqual(example.actions["ARCHIVE_MOVE_REVIEW"], "BLOCK")

    def test_rejects_conflict_downgrade_and_malformed_rules(self):
        valid = json.loads(RULE_FILE.read_text())
        mutations = [
            ("conflict downgrade", lambda d: d["actions"].__setitem__(
                "CONFLICTING_MANUAL_OVERRIDES", "WARN")),
            ("unknown action", lambda d: d["actions"].__setitem__(
                "ARCHIVE_MOVE_REVIEW", "APPROVE")),
            ("lock cannot block", lambda d: d["actions"].__setitem__(
                "MANUAL_LOCK", "BLOCK")),
            ("missing action", lambda d: d["actions"].pop("MANUAL_LOCK")),
            ("lock also category", lambda d: d["category_override_tags"].__setitem__(
                "migratarr-lock", "Special")),
            ("uppercase tag", lambda d: d.__setitem__("lock_tag", "Migratarr-Lock")),
            ("same review category", lambda d: d["review_categories"].__setitem__(
                "archive", "Rare")),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                data = copy.deepcopy(valid)
                mutate(data)
                with self.assertRaises(ValueError):
                    parse_rule_policy(data)

    def test_actions_and_custom_tags_change_only_named_review_rules(self):
        data = json.loads(RULE_FILE.read_text())
        data["lock_tag"] = "custom-lock"
        data["category_override_tags"] = {"custom-vault": "Vault"}
        data["review_categories"] = {"rare": "Vault", "archive": "Archive"}
        data["actions"]["RARE_PROMOTION_REVIEW"] = "IGNORE"
        data["actions"]["ARCHIVE_MOVE_REVIEW"] = "BLOCK"
        rules = parse_rule_policy(data)
        roots = {
            "Movie": {"Vault": (Path("/virtual/disk1/Vault"),),
                      "Archive": (Path("/virtual/disk1/Archive"),)}
        }
        source = Path("/virtual/disk1/Common/Film")
        existing = {source, *roots["Movie"]["Vault"], *roots["Movie"]["Archive"]}
        policy = ValidationPolicy(roots, (Path("/virtual/disk1"),), 0,
                                  rules=rules)
        engine = ValidationEngine(policy, lambda path: path in existing,
                                  lambda _: 1024, lambda _: 100 * 1024**3,
                                  overrides={"Movie": {7: {"custom-vault"}}})
        request = MoveRequest("Movie", 7, "Film", str(source), "Common",
                              "Archive", confidence="high")
        plan = engine.evaluate(request)
        self.assertEqual(plan["recommended"], "Vault")
        self.assertEqual(plan["warnings"], "MANUAL_CATEGORY_OVERRIDE")
        self.assertEqual(plan["status"], "READY_FOR_REVIEW")
        self.assertEqual(apply_override("Movie", 7, "Common", "Archive",
                                        {"Movie": {7: {"custom-lock", "custom-vault"}}}, rules),
                         ("Common", "LOCK", "custom-lock"))

        archive_engine = ValidationEngine(policy, lambda path: path in existing,
                                          lambda _: 1024, lambda _: 100 * 1024**3)
        archive = archive_engine.evaluate(request)
        self.assertEqual(archive["blockers"], "ARCHIVE_MOVE_REVIEW")
        self.assertEqual(archive["status"], "BLOCKED")

        data["actions"]["MANUAL_CATEGORY_OVERRIDE"] = "BLOCK"
        strict_policy = ValidationPolicy(roots, (Path("/virtual/disk1"),), 0,
                                         rules=parse_rule_policy(data))
        strict_engine = ValidationEngine(
            strict_policy, lambda path: path in existing,
            lambda _: 1024, lambda _: 100 * 1024**3,
            overrides={"Movie": {7: {"custom-vault"}}},
        )
        self.assertEqual(strict_engine.evaluate(request)["blockers"],
                         "MANUAL_CATEGORY_OVERRIDE")

    def test_custom_override_snapshot_reads_configured_tags(self):
        rules = parse_rule_policy({
            **json.loads(RULE_FILE.read_text()),
            "lock_tag": "custom-lock",
            "category_override_tags": {"custom-vault": "Vault"},
        })
        responses = {
            "http://radarr/api/v3/tag": [
                {"id": 1, "label": "Custom-Vault"},
                {"id": 2, "label": "Unrelated"},
            ],
            "http://radarr/api/v3/movie": [
                {"id": 7, "tags": [1, 2]}, {"id": 8, "tags": [2]},
            ],
            "http://sonarr/api/v3/tag": [{"id": 4, "label": "Custom-Lock"}],
            "http://sonarr/api/v3/series": [{"id": 9, "tags": [4]}],
        }
        namespace = {
            "RUNTIME": load_legacy()[1]["RUNTIME"],
            "RADARR_URL": "http://radarr", "SONARR_URL": "http://sonarr",
            "arr_key": lambda _: "test-key",
            "api_json": lambda url, _: responses[url],
        }
        self.assertEqual(_load_custom_overrides(namespace, rules), {
            "Movie": {7: {"custom-vault"}}, "TV": {9: {"custom-lock"}},
        })

    def test_override_target_needs_a_destination(self):
        storage = ValidationPolicy(
            {"Movie": {"Archive": (Path("/virtual/disk1/Archive"),)}},
            (Path("/virtual/disk1"),),
            category_paths={"Movie": {"Vault": Path("Movies/Vault"),
                                      "Archive": Path("Movies/Archive")}},
        )
        rules = RulePolicy(category_override_tags={"custom-vault": "Vault"})
        with self.assertRaises(ValueError):
            validate_rule_references(rules, storage)


if __name__ == "__main__":
    unittest.main()
