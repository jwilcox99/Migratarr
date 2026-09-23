#!/usr/bin/env python3

import csv
import json
import os
import random
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from runtime_config import get_config
from planner_settings import at_least, at_most, load_settings
RUNTIME = get_config()
PLANNER_SETTINGS = load_settings()

# ============================================================
# CONFIG
# ============================================================

RADARR_URL = RUNTIME.urls["radarr"]
JELLYFIN_URL = RUNTIME.urls["jellyfin"]

SAMPLE_SIZE = 999999
RANDOM_SEED = 420

CACHE_DIR = (RUNTIME.base_path / "cache")
OUTPUT = (RUNTIME.base_path / "movie_dry_run.csv")

CACHE_DAYS = 7

# Streaming preferences come from config/planner.json (see planner_settings.py).
# SUBSCRIBED/USER_FREE_ACCESS hold provider_family() names; STREAMING_REGION is
# the TMDB watch/providers region whose availability is scored.
SUBSCRIBED = set(PLANNER_SETTINGS.subscribed)
USER_FREE_ACCESS = set(PLANNER_SETTINGS.user_free_access)
STREAMING_REGION = PLANNER_SETTINGS.region
# Every scoring weight, tier and threshold (config/planner.json "scoring").
SCORING = PLANNER_SETTINGS.scoring

# ============================================================
# HELPERS
# ============================================================

def docker_output(container, command):
    return subprocess.check_output(
        ["docker", "exec", container, "sh", "-c", command],
        text=True
    ).strip()


def get_radarr_key():
    return docker_output(
        RUNTIME.containers["radarr"],
        r"""sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' /config/config.xml"""
    )


def get_jellyfin_key():
    paths = [
        "/run/secrets/jellyfin_api_key",
        "/run/secrets/jellyfin_key",
    ]

    for path in paths:
        try:
            value = docker_output(RUNTIME.containers["homepage"], f"cat {path}")
            if value:
                return value
        except Exception:
            pass

    raise RuntimeError("Could not read Jellyfin key from Homepage secrets")


RADARR_KEY = get_radarr_key()
JELLYFIN_KEY = get_jellyfin_key()
TMDB_TOKEN = os.environ["TMDB_TOKEN"]


def request_json(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or {})

    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def radarr(path, timeout=120):
    return request_json(
        RADARR_URL + path,
        headers={"X-Api-Key": RADARR_KEY},
        timeout=timeout
    )


def jellyfin(path, timeout=60):
    return request_json(
        JELLYFIN_URL + path,
        headers={"X-Emby-Token": JELLYFIN_KEY},
        timeout=timeout
    )


def cache_path(namespace, key):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key))
    return CACHE_DIR / f"{namespace}_{safe}.json"


def cached(namespace, key, loader):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = cache_path(namespace, key)

    if p.exists():
        age = time.time() - p.stat().st_mtime

        if age < CACHE_DAYS * 86400:
            with p.open() as f:
                return json.load(f), True

    data = loader()

    with p.open("w") as f:
        json.dump(data, f)

    return data, False


# ============================================================
# REPLACEMENT SCARCITY
# ============================================================

SOFT_PATTERNS = [
    re.compile(r"^Existing file meets cutoff:"),
    re.compile(r"^Quality for release in queue already meets cutoff:"),
    re.compile(
        r"^Existing file and the Quality profile does not allow upgrades$"
    ),
]


def rejection_texts(release):
    values = release.get("rejections") or release.get("rejected") or []

    result = []

    for value in values:
        if isinstance(value, str):
            result.append(value)

        elif isinstance(value, dict):
            result.append(
                value.get("message")
                or value.get("reason")
                or str(value)
            )

    return result


def soft_only(release):
    rejections = rejection_texts(release)

    if not rejections:
        return True

    for reason in rejections:
        if not any(p.search(reason) for p in SOFT_PATTERNS):
            return False

    return True


def normalize_release(title):
    t = title.lower()

    # Remove punctuation without trying to infer indexer identity.
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t)

    return t.strip()


def scarcity_from_count(count):
    return at_least(count, SCORING.replacement.tiers, SCORING.replacement.none)


def replacement_score(movie):
    movie_id = movie["id"]

    def loader():
        return radarr(f"/api/v3/release?movieId={movie_id}")

    try:
        releases, from_cache = cached(
            "radarr_movie",
            movie_id,
            loader
        )

        viable = {
            normalize_release(r.get("title", ""))
            for r in releases
            if r.get("title") and soft_only(r)
        }

        viable.discard("")

        count = len(viable)
        score = scarcity_from_count(count)

        return score, count, "high", from_cache

    except Exception as e:
        return None, None, "low", False


# ============================================================
# STREAMING SCARCITY
# ============================================================

def provider_family(name):
    n = name.lower()

    if "paramount" in n:
        return "Paramount+"

    if "amazon prime video" in n:
        return "Prime Video"

    if "apple tv" in n:
        return "Apple TV"

    if "disney" in n:
        return "Disney+"

    if "hulu" in n:
        return "Hulu"

    if "mgm" in n:
        return "MGM+"

    if "peacock" in n:
        return "Peacock"

    if "starz" in n:
        return "Starz"

    if "max" in n or "hbo" in n:
        return "Max"

    return name


def tmdb_movie_providers(tmdb_id):
    def loader():
        return request_json(
            f"https://api.themoviedb.org/3/movie/"
            f"{tmdb_id}/watch/providers",
            headers={
                "Authorization": f"Bearer {TMDB_TOKEN}",
                "accept": "application/json"
            },
            timeout=30
        )

    return cached("tmdb_provider", tmdb_id, loader)

def streaming_score(tmdb_id):
    try:
        data, from_cache = tmdb_movie_providers(tmdb_id)

        us = data.get("results", {}).get(STREAMING_REGION, {})

        flat = {
            provider_family(x["provider_name"])
            for x in us.get("flatrate", [])
        }

        free = {
            provider_family(x["provider_name"])
            for x in us.get("free", [])
        }

        ads = {
            provider_family(x["provider_name"])
            for x in us.get("ads", [])
        }

        rent = {
            provider_family(x["provider_name"])
            for x in us.get("rent", [])
        }

        buy = {
            provider_family(x["provider_name"])
            for x in us.get("buy", [])
        }

        subscribed = flat & SUBSCRIBED

        if subscribed:
            return SCORING.streaming.subscribed, "Subscribed: " + ", ".join(sorted(subscribed))

        accessible_free = free & USER_FREE_ACCESS

        if accessible_free:
            return SCORING.streaming.free_access, "Free access: " + ", ".join(
                sorted(accessible_free)
            )

        if ads:
            return SCORING.streaming.ads, "Free with ads: " + ", ".join(sorted(ads))

        unsubscribed = flat - SUBSCRIBED

        if len(unsubscribed) >= 3:
            return SCORING.streaming.three_or_more_families, f"{len(unsubscribed)} subscription families"

        if len(unsubscribed) == 2:
            return SCORING.streaming.two_families, "2 subscription families"

        if len(unsubscribed) == 1:
            return SCORING.streaming.one_family, (
                "1 subscription family: "
                + next(iter(unsubscribed))
            )

        if rent:
            return SCORING.streaming.rental, "Rental available"

        if buy:
            return SCORING.streaming.purchase, "Purchase only"

        return SCORING.streaming.unavailable, f"No {STREAMING_REGION} availability found"

    except Exception:
        return None, "Streaming lookup failed"


# ============================================================
# JELLYFIN USAGE
# ============================================================

def parse_dt(value):
    if not value:
        return None

    value = value.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except Exception:
        return None


def recency_points(last_played):
    if not last_played:
        return SCORING.usage.recency_never

    days = (
        datetime.now(timezone.utc) - last_played
    ).total_seconds() / 86400

    return at_most(days, SCORING.usage.recency_days, SCORING.usage.recency_older)


def repeat_points(play_count):
    # Proxy using Jellyfin aggregate PlayCount.
    # Playback Reporting can replace this later.
    return at_least(play_count, SCORING.usage.plays, SCORING.usage.plays_none)


def users_points(users):
    return at_least(users, SCORING.usage.users, SCORING.usage.users_none)


print("Loading Jellyfin users and movie usage...")

jf_users = jellyfin("/Users")

usage = {}
date_created = {}

for user in jf_users:
    uid = user["Id"]

    params = urllib.parse.urlencode({
        "Recursive": "true",
        "IncludeItemTypes": "Movie",
        "Fields": "ProviderIds,DateCreated",
        "EnableUserData": "true",
    })

    data = jellyfin(
        f"/Users/{uid}/Items?{params}"
    )

    for item in data.get("Items", []):
        tmdb = (
            item.get("ProviderIds", {})
            .get("Tmdb")
        )

        if not tmdb:
            continue

        tmdb = str(tmdb)

        ud = item.get("UserData") or {}

        plays = int(ud.get("PlayCount") or 0)
        last = parse_dt(ud.get("LastPlayedDate"))

        date_created.setdefault(
            tmdb,
            parse_dt(item.get("DateCreated"))
        )

        row = usage.setdefault(
            tmdb,
            {
                "plays": 0,
                "users": 0,
                "last": None,
            }
        )

        row["plays"] += plays

        if plays > 0:
            row["users"] += 1

        if last and (
            row["last"] is None
            or last > row["last"]
        ):
            row["last"] = last


def usage_score(tmdb_id):
    key = str(tmdb_id)

    u = usage.get(
        key,
        {"plays": 0, "users": 0, "last": None}
    )

    created = date_created.get(key)

    # New unwatched grace.
    if u["plays"] == 0 and created:
        age_days = (
            datetime.now(timezone.utc) - created
        ).total_seconds() / 86400

        grace = SCORING.usage

        if age_days <= grace.grace_full_days:
            return grace.grace_points, "New unwatched grace"

        if age_days <= grace.grace_end_days:
            score = grace.grace_points * (grace.grace_end_days - age_days) / (
                grace.grace_end_days - grace.grace_full_days)
            return round(score, 1), "Decaying new-item grace"

        return 0, "Never played"

    recency = recency_points(u["last"])
    repeat = repeat_points(u["plays"])
    distinct = users_points(u["users"])

    score = (
        SCORING.usage.weights.recency * recency
        + SCORING.usage.weights.repeat * repeat
        + SCORING.usage.weights.users * distinct
    )

    return round(score, 1), (
        f"{u['plays']} plays / "
        f"{u['users']} users"
    )


# ============================================================
# FRANCHISE BONUS
# ============================================================

def collection_info(movie):
    collection = movie.get("collection")

    if not collection:
        return None

    if isinstance(collection, dict):
        return (
            collection.get("tmdbId")
            or collection.get("id")
            or collection.get("title")
        )

    return str(collection)


# ============================================================
# CURRENT LOCATION / LIFECYCLE
# ============================================================

def current_bucket(path):
    p = (path or "").lower()

    if "/rare/" in p:
        return "Rare"

    if "/library/" in p:
        return "Library"

    if "/archive/" in p:
        return "Archive"

    if "/common/" in p:
        return "Common"

    return "Unknown"


def movie_age_years(movie):
    year = movie.get("year")

    if not year:
        return None

    return datetime.now().year - int(year)


def days_since_last_play(tmdb_id):
    u = usage.get(str(tmdb_id))

    if not u or not u["last"]:
        return None

    return (
        datetime.now(timezone.utc) - u["last"]
    ).total_seconds() / 86400


def days_since_added(tmdb_id):
    created = date_created.get(str(tmdb_id))

    if not created:
        return None

    return (
        datetime.now(timezone.utc) - created
    ).total_seconds() / 86400


def archive_eligible(movie, replacement):
    if replacement is None:
        return False

    rules = SCORING.archive

    if replacement >= rules.max_replacement:
        return False

    age = movie_age_years(movie)

    if age is None or age < rules.movie_min_age_years:
        return False

    added = days_since_added(movie["tmdbId"])

    if added is None or added < rules.min_days_since_added:
        return False

    last_play = days_since_last_play(movie["tmdbId"])

    # Never played is considered cold once grace has expired.
    if last_play is not None and last_play < rules.min_days_since_played:
        return False

    return True


# ============================================================
# LOAD RADARR LIBRARY
# ============================================================

movies = [
    m for m in radarr("/api/v3/movie")
    if m.get("hasFile")
]

print(f"Movies with files: {len(movies)}")

# Determine collections already intentionally protected.
collection_members = {}

for m in movies:
    cid = collection_info(m)

    if cid is None:
        continue

    collection_members.setdefault(
        str(cid),
        []
    ).append(m)


def franchise_bonus(movie):
    cid = collection_info(movie)

    if cid is None:
        return 0, ""

    rules = SCORING.franchise
    bonus = rules.collection_bonus
    reason = "Recognized collection"

    members = collection_members.get(str(cid), [])

    protected = sum(
        current_bucket(m.get("path")) in {"Library", "Rare"}
        for m in members
    )

    if protected >= rules.min_protected:
        bonus += rules.protected_bonus
        reason += "; related titles already protected"

    return min(bonus, rules.max_bonus), reason


# Deterministic random sample.
random.seed(RANDOM_SEED)

if len(movies) > SAMPLE_SIZE:
    selected = random.sample(movies, SAMPLE_SIZE)
else:
    selected = movies

rows = []
RARE = SCORING.thresholds.rare
LIBRARY = SCORING.thresholds.library

print(f"Scoring {len(selected)} movies.")
print("No files will be moved.")
print()

for i, movie in enumerate(selected, 1):

    title = movie.get("title", "Unknown")
    tmdb_id = movie.get("tmdbId")

    print(
        f"[{i:03}/{len(selected):03}] "
        f"{title}"
    )

    replacement, viable, repl_conf, cached_result = (
        replacement_score(movie)
    )

    streaming, streaming_reason = (
        streaming_score(tmdb_id)
    )

    usage_value, usage_reason = (
        usage_score(tmdb_id)
    )

    bonus, bonus_reason = franchise_bonus(movie)

    if replacement is None or streaming is None:
        final = None
        recommendation = "HOLD"
        reason = "Incomplete external data"

    else:
        final = (
            SCORING.weights.replacement * replacement
            + SCORING.weights.streaming * streaming
            + SCORING.weights.usage * usage_value
            + bonus
        )

        final = min(100, round(final, 1))

        if (streaming == SCORING.streaming.unavailable
                and replacement >= SCORING.thresholds.blackout_min_replacement):
            recommendation = "Rare"
            reason = (
                "Streaming blackout + limited replacement paths"
            )

        elif final >= RARE:
            recommendation = "Rare"
            reason = f"Placement score >={RARE:g}"

        elif archive_eligible(movie, replacement):
            recommendation = "Archive"
            reason = (
                "Old + cold + easy to replace"
            )

        elif final >= LIBRARY:
            recommendation = "Library"
            reason = f"Placement score {LIBRARY:g}-{RARE - 0.1:g}"

        else:
            recommendation = "Common"
            reason = f"Placement score <{LIBRARY:g}"

    current = current_bucket(movie.get("path"))

    threshold_flag = ""

    if final is not None:
        if abs(final - LIBRARY) <= SCORING.thresholds.near_margin:
            threshold_flag = f"NEAR_{LIBRARY:g}"

        elif abs(final - RARE) <= SCORING.thresholds.near_margin:
            threshold_flag = f"NEAR_{RARE:g}"

    proposed_change = (
        recommendation not in {"HOLD", current}
    )

    rows.append({
        "title": title,
        "year": movie.get("year"),
        "radarr_id": movie.get("id"),
        "tmdb_id": tmdb_id,
        "current": current,
        "recommended": recommendation,
        "change": "YES" if proposed_change else "",
        "final_score": final,
        "replacement": replacement,
        "viable_releases": viable,
        "replacement_confidence": repl_conf,
        "streaming": streaming,
        "streaming_reason": streaming_reason,
        "usage": usage_value,
        "usage_reason": usage_reason,
        "franchise_bonus": bonus,
        "franchise_reason": bonus_reason,
        "archive_eligible": (
            replacement is not None
            and archive_eligible(movie, replacement)
        ),
        "threshold_flag": threshold_flag,
        "decision_reason": reason,
        "path": movie.get("path"),
    })

    # Be polite to Radarr/indexers when not cached.
    if not cached_result:
        time.sleep(1)


# ============================================================
# WRITE CSV
# ============================================================

fields = list(rows[0].keys())

with OUTPUT.open("w", newline="") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=fields
    )

    writer.writeheader()
    writer.writerows(rows)


# ============================================================
# SUMMARY
# ============================================================

print()
print("=" * 72)
print("DRY RUN COMPLETE")
print("=" * 72)

for bucket in ["Common", "Library", "Rare", "Archive", "HOLD"]:
    count = sum(
        1 for r in rows
        if r["recommended"] == bucket
    )

    print(f"{bucket:<10} {count:>4}")

changes = sum(
    1 for r in rows
    if r["change"] == "YES"
)

near = sum(
    1 for r in rows
    if r["threshold_flag"]
)

print()
print(f"Proposed location changes: {changes}")
print(f"Near thresholds:          {near}")
print()
print(f"CSV: {OUTPUT}")
