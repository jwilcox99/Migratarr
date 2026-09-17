"""Read-only parity runner against the repository's original planner code.

The original module is parsed, not imported. Only its constants, function
definitions, candidate loops, and cumulative capacity block are executed.
Docker calls, module-level output writes, and executor code are excluded.
"""

import ast
import csv
import hashlib
import json
import os
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from .engine import MoveRequest, ValidationEngine, ValidationPolicy


PLANNER = Path(__file__).resolve().parents[1] / "build_move_plan.py"
CONSTANTS = {"DESTINATION_ROOTS", "MIN_FREE_AFTER_GB", "PROJECTED_FREE",
             "OVERRIDE_TAGS", "LOCK_TAG"}


def _assigned_name(node):
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    return target.id if isinstance(target, ast.Name) else None


def _execute(nodes, namespace):
    code = compile(ast.Module(body=nodes, type_ignores=[]), str(PLANNER), "exec")
    exec(code, namespace)


def load_legacy():
    """Load decision code without executing planner module side effects."""
    tree = ast.parse(PLANNER.read_text(encoding="utf-8"), filename=str(PLANNER))
    namespace = {"Path": Path, "os": os, "csv": csv}
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) or _assigned_name(node) in CONSTANTS
    ]
    _execute(nodes, namespace)
    return tree, namespace


def _csv_requests(movie_csv, tv_csv, destination_roots):
    requests = []
    for media_type, path, id_field in (
        ("Movie", movie_csv, "radarr_id"), ("TV", tv_csv, "sonarr_id")
    ):
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                # The original loops skip these before reading the item ID or
                # any remaining scoring fields.
                if (row["recommended"] == "HOLD"
                        or row["current"] == row["recommended"]
                        or row["recommended"] not in destination_roots[media_type]):
                    continue
                requests.append(MoveRequest(
                    media_type=media_type,
                    item_id=int(row[id_field]) if row.get(id_field) else None,
                    title=(f'{row["title"]} ({row["year"]})' if media_type == "Movie"
                           else row["title"]),
                    source_path=row["path"],
                    current=row["current"],
                    recommended=row["recommended"],
                    final_score=row["final_score"],
                    replacement=row["replacement"],
                    confidence=row["replacement_confidence"],
                    decision_reason=row["decision_reason"],
                ))
    return requests


def run_legacy(tree, namespace, movie_csv, tv_csv, overrides):
    """Execute the original candidate and final-capacity AST nodes in memory."""
    namespace.update(MOVIE_CSV=movie_csv, TV_CSV=tv_csv,
                     ARR_OVERRIDES=overrides, plans=[])
    loops = [
        node for node in tree.body
        if isinstance(node, ast.For)
        and isinstance(node.iter, ast.Call)
        and isinstance(node.iter.func, ast.Name)
        and node.iter.func.id == "read_csv"
    ]
    if len(loops) != 2:
        raise ValueError("Planner candidate loops changed; update parity harness")
    _execute(loops, namespace)
    start = next(i for i, node in enumerate(tree.body)
                 if _assigned_name(node) == "disk_roots")
    end = next(i for i in range(start + 1, len(tree.body))
               if isinstance(tree.body[i], ast.With))
    with redirect_stdout(StringIO()):
        _execute(tree.body[start:end], namespace)
    return namespace["plans"]


def compare(movie_csv, tv_csv, overrides):
    """Compare complete plan rows using one set of saved inputs."""
    tree, namespace = load_legacy()
    legacy = run_legacy(tree, namespace, movie_csv, tv_csv, overrides)
    engine = ValidationEngine(
        ValidationPolicy(
            namespace["DESTINATION_ROOTS"],
            tuple(namespace["disk_roots"].values()),
            namespace["MIN_FREE_AFTER_GB"] * 1024**3,
        ),
        exists=lambda path: path.exists(),
        size_bytes=namespace["dir_size_bytes"],
        free_bytes=namespace["free_bytes"],
        overrides=overrides,
    )
    current = engine.evaluate_candidates(
        _csv_requests(movie_csv, tv_csv, namespace["DESTINATION_ROOTS"])
    )
    engine.apply_cumulative_capacity(current)
    differences = []
    for index in range(max(len(legacy), len(current))):
        old = legacy[index] if index < len(legacy) else None
        new = current[index] if index < len(current) else None
        if old == new:
            continue
        changed = sorted(set(old or {}) | set(new or {}))
        differences.append({
            "row": index + 1,
            "title": (old or new)["title"],
            "fields": {key: {"legacy": old.get(key) if old else None,
                             "engine": new.get(key) if new else None}
                       for key in changed
                       if old is None or new is None or old.get(key) != new.get(key)},
        })
    return {
        "legacy_count": len(legacy),
        "engine_count": len(current),
        "difference_count": len(differences),
        "differences": differences,
        "input_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in (movie_csv, tv_csv)},
    }


def load_overrides(path):
    """JSON shape: {"Movie": {"123": ["migratarr-lock"]}, "TV": {...}}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Override snapshot must be a JSON object")
    if set(data) - {"Movie", "TV"}:
        raise ValueError("Only Movie and TV override sections are supported")
    result = {}
    for media, items in data.items():
        if not isinstance(items, dict):
            raise ValueError(f"{media} overrides must be an object keyed by item ID")
        result[media] = {}
        for item_id, tags in items.items():
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                raise ValueError(f"{media} item {item_id} tags must be a string array")
            result[media][int(item_id)] = set(tags)
    return result


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--movie-csv", type=Path, required=True)
    parser.add_argument("--tv-csv", type=Path, required=True)
    parser.add_argument("--overrides-json", type=Path, required=True,
                        help="Saved Arr tag snapshot; use an explicit empty JSON object if none")
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("Run on a POSIX host with the planner's /mnt/nas mounts")
    try:
        result = compare(args.movie_csv, args.tv_csv,
                         load_overrides(args.overrides_json))
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        parser.exit(2, f"Parity check could not complete: {exc}\n")
    print(json.dumps(result, indent=2))
    return 1 if result["difference_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
