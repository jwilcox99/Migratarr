#!/usr/bin/env python3

import json
import urllib.request

from planner_settings import TAG_NAMESPACE, get_settings
from runtime_config import get_config
from service_keys import service_endpoint
RUNTIME = get_config()


# Arr tag labels from config/planner.json "overrides" (default: migratarr-*),
# compared lowercased as build_move_plan.py and the executors do.
OVERRIDES = get_settings().overrides


def get_json(url, key):
    req = urllib.request.Request(
        url,
        headers={"X-Api-Key": key}
    )

    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def meaning(label):
    """What the planner and executors do with `label`, or None if they ignore it."""
    label = label.lower()

    if label == OVERRIDES.lock_tag:
        return "lock"

    if label in OVERRIDES.category_tags:
        return "category " + OVERRIDES.category_tags[label]

    return None


def unrecognized(label):
    """A Migratarr-looking tag that the current configuration ignores."""
    return meaning(label) is None and label.lower().startswith(TAG_NAMESPACE)


def audit(name, url, key, endpoint):
    tags = get_json(f"{url}/api/v3/tag", key)

    tag_map = {
        t["id"]: t["label"]
        for t in tags
    }

    known = {
        tid: label
        for tid, label in tag_map.items()
        if meaning(label)
    }

    stray = {
        tid: label
        for tid, label in tag_map.items()
        if unrecognized(label)
    }

    print()
    print("=" * 72)
    print(name)
    print("=" * 72)

    if not known and not stray:
        print("No Migratarr override tags currently exist.")
        return

    print("Configured override tags:")

    if not known:
        print("  None")

    for tid, label in known.items():
        print(f"  {tid}: {label} ({meaning(label)})")

    if stray:
        print(
            f"\nUnrecognized {TAG_NAMESPACE}* tags "
            "(not in config/planner.json overrides; planner and executors ignore them):"
        )

        for tid, label in stray.items():
            print(f"  {tid}: {label}")

    relevant = {**known, **stray}

    items = get_json(
        f"{url}/api/v3/{endpoint}",
        key
    )

    print("\nTagged media:")

    found = False

    for item in items:
        labels = [
            relevant[tag] if tag in known else relevant[tag] + " (unrecognized)"
            for tag in item.get("tags", [])
            if tag in relevant
        ]

        if labels:
            found = True
            print(
                f'{item.get("title")} | '
                + ", ".join(labels)
            )

    if not found:
        print("None")


def main():
    # API roots and keys: runtime.json urls + "secrets" (see service_keys.py).
    radarr_url, radarr_key = service_endpoint("radarr", RUNTIME)
    sonarr_url, sonarr_key = service_endpoint("sonarr", RUNTIME)

    audit(
        "RADARR",
        radarr_url,
        radarr_key,
        "movie"
    )

    audit(
        "SONARR",
        sonarr_url,
        sonarr_key,
        "series"
    )


if __name__ == "__main__":
    main()
