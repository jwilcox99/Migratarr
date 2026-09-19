"""Summarize validation statuses and blockers for saved placement CSVs.

This command evaluates the standalone engine in memory. It does not write a
move plan, manifest, approval, or media file.
"""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from .impact import ReadSnapshot, _configured, _flags, _sha, evaluate_rows, read_input_rows
from .parity import load_legacy, load_overrides


def summarize(rows, plans):
    statuses = Counter()
    blockers = Counter()
    warnings = Counter()
    blocked_items = []
    for row in rows:
        plan = plans.get(row.key)
        if plan is None:
            continue
        statuses[plan["status"]] += 1
        blockers.update(_flags(plan["blockers"]))
        warnings.update(_flags(plan["warnings"]))
        if _flags(plan["blockers"]):
            blocked_items.append({
                "media_type": row.media_type,
                "csv_row": row.csv_row,
                "title": plan["title"],
                "source_path": plan["source_path"],
                "target_path": plan["target_path"],
                "blockers": sorted(_flags(plan["blockers"])),
            })
    return {
        "input_rows": {"Movie": sum(r.media_type == "Movie" for r in rows),
                       "TV": sum(r.media_type == "TV" for r in rows)},
        "plan_count": len(plans),
        "status_counts": dict(sorted(statuses.items())),
        "blocker_counts": dict(sorted(blockers.items())),
        "warning_counts": dict(sorted(warnings.items())),
        "blocked_items": blocked_items,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--movie-csv", type=Path, required=True)
    parser.add_argument("--tv-csv", type=Path, required=True)
    parser.add_argument("--overrides-json", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--rules-config", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("Run on a POSIX host with the configured NAS mounts")
    try:
        policy = _configured(args.config, args.rules_config)
        rows = read_input_rows(args.movie_csv, args.tv_csv)
        _, legacy = load_legacy()
        reads = ReadSnapshot(Path.exists, legacy["dir_size_bytes"], legacy["free_bytes"])
        plans = evaluate_rows(rows, policy, load_overrides(args.overrides_json), reads)
        result = summarize(rows, plans)
        result["sha256"] = {
            "movie_csv": _sha(args.movie_csv),
            "tv_csv": _sha(args.tv_csv),
            "overrides_json": _sha(args.overrides_json),
            "config": _sha(args.config),
            "rules_config": _sha(args.rules_config),
        }
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        parser.exit(2, f"Audit could not complete: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
