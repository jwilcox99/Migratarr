#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from runtime_config import get_config
RUNTIME = get_config()

BASE = RUNTIME.base_path
MANIFESTS = BASE / "manifests"
APPROVALS = BASE / "approvals"

APPROVALS.mkdir(exist_ok=True)


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def latest_manifest_dir():
    dirs = sorted(
        p for p in MANIFESTS.iterdir()
        if p.is_dir()
    )

    if not dirs:
        raise SystemExit("No manifests found.")

    return dirs[-1]


def load_manifest(run_id=None):
    manifest_dir = (
        MANIFESTS / run_id
        if run_id
        else latest_manifest_dir()
    )

    manifest = manifest_dir / "execution_manifest.csv"
    sums = manifest_dir / "SHA256SUMS"

    if not manifest.exists():
        raise SystemExit(f"Manifest missing: {manifest}")

    if not sums.exists():
        raise SystemExit("Manifest SHA256SUMS missing.")

    expected = {}

    for line in sums.read_text().splitlines():
        if not line.strip():
            continue

        digest, filename = line.split(None, 1)
        expected[filename.strip()] = digest

    if expected.get("execution_manifest.csv") != sha256(manifest):
        raise SystemExit("Manifest checksum verification FAILED.")

    with manifest.open(newline="") as f:
        rows = list(csv.DictReader(f))

    return manifest_dir, manifest, rows


def approval_path(run_id):
    return APPROVALS / f"{run_id}.json"


def load_approvals(run_id):
    path = approval_path(run_id)

    if not path.exists():
        return {
            "run_id": run_id,
            "approved_execution_ids": [],
            "history": [],
        }

    return json.loads(path.read_text())


def save_approvals(run_id, data):
    path = approval_path(run_id)

    path.write_text(
        json.dumps(data, indent=2) + "\n"
    )

    return path


parser = argparse.ArgumentParser(
    description="Review and approve individual Migratarr manifest rows."
)

parser.add_argument(
    "--run",
    help="Run ID. Defaults to latest manifest."
)

group = parser.add_mutually_exclusive_group(required=True)

group.add_argument(
    "--list",
    action="store_true",
    help="List all manifest rows."
)

group.add_argument(
    "--list-same-disk",
    action="store_true",
    help="List SAME_DISK_RENAME candidates."
)

group.add_argument(
    "--approve",
    metavar="EXECUTION_ID",
    help="Approve exactly one execution ID."
)

group.add_argument(
    "--approve-batch",
    action="store_true",
    help=(
        "Approve every eligible pending row matching --media and --transfer, "
        "smallest first. Shows a preview and approves nothing unless --yes is given."
    )
)

group.add_argument(
    "--revoke",
    metavar="EXECUTION_ID",
    help="Revoke one approval."
)

group.add_argument(
    "--status",
    action="store_true",
    help="Show current approval status."
)

parser.add_argument(
    "--media",
    choices=["Movie", "TV"],
    help="With --approve-batch: media type to approve."
)

parser.add_argument(
    "--transfer",
    choices=["SAME_DISK_RENAME", "CROSS_DISK_TRANSFER"],
    help="With --approve-batch: transfer type to approve."
)

parser.add_argument(
    "--limit",
    type=int,
    help="With --approve-batch: approve at most N rows (smallest first)."
)

parser.add_argument(
    "--yes",
    action="store_true",
    help="With --approve-batch: record the approvals shown in the preview."
)

args = parser.parse_args()

if args.approve_batch:
    if not (args.media and args.transfer):
        parser.error("--approve-batch requires --media and --transfer")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
elif args.media or args.transfer or args.limit is not None or args.yes:
    parser.error("--media, --transfer, --limit and --yes are only valid with --approve-batch")

manifest_dir, manifest_path, rows = load_manifest(args.run)

run_id = manifest_dir.name
approvals = load_approvals(run_id)

row_map = {
    r["execution_id"]: r
    for r in rows
}

approved = set(
    approvals.get("approved_execution_ids", [])
)


def print_row(r):
    marker = "APPROVED" if r["execution_id"] in approved else "UNAPPROVED"

    print(
        f'{r["execution_id"]} | '
        f'{r["media_type"]} | '
        f'{r["title"]} | '
        f'{r["current"]} -> {r["recommended"]} | '
        f'{r["transfer_type"]} | '
        f'{r["size_gb"]} GB | '
        f'{marker}'
    )


if args.list:
    for row in rows:
        print_row(row)

elif args.list_same_disk:
    candidates = [
        r for r in rows
        if r["transfer_type"] == "SAME_DISK_RENAME"
    ]

    print(f"Run: {run_id}")
    print(f"Same-disk candidates: {len(candidates)}")
    print()

    for row in candidates:
        print_row(row)

elif args.status:
    print(f"Run: {run_id}")
    print(f"Manifest rows: {len(rows)}")
    print(f"Approved: {len(approved)}")
    print()

    for execution_id in sorted(approved):
        row = row_map.get(execution_id)

        if row:
            print_row(row)
        else:
            print(
                f"{execution_id} | "
                "WARNING: no longer present in manifest"
            )

elif args.approve:
    execution_id = args.approve

    if execution_id not in row_map:
        raise SystemExit(
            f"Execution ID not found: {execution_id}"
        )

    row = row_map[execution_id]

    if row.get("status") != "READY_FOR_REVIEW":
        raise SystemExit(
            "Refusing approval: row is not READY_FOR_REVIEW."
        )

    if execution_id in approved:
        print(f"Already approved: {execution_id}")
        raise SystemExit(0)

    approved.add(execution_id)

    approvals["approved_execution_ids"] = sorted(approved)
    approvals.setdefault("history", []).append({
        "action": "APPROVE",
        "execution_id": execution_id,
        "utc": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": sha256(manifest_path),
    })

    path = save_approvals(run_id, approvals)

    print("APPROVED")
    print_row(row)
    print()
    print(f"Approval record: {path}")
    print("NO FILES WERE MOVED.")

elif args.approve_batch:
    logs = BASE / "execution_logs"

    def already_succeeded(execution_id):
        journal = logs / f"{execution_id}.jsonl"
        if not journal.exists():
            return False
        lines = [line for line in journal.read_text().splitlines() if line.strip()]
        return bool(lines) and json.loads(lines[-1]).get("event") == "SUCCESS"

    candidates = sorted(
        (
            r for r in rows
            if r.get("media_type") == args.media
            and r.get("transfer_type") == args.transfer
            and r.get("status") == "READY_FOR_REVIEW"
            and not r.get("blockers")
            and r.get("executed") == "NO"
            and r["execution_id"] not in approved
            and not already_succeeded(r["execution_id"])
        ),
        key=lambda r: float(r["size_gb"])
    )

    if args.limit is not None:
        candidates = candidates[:args.limit]

    total_gb = sum(float(r["size_gb"]) for r in candidates)

    print(f"Run: {run_id}")
    print(
        f"Batch: {args.media} {args.transfer}; "
        f"{len(candidates)} row(s), {total_gb:.2f} GB"
    )
    print()

    for row in candidates:
        print_row(row)

    if not candidates:
        print("Nothing to approve.")
        raise SystemExit(0)

    if not args.yes:
        print()
        print("PREVIEW ONLY: nothing approved.")
        print("Re-run with --yes to approve the rows listed above.")
        raise SystemExit(0)

    batch_utc = datetime.now(timezone.utc).isoformat()
    manifest_sha256 = sha256(manifest_path)
    batch = {
        "utc": batch_utc,
        "media_type": args.media,
        "transfer_type": args.transfer,
        "size": len(candidates),
    }

    for row in candidates:
        approved.add(row["execution_id"])
        # One APPROVE entry per row, exactly as --approve writes, so executors
        # verify batch approvals the same way; "batch" records the shared decision.
        approvals.setdefault("history", []).append({
            "action": "APPROVE",
            "execution_id": row["execution_id"],
            "utc": batch_utc,
            "manifest_sha256": manifest_sha256,
            "batch": batch,
        })

    approvals["approved_execution_ids"] = sorted(approved)

    path = save_approvals(run_id, approvals)

    print()
    print(f"APPROVED {len(candidates)} row(s) as one batch.")
    print(f"Approval record: {path}")
    print("NO FILES WERE MOVED.")

elif args.revoke:
    execution_id = args.revoke

    if execution_id not in approved:
        print(f"Not currently approved: {execution_id}")
        raise SystemExit(0)

    approved.remove(execution_id)

    approvals["approved_execution_ids"] = sorted(approved)
    approvals.setdefault("history", []).append({
        "action": "REVOKE",
        "execution_id": execution_id,
        "utc": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": sha256(manifest_path),
    })

    path = save_approvals(run_id, approvals)

    print(f"REVOKED: {execution_id}")
    print(f"Approval record: {path}")
    print("NO FILES WERE MOVED.")
