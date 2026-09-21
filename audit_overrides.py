#!/usr/bin/env python3

import json
import subprocess
import urllib.request

from runtime_config import get_config
RUNTIME = get_config()

RADARR_URL = RUNTIME.urls["radarr"]
SONARR_URL = RUNTIME.urls["sonarr"]

PREFIX = "migratarr-"


def docker_key(container):
    return subprocess.check_output(
        [
            "docker", "exec", container, "sh", "-c",
            r"""sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' /config/config.xml"""
        ],
        text=True
    ).strip()


def get_json(url, key):
    req = urllib.request.Request(
        url,
        headers={"X-Api-Key": key}
    )

    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def audit(name, url, key, endpoint):
    tags = get_json(f"{url}/api/v3/tag", key)

    tag_map = {
        t["id"]: t["label"]
        for t in tags
    }

    relevant = {
        tid: label
        for tid, label in tag_map.items()
        if label.lower().startswith(PREFIX)
    }

    print()
    print("=" * 72)
    print(name)
    print("=" * 72)

    if not relevant:
        print("No Migratarr override tags currently exist.")
        return

    print("Known override tags:")

    for tid, label in relevant.items():
        print(f"  {tid}: {label}")

    items = get_json(
        f"{url}/api/v3/{endpoint}",
        key
    )

    print("\nTagged media:")

    found = False

    for item in items:
        labels = [
            relevant[tag]
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


radarr_key = docker_key(RUNTIME.containers["radarr"])
sonarr_key = docker_key(RUNTIME.containers["sonarr"])

audit(
    "RADARR",
    RADARR_URL,
    radarr_key,
    "movie"
)

audit(
    "SONARR",
    SONARR_URL,
    sonarr_key,
    "series"
)
