"""Explicit policy for planner review flags and Arr manual overrides.

Filesystem, path, and capacity blockers remain fixed in the engine. A rule
action changes only the named advisory rule; the conflicting-override blocker
is deliberately mandatory.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


LEGACY_TAGS = {
    "migratarr-common": "Common",
    "migratarr-current": "Current",
    "migratarr-library": "Library",
    "migratarr-rare": "Rare",
    "migratarr-archive": "Archive",
}
LEGACY_ACTIONS = {
    "CONFLICTING_MANUAL_OVERRIDES": "BLOCK",
    "MANUAL_LOCK": "WARN",
    "MANUAL_CATEGORY_OVERRIDE": "WARN",
    "LOW_OR_UNKNOWN_REPLACEMENT_CONFIDENCE": "WARN",
    "RARE_PROMOTION_REVIEW": "WARN",
    "ARCHIVE_MOVE_REVIEW": "WARN",
    "RARE_DEMOTION_REVIEW": "WARN",
}
OPTIONAL_ACTIONS = {"WARN", "BLOCK", "IGNORE"}


@dataclass(frozen=True)
class RulePolicy:
    lock_tag: str = "migratarr-lock"
    category_override_tags: Mapping[str, str] = field(default_factory=lambda: dict(LEGACY_TAGS))
    rare_category: str = "Rare"
    archive_category: str = "Archive"
    actions: Mapping[str, str] = field(default_factory=lambda: dict(LEGACY_ACTIONS))

    def emit(self, rule_id: str, blockers: list[str], warnings: list[str]) -> None:
        action = self.actions[rule_id]
        if action == "BLOCK":
            blockers.append(rule_id)
        elif action == "WARN":
            warnings.append(rule_id)


def _exact_keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must have exactly: {', '.join(sorted(expected))}")


def _tag(value, label):
    if (not isinstance(value, str) or not value or value != value.lower()
            or any(char.isspace() or char in {",", ";"} for char in value)):
        raise ValueError(f"{label} must be a lowercase tag without spaces or separators")


def _category(value, label):
    if (not isinstance(value, str) or not value or value in {"HOLD", ".", ".."}
            or any(char in value for char in "/\\,;")):
        raise ValueError(f"{label} must be a category name")


def parse_rule_policy(data):
    """Validate a versioned JSON object; never inspect Arr or the filesystem."""
    _exact_keys(data, {"schema_version", "lock_tag", "category_override_tags",
                       "review_categories", "actions"}, "Rule policy")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("Rule schema_version must be 1")
    lock_tag = data["lock_tag"]
    _tag(lock_tag, "lock_tag")
    tags = data["category_override_tags"]
    if not isinstance(tags, dict) or not tags:
        raise ValueError("category_override_tags must be a nonempty object")
    for tag, category in tags.items():
        _tag(tag, "category override tag")
        _category(category, f"category for {tag}")
    if lock_tag in tags:
        raise ValueError("Lock tag cannot also be a category override")
    if len(set(tags.values())) != len(tags):
        raise ValueError("Category override tags must map to distinct categories")
    review = data["review_categories"]
    _exact_keys(review, {"rare", "archive"}, "review_categories")
    _category(review["rare"], "rare review category")
    _category(review["archive"], "archive review category")
    if review["rare"] == review["archive"]:
        raise ValueError("Rare and Archive review categories must differ")
    actions = data["actions"]
    _exact_keys(actions, set(LEGACY_ACTIONS), "actions")
    if actions["CONFLICTING_MANUAL_OVERRIDES"] != "BLOCK":
        raise ValueError("Conflicting manual overrides must remain BLOCK")
    if actions["MANUAL_LOCK"] not in {"WARN", "IGNORE"}:
        raise ValueError("MANUAL_LOCK may only be WARN or IGNORE; the lock already cancels the move")
    for rule_id, action in actions.items():
        if action not in OPTIONAL_ACTIONS:
            raise ValueError(f"Invalid action for {rule_id}: {action}")
    return RulePolicy(lock_tag, dict(tags), review["rare"],
                      review["archive"], dict(actions))


def load_rule_policy(path: Path) -> RulePolicy:
    return parse_rule_policy(json.loads(path.read_text(encoding="utf-8")))


def validate_rule_references(rules: RulePolicy, storage_policy) -> None:
    """Require every configured override/review category in storage policy."""
    known = {
        category
        for groups in (storage_policy.category_paths
                       or storage_policy.destination_roots).values()
        for category in groups
    }
    destinations = {
        category
        for groups in storage_policy.destination_roots.values()
        for category in groups
    }
    override_categories = set(rules.category_override_tags.values())
    if override_categories - destinations:
        raise ValueError(
            "Override tags reference categories without destinations: "
            + ", ".join(sorted(override_categories - destinations))
        )
    reviews = {rules.rare_category, rules.archive_category}
    if reviews - known:
        raise ValueError(
            "Rule policy references unknown categories: "
            + ", ".join(sorted(reviews - known))
        )
