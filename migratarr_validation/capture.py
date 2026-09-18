"""Capture fresh placement CSVs in an isolated directory.

The original placement scripts execute unchanged except for their two known
filesystem destinations, which are replaced in memory after a source hash
check. This command never invokes the move planner or executor.
"""

import argparse
import ast
import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = {
    "movie": ("dry_run_movies.py", "4c6ccc9d1d63951b8c691cfe184827bf5a41de326910f898b82ac1a95da40945"),
    "tv": ("dry_run_tv.py", "9fcb84e7a67980fa6bb2edae58b5ebeeecfffa14efe4a2755ddfa413189a2253"),
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_tree(kind):
    name, expected = SCRIPTS[kind]
    path = ROOT / name
    source = path.read_bytes()
    # Git stores LF; Windows checkouts may use CRLF. These are the only
    # differing bytes permitted by this source-integrity check.
    canonical = source.replace(b"\r\n", b"\n")
    actual = hashlib.sha256(canonical).hexdigest()
    if actual != expected:
        raise ValueError(f"{name} changed; review its writes and update capture.py before running")
    return path, ast.parse(source.decode("utf-8"), filename=str(path))


def redirect_destinations(tree, output, cache):
    """Replace only the verified module-level OUTPUT and CACHE_DIR bindings."""
    replacements = {"OUTPUT": output, "CACHE_DIR": cache}
    found = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in replacements:
            continue
        if target.id in found or not isinstance(node.value, ast.Call) or not isinstance(node.value.func, ast.Name) or node.value.func.id != "Path" or len(node.value.args) != 1 or node.value.keywords or not isinstance(node.value.args[0], ast.Constant) or not isinstance(node.value.args[0].value, str):
            raise ValueError(f"Unexpected {target.id} assignment in placement script")
        node.value.args[0].value = str(replacements[target.id])
        found.add(target.id)
    if found != replacements.keys():
        raise ValueError(f"Missing placement path assignments: {sorted(replacements.keys() - found)}")
    return ast.fix_missing_locations(tree)


def run_one(kind, directory):
    if os.environ.get("MIGRATARR_CAPTURE_INTERNAL_DIR") != str(directory):
        raise ValueError("Internal capture must be started by the capture command")
    path, tree = checked_tree(kind)
    output = directory / f"{kind}_dry_run.csv"
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    tree = redirect_destinations(tree, output, directory / "cache")
    namespace = {"__name__": "__main__", "__file__": str(path)}
    exec(compile(tree, str(path), "exec"), namespace)


def capture(directory):
    if not os.environ.get("TMDB_TOKEN"):
        raise ValueError("TMDB_TOKEN is required by the placement scripts")
    for kind in SCRIPTS:
        checked_tree(kind)
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    for kind in SCRIPTS:
        command = [sys.executable, "-m", "migratarr_validation.capture",
                   "--internal-run", kind, "--output-dir", str(directory)]
        env = dict(os.environ, MIGRATARR_CAPTURE_INTERNAL_DIR=str(directory))
        subprocess.run(command, cwd=ROOT, env=env, check=True, stdout=sys.stderr)
    inputs = {}
    for kind in SCRIPTS:
        path = directory / f"{kind}_dry_run.csv"
        with path.open(newline="") as handle:
            reader = csv.reader(handle)
            if not next(reader, None):
                raise ValueError(f"No CSV header in {path}")
            rows = sum(1 for _ in reader)
        inputs[kind] = {"path": str(path), "rows": rows, "sha256": sha256(path)}
    result = {"status": "complete", "inputs": inputs,
              "scripts_sha256": {kind: sha256(ROOT / name)
                                 for kind, (name, _) in SCRIPTS.items()}}
    (directory / "capture.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--internal-run", choices=SCRIPTS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.internal_run:
            run_one(args.internal_run, args.output_dir)
        else:
            print(json.dumps(capture(args.output_dir), indent=2))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"capture failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
