"""Read-only parity runner against the repository's original planner code.

The original module is parsed, not imported. Comparison executes only its
candidate loops and cumulative capacity block. Snapshot mode calls its
read-only Arr tag loader. Planner CSV writes and executor code are excluded.
"""

import ast
import csv
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path

from .config import load_policy
from .engine import MoveRequest, ValidationEngine, ValidationPolicy
from .rules import load_rule_policy, validate_rule_references


PLANNER = Path(__file__).resolve().parents[1] / "build_move_plan.py"
CONSTANTS = {"DESTINATION_ROOTS", "MIN_FREE_AFTER_GB", "PROJECTED_FREE", "TARGET_BY_ID",
             "OVERRIDE_TAGS", "LOCK_TAG", "RADARR_URL", "SONARR_URL"}


def _assigned_name(node):
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    return target.id if isinstance(target, ast.Name) else None


def _execute(nodes, namespace):
    code = compile(ast.Module(body=nodes, type_ignores=[]), str(PLANNER), "exec")
    exec(code, namespace)


def load_legacy(runtime=None, targets=None, planner=None, settings=None):
    """Load decision code without executing planner module side effects."""
    planner = planner or PLANNER
    tree = ast.parse(planner.read_text(encoding="utf-8"), filename=str(planner))
    from runtime_config import load_config
    # Offline parity remains pinned to the documented Phase One deployment.
    # Live callers explicitly supply their deployed configuration.
    runtime = runtime or load_config(PLANNER.parent / 'config' / 'runtime.example.json', environ={})
    from planner_settings import load_settings
    settings = settings or load_settings(PLANNER.parent / 'config' / 'planner.example.json')
    from service_keys import read_key
    namespace = {"RUNTIME": runtime, "PLANNER_SETTINGS": settings, "read_key": read_key, "Path": Path, "os": os, "csv": csv, "json": json,
                 "subprocess": subprocess, "urllib": urllib}
    if any(_assigned_name(node) == 'TARGETS' for node in tree.body):
        from storage_targets import load_targets
        namespace['TARGETS'] = targets or load_targets(PLANNER.parent / 'config/storage-targets.example.json')
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


def compare(movie_csv, tv_csv, overrides, policy=None, rules=None):
    """Compare complete plan rows using one set of saved inputs."""
    tree, namespace = load_legacy()
    legacy = run_legacy(tree, namespace, movie_csv, tv_csv, overrides)
    if policy is None:
        reserves = {t.minimum_free_space_gb for t in namespace['TARGETS'].targets}
        if len(reserves) != 1:
            raise ValueError('Legacy validation engine requires a uniform target reserve')
        policy = ValidationPolicy(
            namespace["DESTINATION_ROOTS"],
            tuple(namespace["disk_roots"].values()),
            reserves.pop() * 1024**3,
        )
    if rules is not None:
        policy = replace(policy, rules=rules)
        validate_rule_references(rules, policy)
    engine = ValidationEngine(
        policy,
        exists=lambda path: path.exists(),
        size_bytes=namespace["dir_size_bytes"],
        free_bytes=namespace["free_bytes"],
        overrides=overrides,
    )
    current = engine.evaluate_candidates(
        _csv_requests(movie_csv, tv_csv, policy.destination_roots)
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


def _load_custom_overrides(namespace, rules):
    """Read Arr tags matching a custom rule policy without changing Arr."""
    result = {"Movie": {}, "TV": {}}
    systems = (
        ("Movie", namespace["RADARR_URL"], "radarr", "movie"),
        ("TV", namespace["SONARR_URL"], "sonarr", "series"),
    )
    for media, base, service, endpoint in systems:
        key = namespace["arr_key"](service)
        tags = namespace["api_json"](f"{base}/api/v3/tag", key)
        tag_map = {tag["id"]: tag["label"].lower() for tag in tags}
        items = namespace["api_json"](f"{base}/api/v3/{endpoint}", key)
        for item in items:
            labels = {
                tag_map[tag_id]
                for tag_id in item.get("tags", [])
                if tag_id in tag_map
            }
            relevant = {
                label for label in labels
                if label == rules.lock_tag or label in rules.category_override_tags
            }
            if relevant:
                result[media][item["id"]] = relevant
    return result


def snapshot_live_overrides(path, rules=None):
    """Save only current relevant Arr tags using read-only API requests."""
    from runtime_config import get_config
    from planner_settings import get_settings
    _, namespace = load_legacy(get_config(), settings=get_settings())
    overrides = (namespace["load_arr_overrides"]() if rules is None
                 else _load_custom_overrides(namespace, rules))
    serializable = {
        media: {str(item_id): sorted(tags) for item_id, tags in items.items()}
        for media, items in overrides.items()
    }
    with path.open("x", encoding="utf-8") as handle:
        json.dump(serializable, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--movie-csv", type=Path)
    parser.add_argument("--tv-csv", type=Path)
    parser.add_argument("--overrides-json", type=Path,
                        help="Saved Arr tag snapshot; use an explicit empty JSON object if none")
    parser.add_argument("--config", type=Path,
                        help="Validate and compare a storage policy JSON against the original planner")
    parser.add_argument("--rules-config", type=Path,
                        help="Validate and compare explicit review and override rules")
    parser.add_argument("--snapshot-overrides", type=Path,
                        help="Read current Arr tags and create this snapshot file only")
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("Run on a POSIX host with the planner's /mnt/nas mounts")
    if args.snapshot_overrides:
        if args.movie_csv or args.tv_csv or args.overrides_json or args.config:
            parser.error("--snapshot-overrides cannot be combined with comparison inputs")
        try:
            snapshot_live_overrides(
                args.snapshot_overrides,
                load_rule_policy(args.rules_config) if args.rules_config else None,
            )
        except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
            parser.exit(2, f"Override snapshot could not complete: {exc}\n")
        print(f"Saved override snapshot: {args.snapshot_overrides}")
        return 0
    if not (args.movie_csv and args.tv_csv and args.overrides_json):
        parser.error("--movie-csv, --tv-csv, and --overrides-json are required for comparison")
    try:
        result = compare(args.movie_csv, args.tv_csv,
                         load_overrides(args.overrides_json),
                         load_policy(args.config) if args.config else None,
                         load_rule_policy(args.rules_config) if args.rules_config else None)
        if args.config:
            result["config_sha256"] = hashlib.sha256(args.config.read_bytes()).hexdigest()
        if args.rules_config:
            result["rules_config_sha256"] = hashlib.sha256(
                args.rules_config.read_bytes()
            ).hexdigest()
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        parser.exit(2, f"Parity check could not complete: {exc}\n")
    print(json.dumps(result, indent=2))
    return 1 if result["difference_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
