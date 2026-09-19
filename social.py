"""Instagram flyers via Apify, for venues that publish no images of their own.

No vision model needed: we already have the events structured (title + date) from the venue's
own API, so this only has to *match* a post to an event and borrow its picture.

Matching requires BOTH a date in the caption and a shared significant word with the title.
Date alone is not enough -- eternia.smichov posted an Alisøn Walks flyer for 26.09.2026 while
the API lists HEIDEN that night, so a date-only rule attaches the wrong poster. Marseille's
rule applies: missing is better than wrong.

The matched image is left as a remote URL; harvest.localize_images() downloads and resizes it
right after, which matters here because Instagram CDN URLs expire within days.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

import dates

HERE = Path(__file__).parent
CACHE = HERE / ".apify_cache.json"
ACTOR = "apify~instagram-scraper"

# Re-fetching costs Apify credits, so a cached pull is reused for this long.
CACHE_TTL = timedelta(hours=12)
# Words too common to prove a caption is about a given event.
STOP = {"praha", "prague", "smichov", "eternia", "subzero", "puda", "akce", "event", "vstup",
        "koncert", "concert", "party", "night", "live", "www", "https", "http", "com", "kdy",
        "cas", "zdarma", "free", "tickets", "vstupne", "info", "more", "bude", "budou"}


def words(s: str) -> set[str]:
    """Significant words: long enough and not boilerplate."""
    return {w for w in dates.slug(s).split() if len(w) >= 4 and w not in STOP}


def token() -> str | None:
    if tok := os.environ.get("APIFY_API_KEY"):
        return tok
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("APIFY_API_KEY="):
                return line.split("=", 1)[1].strip() or None
    return None


def fetch_posts(handle: str, limit: int = 60) -> list[dict]:
    """Recent posts for a handle, cached on disk so repeat runs cost nothing."""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}

    entry = cache.get(handle)
    if entry:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(entry["fetched"])
        if age < CACHE_TTL:
            print(f"  instagram/{handle}: {len(entry['posts'])} posts (cached)")
            return entry["posts"]

    tok = token()
    if not tok:
        print("  ! APIFY_API_KEY not set, skipping Instagram")
        return entry["posts"] if entry else []

    try:
        r = httpx.post(
            f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items",
            params={"token": tok},
            json={"directUrls": [f"https://www.instagram.com/{handle}/"],
                  "resultsType": "posts", "resultsLimit": limit, "addParentData": False},
            timeout=420,
        )
        r.raise_for_status()
        posts = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  ! apify {handle} failed: {type(exc).__name__}: {exc}")
        # a failed run must not wipe images we already matched
        return entry["posts"] if entry else []

    keep = [{k: p.get(k) for k in ("caption", "displayUrl", "url", "timestamp", "shortCode")}
            for p in posts if p.get("displayUrl")]
    cache[handle] = {"fetched": datetime.now(timezone.utc).isoformat(), "posts": keep}
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  instagram/{handle}: {len(keep)} posts (fetched)")
    return keep


def caption_dates(caption: str, today: date) -> set[str]:
    """Every date the caption mentions, as ISO strings."""
    found: set[str] = set()
    for line in (caption or "").splitlines():
        try:
            p = dates.parse(line, today)
        except dates.DateError:
            continue
        found.add(p.start.isoformat())
        if p.end:
            found.add(p.end.isoformat())
    return found


def attach_images(events: list[dict], handle: str, venue: str, today: date,
                  limit: int = 60) -> None:
    """Give `venue`'s image-less events the flyer from a matching Instagram post."""
    targets = [e for e in events if e["venue"] == venue and not e.get("image")]
    if not targets:
        return

    posts = fetch_posts(handle, limit)
    if not posts:
        return

    scored = [(p, caption_dates(p.get("caption") or "", today), words(p.get("caption") or ""))
              for p in posts]

    matched = 0
    for e in targets:
        best, best_score = None, 0
        for post, days, cap_words in scored:
            if e["date"] not in days:
                continue
            score = len(cap_words & words(e["title"]))
            if score > best_score:
                best, best_score = post, score
        # a shared date proves nothing on its own; require a word from the title too
        if not best:
            continue
        e["image"] = best["displayUrl"]
        e["image_source"] = best.get("url")
        matched += 1
    print(f"  {venue}: {matched}/{len(targets)} matched a flyer")
