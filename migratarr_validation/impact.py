"""Read-only comparison of a baseline policy and a proposed policy.

Both policies evaluate the same saved placement CSVs. Each plan is linked to
its original CSV row, so added or removed plans do not shift later matches.
No planner CSV, manifest, approval, Arr record, or media file is changed.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .config import load_policy
from .engine import MoveRequest, ValidationEngine, ValidationPolicy
from .parity import load_legacy, load_overrides
from .rules import RulePolicy, load_rule_policy, validate_rule_references


@dataclass(frozen=True)
class InputRow:
    media_type: str
    csv_row: int
    data: dict[str, str]

    @property
    def key(self) -> tuple[str, int]:
        return self.media_type, self.csv_row


def read_input_rows(movie_csv: Path, tv_csv: Path) -> list[InputRow]:
    rows = []
    for media, path in (("Movie", movie_csv), ("TV", tv_csv)):
        with path.open(newline="") as handle:
            for number, row in enumerate(csv.DictReader(handle), start=2):
                rows.append(InputRow(media, number, row))
    return rows


@dataclass
class ReadSnapshot:
    """Reuse sampled filesystem answers for both evaluations where possible."""

    exists_read: Callable[[Path], bool]
    size_read: Callable[[Path], int | None]
    free_read: Callable[[Path], int | None]
    exists_cache: dict[Path, bool] = field(default_factory=dict)
    size_cache: dict[Path, int | None] = field(default_factory=dict)
    free_cache: dict[Path, int | None] = field(default_factory=dict)

    def exists(self, path: Path) -> bool:
        if path not in self.exists_cache:
            self.exists_cache[path] = self.exists_read(path)
        return self.exists_cache[path]

    def size(self, path: Path) -> int | None:
        if path not in self.size_cache:
            self.size_cache[path] = self.size_read(path)
        return self.size_cache[path]

    def free(self, path: Path) -> int | None:
        if path not in self.free_cache:
            self.free_cache[path] = self.free_read(path)
        return self.free_cache[path]


def _request(row: InputRow) -> MoveRequest:
    values = row.data
    id_field = "radarr_id" if row.media_type == "Movie" else "sonarr_id"
    return MoveRequest(
        media_type=row.media_type,
        item_id=int(values[id_field]) if values.get(id_field) else None,
        title=(f'{values["title"]} ({values["year"]})'
               if row.media_type == "Movie" else values["title"]),
        source_path=values["path"],
        current=values["current"],
        recommended=values["recommended"],
        final_score=values["final_score"],
        replacement=values["replacement"],
        confidence=values["replacement_confidence"],
        decision_reason=values["decision_reason"],
    )


def evaluate_rows(
    rows: list[InputRow], policy: ValidationPolicy,
    overrides: dict, reads: ReadSnapshot,
) -> dict[tuple[str, int], dict[str, object]]:
    """Evaluate in CSV order and apply the existing cumulative capacity pass."""
    engine = ValidationEngine(policy, reads.exists, reads.size, reads.free,
                              overrides=overrides)
    plans = {}
    for row in rows:
        values = row.data
        current, recommended = values["current"], values["recommended"]
        if (recommended == "HOLD" or current == recommended
                or recommended not in policy.destination_roots[row.media_type]):
            continue
        plan = engine.evaluate(_request(row))
        if plan["current"] != plan["recommended"]:
            plans[row.key] = plan
    engine.apply_cumulative_capacity(list(plans.values()))
    return plans


def _flags(value: object) -> set[str]:
    return set(str(value).split(";")) - {""} if value else set()


def compare_policies(
    rows: list[InputRow], baseline: ValidationPolicy,
    candidate: ValidationPolicy, baseline_overrides: dict,
    candidate_overrides: dict, reads: ReadSnapshot,
) -> dict[str, object]:
    """Return stable, field-level impact grouped by original CSV row."""
    old = evaluate_rows(rows, baseline, baseline_overrides, reads)
    new = evaluate_rows(rows, candidate, candidate_overrides, reads)
    changes = []
    summary = {
        "baseline_plans": len(old), "candidate_plans": len(new),
        "unchanged": 0, "added": 0, "removed": 0, "changed": 0,
        "destination_changes": 0, "status_changes": 0,
        "blocker_changes": 0, "warning_changes": 0,
    }
    for row in rows:
        before, after = old.get(row.key), new.get(row.key)
        if before == after:
            if before is not None:
                summary["unchanged"] += 1
            continue
        if before is None and after is None:
            continue
        kind = "added" if before is None else "removed" if after is None else "changed"
        summary[kind] += 1
        item = {
            "media_type": row.media_type,
            "csv_row": row.csv_row,
            "item_id": row.data.get("radarr_id" if row.media_type == "Movie"
                                    else "sonarr_id") or None,
            "title": (after or before)["title"],
            "kind": kind,
        }
        if kind == "changed":
            item["field_changes"] = {
                key: {"baseline": before.get(key), "candidate": after.get(key)}
                for key in sorted(set(before) | set(after))
                if before.get(key) != after.get(key)
            }
            item["blockers_added"] = sorted(_flags(after["blockers"]) - _flags(before["blockers"]))
            item["blockers_removed"] = sorted(_flags(before["blockers"]) - _flags(after["blockers"]))
            item["warnings_added"] = sorted(_flags(after["warnings"]) - _flags(before["warnings"]))
            item["warnings_removed"] = sorted(_flags(before["warnings"]) - _flags(after["warnings"]))
            if item["blockers_added"] or item["blockers_removed"]:
                summary["blocker_changes"] += 1
            if item["warnings_added"] or item["warnings_removed"]:
                summary["warning_changes"] += 1
            if before["target_path"] != after["target_path"]:
                summary["destination_changes"] += 1
            if before["status"] != after["status"]:
                summary["status_changes"] += 1
        else:
            item["baseline"] = before
            item["candidate"] = after
        changes.append(item)
    return {"summary": summary, "changes": changes}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configured(storage_path: Path, rules_path: Path) -> ValidationPolicy:
    storage = load_policy(storage_path)
    rules = load_rule_policy(rules_path)
    validate_rule_references(rules, storage)
    return replace(storage, rules=rules)


def requires_candidate_overrides(
    baseline: ValidationPolicy, candidate: ValidationPolicy
) -> bool:
    return (baseline.rules.lock_tag != candidate.rules.lock_tag
            or dict(baseline.rules.category_override_tags)
            != dict(candidate.rules.category_override_tags))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--movie-csv", type=Path, required=True)
    parser.add_argument("--tv-csv", type=Path, required=True)
    parser.add_argument("--overrides-json", type=Path, required=True)
    parser.add_argument("--baseline-storage", type=Path, required=True)
    parser.add_argument("--baseline-rules", type=Path, required=True)
    parser.add_argument("--candidate-storage", type=Path)
    parser.add_argument("--candidate-rules", type=Path)
    parser.add_argument("--candidate-overrides-json", type=Path)
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("Run on a POSIX host with the configured NAS mounts")
    if not (args.candidate_storage or args.candidate_rules):
        parser.error("Provide a candidate storage or rule policy file")
    candidate_storage = args.candidate_storage or args.baseline_storage
    candidate_rules = args.candidate_rules or args.baseline_rules
    try:
        baseline = _configured(args.baseline_storage, args.baseline_rules)
        candidate = _configured(candidate_storage, candidate_rules)
        if requires_candidate_overrides(baseline, candidate) and not args.candidate_overrides_json:
            raise ValueError(
                "Changed override tag names require --candidate-overrides-json"
            )
        overrides = load_overrides(args.overrides_json)
        candidate_overrides = (load_overrides(args.candidate_overrides_json)
                               if args.candidate_overrides_json else overrides)
        _, namespace = load_legacy()
        reads = ReadSnapshot(Path.exists, namespace["dir_size_bytes"],
                             namespace["free_bytes"])
        result = compare_policies(
            read_input_rows(args.movie_csv, args.tv_csv), baseline, candidate,
            overrides, candidate_overrides, reads,
        )
        result["sha256"] = {
            "movie_csv": _sha(args.movie_csv),
            "tv_csv": _sha(args.tv_csv),
            "baseline_storage": _sha(args.baseline_storage),
            "baseline_rules": _sha(args.baseline_rules),
            "candidate_storage": _sha(candidate_storage),
            "candidate_rules": _sha(candidate_rules),
            "baseline_overrides": _sha(args.overrides_json),
            "candidate_overrides": _sha(args.candidate_overrides_json)
            if args.candidate_overrides_json else _sha(args.overrides_json),
        }
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        parser.exit(2, f"Impact report could not complete: {exc}\n")
    print(json.dumps(result, indent=2))
    return 1 if result["changes"] else 0


if __name__ == "__main__":
    sys.exit(main())
