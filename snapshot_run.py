#!/usr/bin/env python3

import csv
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from runtime_config import get_config
RUNTIME = get_config()

BASE = RUNTIME.base_path
RUNS = BASE / "runs"

FILES = [
    "movie_dry_run.csv",
    "tv_dry_run.csv",
    "move_plan.csv",
]

CODE_FILES = [
    "runtime_config.py",
    "dry_run_movies.py",
    "dry_run_tv.py",
    "build_move_plan.py",
    "audit_overrides.py",
]

RUNS.mkdir(exist_ok=True)


def sha256(path):
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def git_output(*args):
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=BASE,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return ""


def csv_count(path):
    with path.open(newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


# ------------------------------------------------------------
# Validate required files
# ------------------------------------------------------------

missing = [
    filename
    for filename in FILES
    if not (BASE / filename).exists()
]

if missing:
    raise SystemExit(
        "Missing required files: " + ", ".join(missing)
    )


# ------------------------------------------------------------
# Create immutable run directory
# ------------------------------------------------------------

now = datetime.now(timezone.utc)

run_id = now.strftime("%Y%m%dT%H%M%SZ")
run_dir = RUNS / run_id

if run_dir.exists():
    raise SystemExit(f"Run already exists: {run_dir}")

run_dir.mkdir()


# ------------------------------------------------------------
# Copy score/plan files
# ------------------------------------------------------------

checksums = {}

for filename in FILES:
    src = BASE / filename
    dst = run_dir / filename

    shutil.copy2(src, dst)
    checksums[filename] = sha256(dst)


# ------------------------------------------------------------
# Copy exact code used for the run
# ------------------------------------------------------------

code_dir = run_dir / "code"
code_dir.mkdir()

for filename in CODE_FILES:
    src = BASE / filename

    if not src.exists():
        continue

    dst = code_dir / filename
    shutil.copy2(src, dst)

    checksums[f"code/{filename}"] = sha256(dst)

# Include the effective non-secret host settings, including environment overrides.
runtime_snapshot = code_dir / "runtime.json"
runtime_snapshot.write_text(json.dumps(RUNTIME.as_dict(), indent=2) + "\n", encoding="utf-8")
checksums["code/runtime.json"] = sha256(runtime_snapshot)


# ------------------------------------------------------------
# Collect plan statistics
# ------------------------------------------------------------

with (BASE / "move_plan.csv").open(newline="") as f:
    plans = list(csv.DictReader(f))

status_counts = {}
media_counts = {}
transfer_counts = {}

for row in plans:
    status = row.get("status", "")
    media = row.get("media_type", "")
    transfer = row.get("transfer_type", "")

    status_counts[status] = status_counts.get(status, 0) + 1
    media_counts[media] = media_counts.get(media, 0) + 1
    transfer_counts[transfer] = transfer_counts.get(transfer, 0) + 1


# ------------------------------------------------------------
# Git state
# ------------------------------------------------------------

git_commit = git_output("rev-parse", "HEAD")
git_branch = git_output("branch", "--show-current")
git_status = git_output("status", "--porcelain")

metadata = {
    "run_id": run_id,
    "created_utc": now.isoformat(),
    "git": {
        "commit": git_commit,
        "branch": git_branch,
        "working_tree_clean": not bool(git_status),
        "working_tree_status": git_status.splitlines() if git_status else [],
    },
    "counts": {
        "movie_scores": csv_count(BASE / "movie_dry_run.csv"),
        "tv_scores": csv_count(BASE / "tv_dry_run.csv"),
        "planned_moves": len(plans),
        "status": status_counts,
        "media_type": media_counts,
        "transfer_type": transfer_counts,
    },
    "checksums": checksums,
    "execution": {
        "approved": False,
        "executed": False,
    },
}

with (run_dir / "metadata.json").open("w") as f:
    json.dump(metadata, f, indent=2)

checksums["metadata.json"] = sha256(
    run_dir / "metadata.json"
)

with (run_dir / "SHA256SUMS").open("w") as f:
    for filename, digest in sorted(checksums.items()):
        f.write(f"{digest}  {filename}\n")


# ------------------------------------------------------------
# Make snapshot read-only
# ------------------------------------------------------------

for path in run_dir.rglob("*"):
    if path.is_file():
        path.chmod(0o444)

for path in sorted(
    [p for p in run_dir.rglob("*") if p.is_dir()],
    reverse=True,
):
    path.chmod(0o555)

run_dir.chmod(0o555)


# ------------------------------------------------------------
# Output
# ------------------------------------------------------------

print("=" * 72)
print("MIGRATARR RUN SNAPSHOT")
print("=" * 72)
print(f"Run ID:       {run_id}")
print(f"Git commit:   {git_commit or 'UNKNOWN'}")
print(f"Git clean:    {'YES' if not git_status else 'NO'}")
print()
print(f"Movies:       {metadata['counts']['movie_scores']}")
print(f"TV series:    {metadata['counts']['tv_scores']}")
print(f"Planned moves:{metadata['counts']['planned_moves']:>5}")
print()
print(f"Snapshot: {run_dir}")
print()
print("Snapshot is READ-ONLY.")
print("NO FILES WERE MOVED.")
