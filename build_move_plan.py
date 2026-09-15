#!/usr/bin/env python3

import csv
import os
from pathlib import Path
from collections import Counter

BASE = Path("/opt/media-stack/migratarr")

MOVIE_CSV = BASE / "movie_dry_run.csv"
TV_CSV = BASE / "tv_dry_run.csv"
OUTPUT = BASE / "move_plan.csv"

DESTINATION_ROOTS = {
    "Movie": {
        "Common": [
            Path("/mnt/nas/media01/Movies/Common"),
        ],
        "Rare": [
            Path("/mnt/nas/media02/Movies/Rare"),
        ],
        "Library": [
            Path("/mnt/nas/media03/Movies/Library"),
            Path("/mnt/nas/media04/Movies/Library"),
        ],
        "Archive": [
            Path("/mnt/nas/media04/Movies/Archive"),
        ],
    },
    "TV": {
        "Current": [
            Path("/mnt/nas/media01/TV/Current"),
        ],
        "Rare": [
            Path("/mnt/nas/media02/TV/Rare"),
        ],
        "Library": [
            Path("/mnt/nas/media03/TV/Library"),
            Path("/mnt/nas/media04/TV/Library"),
        ],
        "Archive": [
            Path("/mnt/nas/media04/TV/Archive"),
        ],
    },
}

# Require this much free space to remain after any proposed move.
MIN_FREE_AFTER_GB = 50


def read_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def dir_size_bytes(path):
    total = 0

    try:
        if path.is_file():
            return path.stat().st_size

        for root, dirs, files in os.walk(path):
            for name in files:
                p = Path(root) / name

                try:
                    total += p.stat().st_size
                except (FileNotFoundError, PermissionError):
                    pass

    except (FileNotFoundError, PermissionError):
        return None

    return total


def free_bytes(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


def gb(value):
    if value is None:
        return ""

    return round(value / (1024 ** 3), 2)


def resolve_host_source(media_type, arr_path, current):
    """
    Arr reports a logical /media path, but the same logical category may
    physically live on more than one NAS disk.

    Resolve the real host path by searching all known NAS mounts for the
    exact media-directory name inside the expected logical category.
    """

    arr = Path(arr_path)
    folder_name = arr.name

    if media_type == "Movie":
        category_path = Path("Movies") / current
    else:
        category_path = Path("TV") / current

    candidates = []

    for disk in [
        Path("/mnt/nas/media01"),
        Path("/mnt/nas/media02"),
        Path("/mnt/nas/media03"),
        Path("/mnt/nas/media04"),
    ]:
        candidate = disk / category_path / folder_name

        if candidate.exists():
            candidates.append(candidate)

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        # Return a special non-existent path so the planner blocks the move.
        # We do not guess when duplicate folders exist.
        return Path(
            f"/__MIGRATARR_AMBIGUOUS__/{media_type}/{current}/{folder_name}"
        )

    # Fall back to the logical Arr path so SOURCE_MISSING is triggered.
    return arr


def target_path_for(source, destination_root):
    # Preserve the existing movie/series folder name.
    return destination_root / source.name


PROJECTED_FREE = {}


def physical_disk(path):
    """
    Return media01/media02/media03/media04 for a NAS path.
    """
    path = Path(path)

    if (
        len(path.parts) > 3
        and path.parts[:3] == ("/", "mnt", "nas")
    ):
        return path.parts[3]

    return ""


def projected_free_for(root):
    disk = physical_disk(root)

    if disk not in PROJECTED_FREE:
        actual = free_bytes(root)

        if actual is not None:
            PROJECTED_FREE[disk] = actual

    return PROJECTED_FREE.get(disk)


def choose_destination_root(
    media_type,
    recommended,
    source,
):
    """
    Choose physical storage independently from logical category.

    1. Prefer keeping the item on its current physical disk if that disk
       supports the destination category.
    2. Otherwise choose the eligible disk with the most projected free space.
    """

    roots = [
        r for r in DESTINATION_ROOTS[media_type][recommended]
        if r.exists()
    ]

    if not roots:
        return None

    source_disk = physical_disk(source)

    # Avoid needless cross-disk copies whenever possible.
    for root in roots:
        if physical_disk(root) == source_disk:
            return root

    # Otherwise load-balance using projected free space.
    valid = []

    for root in roots:
        free = projected_free_for(root)

        if free is not None:
            valid.append((free, root))

    if not valid:
        return roots[0]

    valid.sort(
        key=lambda item: item[0],
        reverse=True
    )

    return valid[0][1]


def evaluate_move(
    media_type,
    title,
    source_path,
    current,
    recommended,
    final_score,
    replacement,
    confidence,
    decision_reason,
):
    source = resolve_host_source(
        media_type,
        source_path,
        current
    )

    size = dir_size_bytes(source) if source.exists() else None

    dest_root = choose_destination_root(
        media_type,
        recommended,
        source,
    )

    blockers = []
    warnings = []

    if dest_root is None:
        blockers.append("NO_ELIGIBLE_DESTINATION")
        target = Path(
            f"/__MIGRATARR_NO_DESTINATION__/"
            f"{media_type}/{recommended}/{source.name}"
        )
        free = None
    else:
        target = target_path_for(source, dest_root)
        free = projected_free_for(dest_root)

    if not source.exists():
        blockers.append("SOURCE_MISSING")

    if not dest_root.exists():
        blockers.append("DESTINATION_ROOT_MISSING")

    if target.exists() and target.resolve() != source.resolve():
        blockers.append("DESTINATION_COLLISION")

    if confidence in {"low", ""}:
        warnings.append("LOW_OR_UNKNOWN_REPLACEMENT_CONFIDENCE")

    if recommended == "Rare":
        warnings.append("RARE_PROMOTION_REVIEW")

    if recommended == "Archive":
        warnings.append("ARCHIVE_MOVE_REVIEW")

    # Moving something out of Rare deserves explicit review.
    if current == "Rare" and recommended != "Rare":
        warnings.append("RARE_DEMOTION_REVIEW")

    if size is not None and free is not None:
        min_remaining = MIN_FREE_AFTER_GB * 1024 ** 3

        if free - size < min_remaining:
            blockers.append("INSUFFICIENT_DESTINATION_SPACE")

    # Any category change means Radarr/Sonarr will eventually need its
    # database path/root updated. We are NOT doing that here.
    arr_update_required = current != recommended

    # Determine whether this is an inexpensive same-filesystem rename
    # or a real cross-disk transfer.
    source_disk = physical_disk(source)
    target_disk = physical_disk(target)

    if source_disk and target_disk and source_disk == target_disk:
        transfer_type = "SAME_DISK_RENAME"
    else:
        transfer_type = "CROSS_DISK_TRANSFER"

    # Reserve projected capacity immediately so later planned moves
    # choose destinations using the evolving whole-plan state.
    if (
        not blockers
        and size is not None
        and source_disk
        and target_disk
        and source_disk != target_disk
    ):
        source_root = Path("/mnt/nas") / source_disk

        if source_disk not in PROJECTED_FREE:
            src_free = free_bytes(source_root)

            if src_free is not None:
                PROJECTED_FREE[source_disk] = src_free

        if target_disk not in PROJECTED_FREE:
            dst_free = free_bytes(dest_root)

            if dst_free is not None:
                PROJECTED_FREE[target_disk] = dst_free

        reserve = MIN_FREE_AFTER_GB * (1024 ** 3)

        projected_target = (
            PROJECTED_FREE.get(target_disk, 0) - size
        )

        if projected_target < reserve:
            blockers.append("INSUFFICIENT_PROJECTED_SPACE")
        else:
            PROJECTED_FREE[source_disk] += size
            PROJECTED_FREE[target_disk] -= size

    status = "BLOCKED" if blockers else "READY_FOR_REVIEW"

    return {
        "media_type": media_type,
        "title": title,
        "current": current,
        "recommended": recommended,
        "source_path": str(source),
        "target_path": str(target),
        "size_gb": gb(size),
        "destination_free_gb": gb(free),
        "free_after_move_gb": (
            gb(free - size)
            if free is not None and size is not None
            else ""
        ),
        "source_disk": source_disk,
        "target_disk": target_disk,
        "transfer_type": transfer_type,
        "final_score": final_score,
        "replacement": replacement,
        "replacement_confidence": confidence,
        "decision_reason": decision_reason,
        "arr_path_update_required": (
            "YES" if arr_update_required else ""
        ),
        "status": status,
        "blockers": ";".join(blockers),
        "warnings": ";".join(warnings),
    }


plans = []


# ------------------------------------------------------------
# MOVIES
# ------------------------------------------------------------

for row in read_csv(MOVIE_CSV):

    current = row["current"]
    recommended = row["recommended"]

    if recommended == "HOLD":
        continue

    if current == recommended:
        continue

    if recommended not in DESTINATION_ROOTS["Movie"]:
        continue

    plans.append(
        evaluate_move(
            media_type="Movie",
            title=f'{row["title"]} ({row["year"]})',
            source_path=row["path"],
            current=current,
            recommended=recommended,
            final_score=row["final_score"],
            replacement=row["replacement"],
            confidence=row["replacement_confidence"],
            decision_reason=row["decision_reason"],
        )
    )


# ------------------------------------------------------------
# TV
# ------------------------------------------------------------

for row in read_csv(TV_CSV):

    current = row["current"]
    recommended = row["recommended"]

    if recommended == "HOLD":
        continue

    if current == recommended:
        continue

    if recommended not in DESTINATION_ROOTS["TV"]:
        continue

    plans.append(
        evaluate_move(
            media_type="TV",
            title=row["title"],
            source_path=row["path"],
            current=current,
            recommended=recommended,
            final_score=row["final_score"],
            replacement=row["replacement"],
            confidence=row["replacement_confidence"],
            decision_reason=row["decision_reason"],
        )
    )


FIELDS = [
    "media_type",
    "title",
    "current",
    "recommended",
    "source_path",
    "target_path",
    "size_gb",
    "destination_free_gb",
    "free_after_move_gb",
    "source_disk",
    "target_disk",
    "transfer_type",
    "final_score",
    "replacement",
    "replacement_confidence",
    "decision_reason",
    "arr_path_update_required",
    "status",
    "blockers",
    "warnings",
]


with OUTPUT.open("w", newline="") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=FIELDS
    )

    writer.writeheader()
    writer.writerows(plans)


print("=" * 72)
print("MIGRATARR MOVE PLAN")
print("=" * 72)

print(f"Proposed moves: {len(plans)}")

print("\n=== STATUS ===")
for key, value in Counter(
    p["status"] for p in plans
).items():
    print(f"{key:<20} {value}")

print("\n=== BY MEDIA TYPE ===")
for key, value in Counter(
    p["media_type"] for p in plans
).items():
    print(f"{key:<10} {value}")

print("\n=== CATEGORY MOVES ===")
for (media, src, dst), count in sorted(
    Counter(
        (
            p["media_type"],
            p["current"],
            p["recommended"]
        )
        for p in plans
    ).items()
):
    print(
        f"{media:<6} "
        f"{src:<10} -> {dst:<10} "
        f"{count}"
    )

# ------------------------------------------------------------
# CUMULATIVE DISK CAPACITY CHECK
# ------------------------------------------------------------

disk_roots = {
    "media01": Path("/mnt/nas/media01"),
    "media02": Path("/mnt/nas/media02"),
    "media03": Path("/mnt/nas/media03"),
    "media04": Path("/mnt/nas/media04"),
}

disk_free = {}
disk_incoming = {disk: 0 for disk in disk_roots}
disk_outgoing = {disk: 0 for disk in disk_roots}

for disk, root in disk_roots.items():
    disk_free[disk] = free_bytes(root)

for plan in plans:
    if plan["status"] == "BLOCKED":
        continue

    try:
        size = float(plan["size_gb"]) * (1024 ** 3)
    except (TypeError, ValueError):
        continue

    src = plan["source_disk"]
    dst = plan["target_disk"]

    if src and dst and src != dst:
        disk_outgoing[src] += size
        disk_incoming[dst] += size

print("\n=== CUMULATIVE DISK CAPACITY ===")

cumulative_blocked_disks = set()

for disk in disk_roots:
    free = disk_free[disk]
    incoming = disk_incoming[disk]
    outgoing = disk_outgoing[disk]

    projected = free + outgoing - incoming
    reserve = MIN_FREE_AFTER_GB * (1024 ** 3)

    print(
        f"{disk}: "
        f"free_now={gb(free)} GB | "
        f"in={gb(incoming)} GB | "
        f"out={gb(outgoing)} GB | "
        f"projected_free={gb(projected)} GB"
    )

    if projected < reserve:
        cumulative_blocked_disks.add(disk)

if cumulative_blocked_disks:
    print(
        "\nCUMULATIVE CAPACITY BLOCK: "
        + ", ".join(sorted(cumulative_blocked_disks))
    )

    for plan in plans:
        if (
            plan["target_disk"] in cumulative_blocked_disks
            and plan["source_disk"] != plan["target_disk"]
        ):
            plan["status"] = "BLOCKED"

            existing = plan["blockers"]
            extra = "CUMULATIVE_DESTINATION_SPACE"

            plan["blockers"] = (
                existing + ";" + extra
                if existing
                else extra
            )

# Rewrite CSV after cumulative status updates.
with OUTPUT.open("w", newline="") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=FIELDS
    )
    writer.writeheader()
    writer.writerows(plans)


print("\n=== BLOCKED ===")
blocked = [
    p for p in plans
    if p["status"] == "BLOCKED"
]

if not blocked:
    print("None")
else:
    for p in blocked:
        print(
            f'{p["media_type"]}: {p["title"]} | '
            f'{p["blockers"]}'
        )

print()
print("NO FILES WERE MOVED.")
print(f"Plan written to: {OUTPUT}")
