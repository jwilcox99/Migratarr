"""Validated storage policy configuration for the standalone engine.

This module does not read the NAS or alter the existing planner. Its JSON
format is versioned so future policy changes do not silently reinterpret a
saved configuration.
"""

import json
from pathlib import Path, PurePosixPath

from .engine import GIB, ValidationPolicy


MEDIA_TYPES = {"Movie", "TV"}
TOP_LEVEL = {"schema_version", "source_roots", "category_paths",
             "destination_disks", "min_free_after_gb"}


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must have exactly: {', '.join(sorted(expected))}")


def _category_name(name):
    if not isinstance(name, str) or not name or name in {".", "..", "HOLD"}:
        raise ValueError("Category names must be nonempty strings")
    if "/" in name or "\\" in name:
        raise ValueError(f"Category name cannot contain a path separator: {name}")


def _relative_directory(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError(f"{label} must stay below its source root")
    if "\\" in value or "//" in value or value.startswith("/") or value.endswith("/"):
        raise ValueError(f"{label} must use a normalized relative POSIX path")
    return Path(value)


def parse_policy(data):
    """Validate one JSON-compatible object and build a filesystem-free policy."""
    _keys(data, TOP_LEVEL, "Configuration")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    reserve = data["min_free_after_gb"]
    if type(reserve) is not int or reserve < 0:
        raise ValueError("min_free_after_gb must be a nonnegative integer")
    sources = data["source_roots"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("source_roots must be a nonempty array")
    roots = []
    names = set()
    for value in sources:
        if not isinstance(value, str) or not value:
            raise ValueError("Each source root must be an absolute POSIX path")
        posix = PurePosixPath(value)
        if (not posix.is_absolute() or any(part in {".", ".."} for part in value.split("/"))
                or "//" in value or value.endswith("/") or "\\" in value):
            raise ValueError(f"Invalid source root: {value}")
        if not posix.name or posix.name in names:
            raise ValueError(f"Source root disk names must be unique: {value}")
        names.add(posix.name)
        roots.append(Path(value))
    for i, root in enumerate(roots):
        if any(root in other.parents or other in root.parents
               for other in roots[i + 1:]):
            raise ValueError("Source roots must not overlap")

    _keys(data["category_paths"], MEDIA_TYPES, "category_paths")
    _keys(data["destination_disks"], MEDIA_TYPES, "destination_disks")
    category_paths = {}
    destinations = {}
    by_name = {root.name: root for root in roots}
    for media in ("Movie", "TV"):
        raw_paths = data["category_paths"][media]
        raw_disks = data["destination_disks"][media]
        if not isinstance(raw_paths, dict) or not raw_paths:
            raise ValueError(f"{media} category_paths must be a nonempty object")
        if not isinstance(raw_disks, dict) or not raw_disks:
            raise ValueError(f"{media} destination_disks must be a nonempty object")
        category_paths[media] = {}
        for category, value in raw_paths.items():
            _category_name(category)
            category_paths[media][category] = _relative_directory(
                value, f"{media}/{category}"
            )
        if len(set(category_paths[media].values())) != len(category_paths[media]):
            raise ValueError(f"{media} categories must use distinct directories")
        destinations[media] = {}
        for category, disks in raw_disks.items():
            if category not in category_paths[media]:
                raise ValueError(f"Missing category path for {media}/{category}")
            if (not isinstance(disks, list) or not disks
                    or any(not isinstance(disk, str) for disk in disks)
                    or len(disks) != len(set(disks))):
                raise ValueError(f"{media}/{category} needs unique destination disks")
            if any(disk not in by_name for disk in disks):
                raise ValueError(f"{media}/{category} names an unknown destination disk")
            destinations[media][category] = tuple(
                by_name[disk] / category_paths[media][category] for disk in disks
            )
    return ValidationPolicy(destinations, tuple(roots), reserve * GIB,
                            category_paths)


def load_policy(path):
    return parse_policy(json.loads(path.read_text(encoding="utf-8")))
