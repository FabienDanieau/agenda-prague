"""Locate venues for the map view, cached in venues.json.

Order matters -- each step is more accurate than the next:

1. MANUAL          coordinates read off OSM by hand for venues nothing else resolves
2. Noc vedcu       its place pages publish exact lat/lon, so no geocoding at all
3. address verbatim  some venue strings ARE a full address ("Ječná 545/19, 120 00 Praha 2")
4. Nominatim       name + ", Praha" as a last resort

Nominatim is free and needs no key, but it is rate-limited to 1 request/second and wants a
real User-Agent. Everything is cached, misses included, so this only runs for new venues.

    python3 geocode.py          # locate whatever events.json mentions and is missing
    python3 geocode.py --retry  # also retry the ones that previously failed
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
EVENTS = HERE / "events.json"
VENUES = HERE / "venues.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "agenda-prague/0.1 (https://github.com/FabienDanieau/agenda-prague)"
NOCVEDY = "https://www.nocvedy.cz"
NV_TOPICS = (20, 21, 22, 23)
NV_PRAGUE = {"prague", "praha"}

# Aggregator bylines, not places -- geocoding them would drop a pin on nothing.
NOT_A_PLACE = {"expats.cz", "Praha.eu", "Noc vědců", "Mountains on Stage", "Praha 2", "?"}

# "Ječná 545/19, 120 00 Praha 2" -- a street number and a postal code mean it is already an
# address, so it must be queried verbatim instead of having ", Praha" appended to it.
LOOKS_LIKE_ADDRESS = re.compile(r"\d+/\d+|\b\d{3} ?\d{2}\b")
PLACE_JSON = re.compile(r'"title":"([^"]+)","detailUrl":"([^"]+)","lat":([\d.]+),"lon":([\d.]+)')
# Drupal geofield on a dvojka event page, inside an escaped Leaflet config
GEOFIELD = re.compile(r'"lat":([\d.]+),"lon":([\d.]+)')

MANUAL = {
    "007 Strahov": (50.0766, 14.3899),
    "Eternia Smíchov": (50.0703, 14.4088),
    "Kasárna Karlín": (50.0929, 14.4508),
    "Cross Club": (50.1035, 14.4470),
    "Rock Café": (50.0824, 14.4176),
    "Lucerna Music Bar": (50.0817, 14.4249),
    "Palác Akropolis": (50.0876, 14.4497),
    "Institut français": (50.0846, 14.4260),
    "Prague Accueil": (50.0846, 14.4260),
    "Meet Factory": (50.0606, 14.4033),
    "O2 arena": (50.1039, 14.4930),
    "Prague Congress Centre": (50.0621, 14.4283),
    "Roxy": (50.0897, 14.4243),
    "Futurum": (50.0687, 14.4046),
    "Chapeau Rouge": (50.0872, 14.4207),
    "DOX": (50.1010, 14.4453),
    # Verified against Nominatim's display_name, not typed from memory. The last two are halls
    # inside Výstaviště Praha, so the grounds are an honest pin for them.
    "Bazilika sv. Petra a Pavla na Vyšehradě": (50.06442, 14.41788),
    "Národní kulturní památka Vyšehrad": (50.06421, 14.41945),
    "Havlíčkovy sady Náměstí Míru": (50.06892, 14.44762),
    "Nová Spirála": (50.10837, 14.42992),
    "Křižíkův Pavilon B": (50.10914, 14.42642),
}

PRAGUE = (50.0755, 14.4378)


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def in_prague(lat: float, lon: float) -> bool:
    """A hit far outside Prague is some other town with the same name, not this venue."""
    return abs(lat - PRAGUE[0]) < 0.3 and abs(lon - PRAGUE[1]) < 0.4


def dvojka_places(client: httpx.Client, events: list[dict]) -> dict[str, tuple[float, float]]:
    """{venue: (lat, lon)} from dvojka.praha2.cz event pages -- exact, not geocoded.

    Its venue strings are display labels ("Bazilika sv. Ludmily", "Úřad MČ Praha 2") that
    Nominatim's free-text index simply does not carry, so every query shape misses. The
    detail page meanwhile embeds a Drupal geofield with the real point and street address,
    which is the thing worth reading.
    """
    first: dict[str, str] = {}
    for e in events:
        if e.get("source") == "dvojka" and e.get("url"):
            first.setdefault(e["venue"], e["url"])

    out: dict[str, tuple[float, float]] = {}
    for venue, url in first.items():
        try:
            html = client.get(url).text
        except httpx.HTTPError:
            continue
        # the Leaflet config opens with a "lat":0,"lon":0 default, so take the first
        # match that is actually a Prague point rather than the first match
        for m in GEOFIELD.finditer(html):
            lat, lon = float(m.group(1)), float(m.group(2))
            if in_prague(lat, lon):
                out[venue] = (round(lat, 5), round(lon, 5))
                break
    print(f"  dvojka: {len(out)}/{len(first)} venues with a published geofield")
    return out


def nocvedy_places(client: httpx.Client) -> dict[str, tuple[float, float]]:
    """{place title: (lat, lon)} straight from Noc vedcu -- exact, not geocoded.

    Its place pages embed the coordinates in a JSON blob, which beats guessing "Faculty of
    Nuclear Sciences and Physical Engineering" from the name.
    """
    headers = {"Accept": "application/json", "Referer": f"{NOCVEDY}/en/schedule",
               "Accept-Language": "en"}
    try:
        client.get(f"{NOCVEDY}/en/schedule")  # the API is cookie-gated
        urls: dict[str, str] = {}
        for topic in NV_TOPICS:
            r = client.get(f"{NOCVEDY}/api/events", params={"topics": topic}, headers=headers)
            r.raise_for_status()
            for it in r.json().get("data", {}).get("events", []):
                # Only Prague places: "Faculty of Information Technology" is a CTU building
                # here and a BUT building in Brno, and taking whichever came first pinned the
                # Prague events to Brno -- where the in_prague check then dropped them.
                if (it.get("cityName") or "").strip().lower() not in NV_PRAGUE:
                    continue
                if it.get("placeTitle") and it.get("placeUrl"):
                    urls.setdefault(it["placeTitle"], it["placeUrl"])
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  ! nocvedy places unavailable: {type(exc).__name__}")
        return {}

    out: dict[str, tuple[float, float]] = {}
    for title, path in urls.items():
        try:
            html = client.get(f"{NOCVEDY}{path}").text.replace("&quot;", '"')
        except httpx.HTTPError:
            continue
        points = [(t, float(la), float(lo)) for t, _, la, lo in PLACE_JSON.findall(html)]
        # prefer the entry whose title matches, but a place page is about one place, so any
        # Prague point on it beats falling through to a geocoder guess
        exact = [(la, lo) for t, la, lo in points if t == title and in_prague(la, lo)]
        any_hit = [(la, lo) for _, la, lo in points if in_prague(la, lo)]
        if chosen := (exact or any_hit):
            out[title] = (round(chosen[0][0], 5), round(chosen[0][1], 5))
    print(f"  nocvedy: {len(out)}/{len(urls)} places with published coordinates")
    return out


def relates_to(name: str, display_name: str) -> bool:
    """Does the hit actually name this venue, or did Nominatim latch onto a stray number?

    "Fuchs 2 Praha" returns a house number at Za Zelenou liškou with no "Fuchs" in it, and
    "Klub Varšava" returns a bookshop. Both sit inside Prague, so the bounds check passes them
    and the map gets a confidently wrong pin. Require a real word of the venue to come back.
    """
    words = {w for w in re.split(r"[^\w]+", name.lower()) if len(w) >= 4}
    if not words:  # nothing distinctive to check against; take the hit
        return True
    got = display_name.lower()
    return any(w in got for w in words)


def nominatim(client: httpx.Client, name: str) -> tuple[float, float] | None:
    # an address stands on its own; a bare name needs the city appended
    queries = [name] if LOOKS_LIKE_ADDRESS.search(name) else [f"{name}, Praha, Česko",
                                                              f"{name}, Prague"]
    # Venue strings carry trailing noise -- "Ječná 545/19, 120 00 Praha 2-Nové Město, Česko"
    # and "Albertov, Nové Město, Praha-Praha 2, Česko" both miss verbatim but hit on their
    # first chunk alone.
    head = name.split(",")[0].strip()
    if head and head != name:
        queries.append(f"{head}, Praha")

    for query in queries:
        try:
            r = client.get(NOMINATIM, params={"q": query, "format": "json", "limit": 1,
                                              "countrycodes": "cz"},
                           headers={"User-Agent": UA})
            r.raise_for_status()
            hits = r.json()
        except (httpx.HTTPError, ValueError):
            hits = []
        time.sleep(1.1)  # Nominatim's published limit is 1 req/s; stay under it
        if not hits:
            continue
        lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
        if in_prague(lat, lon) and relates_to(name, hits[0].get("display_name", "")):
            return round(lat, 5), round(lon, 5)
    return None


def main() -> None:
    retry = "--retry" in sys.argv
    data = load(EVENTS, {})
    if not data:
        raise SystemExit("no events.json -- run harvest.py first")

    cache: dict[str, list[float] | None] = load(VENUES, {})
    wanted = sorted({e["venue"] for e in data["events"]} - NOT_A_PLACE)

    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": UA}) as client:
        known = {k: tuple(v) for k, v in MANUAL.items()}
        missing = [v for v in wanted if v not in known
                   and (v not in cache or cache[v] is None)]
        if missing:
            # sources that publish their own coordinates first -- exact beats geocoded, and
            # these are the venues Nominatim's index does not carry at all
            known |= nocvedy_places(client)
            known |= dvojka_places(client, data["events"])

        todo = [v for v in wanted
                if v not in known and (v not in cache or (retry and cache[v] is None))]
        print(f"{len(wanted)} venues, {len(known)} already located, {len(todo)} to geocode")

        for i, name in enumerate(todo, 1):
            found = nominatim(client, name)
            cache[name] = list(found) if found else None
            print(f"  {i:3}/{len(todo)}  {'ok ' if found else '-- '} {name[:52]}")

    cache |= {k: list(v) for k, v in known.items()}
    VENUES.write_text(json.dumps(dict(sorted(cache.items())), ensure_ascii=False, indent=1),
                      encoding="utf-8")

    # harvest.py reads venues.json to fill events.json's `places`, but it has already run by
    # the time we get here. Patch the result instead of making CI harvest a second time.
    data["places"] = {k: v for k, v in cache.items() if v}
    EVENTS.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    located = sum(1 for v in cache.values() if v)
    placed = sum(1 for e in data["events"] if data["places"].get(e["venue"]))
    print(f"\n{located}/{len(cache)} venues located -> {VENUES.name}")
    print(f"{placed}/{len(data['events'])} events on the map -> {EVENTS.name}")
    for name, pos in sorted(cache.items()):
        if not pos:
            print(f"  still unlocated: {name}")


if __name__ == "__main__":
    main()
