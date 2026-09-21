#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from runtime_config import get_config
RUNTIME = get_config()

BASE = RUNTIME.base_path
RUNS = BASE / "runs"
MANIFESTS = BASE / "manifests"


def sha256(path):
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def latest_run():
    runs = sorted(
        p for p in RUNS.iterdir()
        if p.is_dir()
    )

    if not runs:
        raise SystemExit("No snapshots found in runs/")

    return runs[-1]


def verify_snapshot(run_dir):
    sums = run_dir / "SHA256SUMS"

    if not sums.exists():
        raise SystemExit("Snapshot has no SHA256SUMS")

    failures = []

    for line in sums.read_text().splitlines():
        if not line.strip():
            continue

        expected, filename = line.split(None, 1)
        filename = filename.strip()

        path = run_dir / filename

        if not path.exists():
            failures.append(f"MISSING: {filename}")
            continue

        actual = sha256(path)

        if actual != expected:
            failures.append(f"CHECKSUM FAILED: {filename}")

    if failures:
        print("Snapshot verification FAILED:")

        for failure in failures:
            print("  " + failure)

        raise SystemExit(1)


parser = argparse.ArgumentParser(
    description="Build a Migratarr execution manifest from a frozen run."
)

parser.add_argument(
    "run_id",
    nargs="?",
    help="Snapshot run ID. Defaults to latest run."
)

args = parser.parse_args()

if args.run_id:
    run_dir = RUNS / args.run_id
else:
    run_dir = latest_run()

if not run_dir.exists():
    raise SystemExit(f"Run not found: {run_dir}")

verify_snapshot(run_dir)

metadata_path = run_dir / "metadata.json"
move_plan_path = run_dir / "move_plan.csv"

metadata = json.loads(metadata_path.read_text())

with move_plan_path.open(newline="") as f:
    rows = list(csv.DictReader(f))

eligible = [
    row for row in rows
    if row.get("status") == "READY_FOR_REVIEW"
    and row.get("current") != row.get("recommended")
]

blocked = [
    row for row in rows
    if row.get("status") == "BLOCKED"
]

MANIFESTS.mkdir(exist_ok=True)

manifest_dir = MANIFESTS / run_dir.name

if manifest_dir.exists():
    raise SystemExit(
        f"Manifest already exists for this run: {manifest_dir}"
    )

manifest_dir.mkdir()

manifest_csv = manifest_dir / "execution_manifest.csv"


# ------------------------------------------------------------
# Add stable execution IDs
# ------------------------------------------------------------

manifest_rows = []

for i, row in enumerate(eligible, 1):
    item = dict(row)

    item["execution_id"] = f"{run_dir.name}-{i:04d}"
    item["approved"] = "NO"
    item["executed"] = "NO"
    item["execution_result"] = ""

    manifest_rows.append(item)


# ------------------------------------------------------------
# Write manifest
# ------------------------------------------------------------

if manifest_rows:
    fields = [
        "execution_id",
        *eligible[0].keys(),
        "approved",
        "executed",
        "execution_result",
    ]

    with manifest_csv.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fields
        )

        writer.writeheader()
        writer.writerows(manifest_rows)

else:
    manifest_csv.write_text("")


# ------------------------------------------------------------
# Statistics
# ------------------------------------------------------------

category_moves = Counter(
    (
        r["media_type"],
        r["current"],
        r["recommended"],
    )
    for r in manifest_rows
)

transfer_types = Counter(
    r["transfer_type"]
    for r in manifest_rows
)

disk_flow = defaultdict(float)

for r in manifest_rows:
    if r["source_disk"] == r["target_disk"]:
        continue

    try:
        size = float(r["size_gb"])
    except (TypeError, ValueError):
        continue

    disk_flow[(r["source_disk"], "out")] += size
    disk_flow[(r["target_disk"], "in")] += size


manifest_metadata = {
    "manifest_version": 1,
    "run_id": run_dir.name,
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "source_snapshot": str(run_dir),
    "source_git_commit": metadata.get("git", {}).get("commit"),
    "snapshot_verified": True,
    "counts": {
        "source_plan_rows": len(rows),
        "eligible_rows": len(manifest_rows),
        "blocked_source_rows": len(blocked),
        "transfer_types": dict(transfer_types),
    },
    "approval": {
        "approved": False,
        "approved_utc": None,
    },
    "execution": {
        "started": False,
        "completed": False,
    },
}

metadata_out = manifest_dir / "manifest_metadata.json"

with metadata_out.open("w") as f:
    json.dump(
        manifest_metadata,
        f,
        indent=2
    )


# ------------------------------------------------------------
# Hash manifest
# ------------------------------------------------------------

with (manifest_dir / "SHA256SUMS").open("w") as f:
    f.write(
        f"{sha256(manifest_csv)}  execution_manifest.csv\n"
    )
    f.write(
        f"{sha256(metadata_out)}  manifest_metadata.json\n"
    )


# ------------------------------------------------------------
# Output
# ------------------------------------------------------------

print("=" * 72)
print("MIGRATARR EXECUTION MANIFEST")
print("=" * 72)

print(f"Run:              {run_dir.name}")
print(f"Snapshot verified:YES")
print(f"Eligible moves:   {len(manifest_rows)}")
print(f"Blocked in run:   {len(blocked)}")

print("\n=== TRANSFER TYPES ===")

for key, value in transfer_types.items():
    print(f"{key:<24} {value}")

print("\n=== CATEGORY MOVES ===")

for (media, current, recommended), count in sorted(
    category_moves.items()
):
    print(
        f"{media:<6} "
        f"{current:<10} -> "
        f"{recommended:<10} "
        f"{count}"
    )

print("\n=== CROSS-DISK FLOW ===")

for disk in [
    "media01",
    "media02",
    "media03",
    "media04",
]:
    incoming = disk_flow[(disk, "in")]
    outgoing = disk_flow[(disk, "out")]

    print(
        f"{disk}: "
        f"in={incoming:.1f} GB  "
        f"out={outgoing:.1f} GB  "
        f"net={incoming - outgoing:+.1f} GB"
    )

print()
print(f"Manifest: {manifest_csv}")
print()
print("ALL ROWS ARE UNAPPROVED.")
print("NO FILES WERE MOVED.")
