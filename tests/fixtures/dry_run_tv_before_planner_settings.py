#!/usr/bin/env python3

import csv
import json
import os
import random
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from runtime_config import get_config
RUNTIME = get_config()

SONARR_URL = RUNTIME.urls["sonarr"]
JELLYFIN_URL = RUNTIME.urls["jellyfin"]

SAMPLE_SIZE = 999999
RANDOM_SEED = 420
CACHE_DAYS = 7

CACHE_DIR = (RUNTIME.base_path / "cache")
OUTPUT = (RUNTIME.base_path / "tv_dry_run.csv")

SUBSCRIBED = {
    "Hulu",
    "Peacock",
}

USER_FREE_ACCESS = set()


def docker_output(container, command):
    return subprocess.check_output(
        ["docker", "exec", container, "sh", "-c", command],
        text=True
    ).strip()


SONARR_KEY = docker_output(
    RUNTIME.containers["sonarr"],
    r"""sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' /config/config.xml"""
)

JELLYFIN_KEY = docker_output(
    RUNTIME.containers["homepage"],
    "cat /run/secrets/jellyfin_api_key"
)

TMDB_TOKEN = os.environ["TMDB_TOKEN"]


def request_json(url, headers=None, timeout=120, retries=4):
    last_error = None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers=headers or {}
            )

            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)

        except Exception as e:
            last_error = e

            if attempt < retries - 1:
                delay = 2 ** attempt
                print(
                    f"Request failed: {type(e).__name__}: {e} "
                    f"- retrying in {delay}s"
                )
                time.sleep(delay)

    raise last_error


def sonarr(path, timeout=45):
    return request_json(
        SONARR_URL + path,
        headers={"X-Api-Key": SONARR_KEY},
        timeout=timeout,
        retries=1
    )


def jellyfin(path, timeout=60):
    return request_json(
        JELLYFIN_URL + path,
        headers={"X-Emby-Token": JELLYFIN_KEY},
        timeout=timeout
    )


def cached(namespace, key, loader):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key))
    path = CACHE_DIR / f"{namespace}_{safe}.json"

    if path.exists():
        age = time.time() - path.stat().st_mtime

        if age < CACHE_DAYS * 86400:
            with path.open() as f:
                return json.load(f), True

    data = loader()

    with path.open("w") as f:
        json.dump(data, f)

    return data, False


# ------------------------------------------------------------
# REPLACEMENT SCARCITY
# ------------------------------------------------------------

SOFT_PATTERNS = [
    re.compile(r"^Existing file meets cutoff:"),
    re.compile(r"^Quality for release in queue already meets cutoff:"),
    re.compile(
        r"^Existing file and the Quality profile does not allow upgrades$"
    ),
    re.compile(
        r"^Episode file on disk contains more episodes than this release contains$"
    ),
    re.compile(
        r"^Importing after download will exceed available disk space$"
    ),
    re.compile(
        r"^Existing file on disk is of equal or higher preference:"
    ),
    re.compile(
        r"^Has same release name as a grabbed and imported release$"
    ),
]


def rejection_texts(release):
    values = release.get("rejections") or []
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
    reasons = rejection_texts(release)

    if not reasons:
        return True

    return all(
        any(p.search(reason) for p in SOFT_PATTERNS)
        for reason in reasons
    )


def normalize_release(title):
    value = title.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def scarcity(count):
    if count >= 20:
        return 0
    if count >= 10:
        return 15
    if count >= 6:
        return 30
    if count >= 4:
        return 45
    if count == 3:
        return 60
    if count == 2:
        return 75
    if count == 1:
        return 90

    return 100


def season_replacement(series_id, season_number, owned_episode_numbers):
    key = f"{series_id}_s{season_number}"

    def loader():
        return sonarr(
            f"/api/v3/release?"
            f"seriesId={series_id}&seasonNumber={season_number}"
        )

    try:
        releases, from_cache = cached(
            "sonarr_season",
            key,
            loader
        )
    except Exception as e:
        return None, "low", False, f"{type(e).__name__}: {e}"

    # An empty interactive search does NOT prove there are
    # zero recovery paths. Sonarr/indexers frequently timeout or
    # return empty results transiently.
    if not releases:
        return (
            None,
            "low",
            from_cache,
            "Empty Sonarr season search; replacement unknown"
        )

    viable = [
        r for r in releases
        if r.get("title") and soft_only(r)
    ]

    packs = {
        normalize_release(r["title"])
        for r in viable
        if r.get("fullSeason") is True
    }

    episode_scores = []

    for epnum in owned_episode_numbers:
        paths = set(packs)

        for r in viable:
            numbers = r.get("episodeNumbers") or []

            if epnum in numbers:
                paths.add(normalize_release(r["title"]))

        paths.discard("")

        episode_scores.append(
            scarcity(min(len(paths), 20))
        )

    if not episode_scores:
        return None, "low", from_cache, "No owned episodes"

    hardest = max(episode_scores)
    average = mean(episode_scores)

    score = 0.70 * hardest + 0.30 * average

    return round(score, 1), "high", from_cache, (
        f"hardest={hardest:.1f}; avg={average:.1f}; packs={len(packs)}"
    )


def series_replacement(series):
    episodes = series_episodes(series["id"])

    owned = {}

    for ep in episodes:
        if not ep.get("hasFile"):
            continue

        season = ep.get("seasonNumber")

        if season is None or season == 0:
            continue

        owned.setdefault(season, []).append(
            ep["episodeNumber"]
        )

    if not owned:
        return None, "low", "No owned regular episodes", []

    season_scores = []
    details = []
    uncached = False

    for season, eps in sorted(owned.items()):
        score, confidence, was_cached, detail = season_replacement(
            series["id"],
            season,
            eps
        )

        details.append(
            f"S{season}={score if score is not None else 'UNKNOWN'}"
        )

        if not was_cached:
            uncached = True

        if score is not None:
            season_scores.append(score)

    if not season_scores:
        return None, "low", "; ".join(details), []

    worst = max(season_scores)
    average = mean(season_scores)

    result = 0.70 * worst + 0.30 * average

    return (
        round(result, 1),
        "high" if len(season_scores) == len(owned) else "medium",
        "; ".join(details),
        season_scores
    )


# ------------------------------------------------------------
# STREAMING
# ------------------------------------------------------------

def family(name):
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


def tmdb_season_providers(tmdb_id, season):
    key = f"{tmdb_id}_s{season}"

    def loader():
        return request_json(
            f"https://api.themoviedb.org/3/tv/"
            f"{tmdb_id}/season/{season}/watch/providers",
            headers={
                "Authorization": f"Bearer {TMDB_TOKEN}",
                "accept": "application/json"
            },
            timeout=30
        )

    return cached("tmdb_tv_provider", key, loader)


def tmdb_series_providers(tmdb_id):
    def loader():
        return request_json(
            f"https://api.themoviedb.org/3/tv/"
            f"{tmdb_id}/watch/providers",
            headers={
                "Authorization": f"Bearer {TMDB_TOKEN}",
                "accept": "application/json"
            },
            timeout=30
        )

    return cached(
        "tmdb_tv_series_provider",
        tmdb_id,
        loader
    )


def provider_score(data):
    us = data.get("results", {}).get("US", {})

    flat = {
        family(x["provider_name"])
        for x in us.get("flatrate", [])
    }

    free = {
        family(x["provider_name"])
        for x in us.get("free", [])
    }

    ads = {
        family(x["provider_name"])
        for x in us.get("ads", [])
    }

    rent = {
        family(x["provider_name"])
        for x in us.get("rent", [])
    }

    buy = {
        family(x["provider_name"])
        for x in us.get("buy", [])
    }

    subscribed = flat & SUBSCRIBED

    if subscribed:
        return 0, "Subscribed: " + ", ".join(sorted(subscribed))

    accessible_free = free & USER_FREE_ACCESS

    if accessible_free:
        return 0, "Free access: " + ", ".join(sorted(accessible_free))

    if ads:
        return 25, "Free with ads: " + ", ".join(sorted(ads))

    unsub = flat - SUBSCRIBED

    if len(unsub) >= 3:
        return 35, f"{len(unsub)} subscription families"
    if len(unsub) == 2:
        return 40, "2 subscription families"
    if len(unsub) == 1:
        return 50, "1 subscription family: " + next(iter(unsub))

    if rent:
        return 60, "Rental available"

    if buy:
        return 80, "Purchase only"

    return 100, "No US availability found"


def series_streaming(tmdb_id, owned_seasons):
    scores = []
    details = []

    # Load series-level availability once as fallback.
    try:
        series_data, _ = tmdb_series_providers(tmdb_id)
    except Exception:
        series_data = None

    for season in sorted(owned_seasons):
        data = None
        source = "season"

        try:
            season_data, _ = tmdb_season_providers(
                tmdb_id,
                season
            )

            # A successful response can still contain no US availability.
            if season_data.get("results", {}).get("US"):
                data = season_data

        except Exception:
            data = None

        # Fall back to show-level availability.
        if data is None and series_data is not None:
            if series_data.get("results", {}).get("US"):
                data = series_data
                source = "series-fallback"

        if data is None:
            details.append(
                f"S{season}=UNKNOWN (no provider data)"
            )
            continue

        score, reason = provider_score(data)

        scores.append(score)
        details.append(
            f"S{season}={score} [{source}] ({reason})"
        )

    if not scores:
        return None, "; ".join(details) or "No streaming data"

    worst = max(scores)
    average = mean(scores)

    result = 0.70 * worst + 0.30 * average

    return round(result, 1), "; ".join(details)


# ------------------------------------------------------------
# JELLYFIN USAGE
# ------------------------------------------------------------

def parse_dt(value):
    if not value:
        return None

    try:
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except Exception:
        return None


jf_users = jellyfin("/Users")

jf_usage = {}
jf_created = {}

for user in jf_users:
    uid = user["Id"]

    data = jellyfin(
        f"/Users/{uid}/Items?"
        f"Recursive=true&IncludeItemTypes=Series&"
        f"Fields=ProviderIds,DateCreated&EnableUserData=true"
    )

    for item in data.get("Items", []):
        tmdb = item.get("ProviderIds", {}).get("Tmdb")

        if not tmdb:
            continue

        tmdb = str(tmdb)
        ud = item.get("UserData") or {}

        plays = int(ud.get("PlayCount") or 0)
        last = parse_dt(ud.get("LastPlayedDate"))

        jf_created.setdefault(
            tmdb,
            parse_dt(item.get("DateCreated"))
        )

        row = jf_usage.setdefault(
            tmdb,
            {"plays": 0, "users": 0, "last": None}
        )

        row["plays"] += plays

        if plays:
            row["users"] += 1

        if last and (
            row["last"] is None
            or last > row["last"]
        ):
            row["last"] = last


def recency_points(last):
    if not last:
        return 0

    days = (
        datetime.now(timezone.utc) - last
    ).total_seconds() / 86400

    if days <= 7:
        return 100
    if days <= 30:
        return 90
    if days <= 90:
        return 75
    if days <= 180:
        return 55
    if days <= 365:
        return 35
    if days <= 730:
        return 20

    return 10


def repeat_points(plays):
    if plays >= 10:
        return 100
    if plays >= 6:
        return 80
    if plays >= 3:
        return 60
    if plays == 2:
        return 40
    if plays == 1:
        return 20

    return 0


def user_points(users):
    if users >= 4:
        return 100
    if users == 3:
        return 80
    if users == 2:
        return 60
    if users == 1:
        return 35

    return 0


def usage_score(tmdb_id):
    key = str(tmdb_id)

    usage = jf_usage.get(
        key,
        {"plays": 0, "users": 0, "last": None}
    )

    created = jf_created.get(key)

    if usage["plays"] == 0 and created:
        age = (
            datetime.now(timezone.utc) - created
        ).total_seconds() / 86400

        if age <= 90:
            return 50, "New unwatched grace"

        if age <= 180:
            value = 50 * (180 - age) / 90
            return round(value, 1), "Decaying new-item grace"

        return 0, "Never played"

    result = (
        0.60 * recency_points(usage["last"])
        + 0.25 * repeat_points(usage["plays"])
        + 0.15 * user_points(usage["users"])
    )

    return round(result, 1), (
        f'{usage["plays"]} plays / {usage["users"]} users'
    )


# ------------------------------------------------------------
# LIFECYCLE
# ------------------------------------------------------------

def current_bucket(path):
    value = (path or "").lower()

    if "/rare/" in value:
        return "Rare"
    if "/library/" in value:
        return "Library"
    if "/archive/" in value:
        return "Archive"
    if "/current/" in value:
        return "Current"

    return "Unknown"


def series_episodes(series_id):
    def loader():
        return sonarr(
            f"/api/v3/episode?seriesId={series_id}"
        )

    data, _ = cached(
        "sonarr_episodes",
        series_id,
        loader
    )

    return data


def owned_seasons(series_id):
    eps = series_episodes(series_id)

    return sorted({
        e["seasonNumber"]
        for e in eps
        if e.get("hasFile")
        and e.get("seasonNumber", 0) > 0
    })


def series_status(series):
    status = (series.get("status") or "").lower()

    if status in {
        "continuing",
        "upcoming"
    }:
        return "active"

    if status in {
        "ended",
        "cancelled",
        "canceled"
    }:
        return "ended"

    return "unknown"


def latest_air_date(series_id):
    episodes = series_episodes(series_id)

    dates = []

    for ep in episodes:
        if not ep.get("hasFile"):
            continue

        dt = parse_dt(ep.get("airDateUtc"))

        if dt:
            dates.append(dt)

    return max(dates) if dates else None


def days_since_added(tmdb_id):
    created = jf_created.get(str(tmdb_id))

    if not created:
        return None

    return (
        datetime.now(timezone.utc) - created
    ).total_seconds() / 86400


def days_since_played(tmdb_id):
    row = jf_usage.get(str(tmdb_id))

    if not row or not row["last"]:
        return None

    return (
        datetime.now(timezone.utc) - row["last"]
    ).total_seconds() / 86400


def archive_eligible(series, replacement, tmdb_id):
    if replacement is None or replacement >= 35:
        return False

    if series_status(series) != "ended":
        return False

    finale = latest_air_date(series["id"])

    if not finale:
        return False

    finale_age = (
        datetime.now(timezone.utc) - finale
    ).total_seconds() / 86400

    if finale_age < 365:
        return False

    added = days_since_added(tmdb_id)

    if added is None or added < 180:
        return False

    played = days_since_played(tmdb_id)

    if played is not None and played < 180:
        return False

    return True


# ------------------------------------------------------------
# SCORE SERIES
# ------------------------------------------------------------

series_list = [
    s for s in sonarr("/api/v3/series")
    if s.get("statistics", {}).get("episodeFileCount", 0) > 0
]

random.seed(RANDOM_SEED)

if len(series_list) > SAMPLE_SIZE:
    selected = random.sample(series_list, SAMPLE_SIZE)
else:
    selected = series_list

print(f"TV series with files: {len(series_list)}")
print(f"Scoring sample: {len(selected)}")
print("NO FILES WILL BE MOVED.")
print()

rows = []

for i, series in enumerate(selected, 1):
    title = series.get("title", "Unknown")
    tmdb_id = series.get("tmdbId")

    print(f"[{i:03}/{len(selected):03}] {title}")

    current = current_bucket(series.get("path"))
    seasons = owned_seasons(series["id"])

    replacement, confidence, repl_detail, _ = (
        series_replacement(series)
    )

    if tmdb_id:
        streaming, stream_detail = series_streaming(
            tmdb_id,
            seasons
        )
        usage, usage_detail = usage_score(tmdb_id)
    else:
        streaming = None
        stream_detail = "No TMDB ID"
        usage = 0
        usage_detail = "No TMDB ID"

    lifecycle = series_status(series)

    if streaming is None:
        final = None

        # Missing TMDB provider data means UNKNOWN, not unavailable.
        # Unknown streaming must never cause a demotion, but known
        # replacement scarcity may justify rescuing media from Archive.

        if current == "Rare":
            recommendation = "Rare"
            decision = (
                "Streaming unknown; preserve existing Rare placement"
            )

        elif (
            current == "Archive"
            and replacement is not None
            and replacement >= 35
        ):
            recommendation = "Library"
            decision = (
                "Streaming unknown; high replacement scarcity "
                "promotes Archive to Library"
            )

        elif current == "Archive":
            recommendation = "Archive"
            decision = (
                "Streaming unknown; preserve Archive until data improves"
            )

        elif lifecycle == "active":
            recommendation = "Current"
            decision = (
                "Provisional Current; streaming data unknown"
            )

        elif lifecycle == "ended":
            recommendation = "Library"
            decision = (
                "Provisional Library; streaming data unknown"
            )

        else:
            recommendation = "HOLD"
            decision = (
                "Streaming unknown and lifecycle insufficient"
            )

    elif replacement is None:
        final = None

        # Unknown replacement data must never cause a risky move.
        # Preserve already-protected placements until enrichment succeeds.
        if current in {"Rare", "Archive"}:
            recommendation = current
            decision = (
                "Replacement unknown; preserve existing protected placement"
            )

        elif lifecycle == "active":
            recommendation = "Current"
            decision = (
                "Provisional Current; replacement data unknown"
            )

        elif lifecycle == "ended":
            recommendation = "Library"
            decision = (
                "Provisional Library; replacement data unknown"
            )

        else:
            recommendation = "HOLD"
            decision = (
                "Lifecycle and replacement data insufficient"
            )

    else:
        final = round(
            0.45 * replacement
            + 0.30 * streaming
            + 0.25 * usage,
            1
        )

        # Same blackout preservation rule as movies.
        if streaming == 100 and replacement >= 15:
            recommendation = "Rare"
            decision = "Streaming blackout + limited replacement paths"

        elif final >= 70:
            recommendation = "Rare"
            decision = "Placement score >=70"

        elif lifecycle == "active":
            recommendation = "Current"
            decision = "Active/continuing series"

        elif archive_eligible(
            series,
            replacement,
            tmdb_id
        ):
            recommendation = "Archive"
            decision = "Ended + old + cold + easy to replace"

        else:
            recommendation = "Library"
            decision = "Ended/protected series"

    rows.append({
        "title": title,
        "sonarr_id": series["id"],
        "tmdb_id": tmdb_id,
        "status": series.get("status"),
        "owned_seasons": ",".join(map(str, seasons)),
        "current": current,
        "recommended": recommendation,
        "change": (
            "YES"
            if recommendation not in {"HOLD", current}
            else ""
        ),
        "final_score": final,
        "replacement": replacement,
        "replacement_confidence": confidence,
        "replacement_detail": repl_detail,
        "streaming": streaming,
        "streaming_detail": stream_detail,
        "usage": usage,
        "usage_detail": usage_detail,
        "decision_reason": decision,
        "path": series.get("path"),
    })

    time.sleep(1)


with OUTPUT.open("w", newline="") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=list(rows[0].keys())
    )
    writer.writeheader()
    writer.writerows(rows)


print()
print("=" * 72)
print("TV DRY RUN COMPLETE")
print("=" * 72)

from collections import Counter

print("Recommended:")
print(Counter(r["recommended"] for r in rows))

print()
print("Current -> Recommended:")

moves = Counter(
    (r["current"], r["recommended"])
    for r in rows
    if r["change"] == "YES"
)

for (src, dst), count in sorted(moves.items()):
    print(f"{src:10} -> {dst:10} {count}")

print()
print("CSV:", OUTPUT)
