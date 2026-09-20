"""Harvest Prague events into events.json.

    praguerocks    Firestore REST, open read      9 rock/club venues
    Eternia        plain JSON API                 1 venue, 4 rooms
    Cross Club     HTML day-run listing           own page beats the aggregator's copy
    ifp.cz         HTML listing, self-labelled    Institut francais
    pragueaccueil  HTML listing (SPIP), labelled  French-speaking cultural visits
    Noc vedcu      cookie-gated JSON API          one nationwide night, Prague only
    o2arena        HTML listing                   concerts + sport
    praguecc       HTML accordion                 Prague Congress Centre
    DOX            HTML listing                   contemporary art
    praha.eu       HTML city calendar
    expats.cz      prose roundup (heuristic)      weekly "what to do this weekend"
    mountainsonstage  Wix text scan               multi-country tour, Prague only

Sources that label their own category are read, not guessed; the rest fall back to keywords.

    python3 harvest.py [--posters] [--social] && python3 -m http.server 8000
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from PIL import Image

import dates

OUT = Path(__file__).parent / "events.json"

FIRESTORE = "https://firestore.googleapis.com/v1/projects/praguerocks-a1134/databases/(default)/documents"
ETERNIA = "https://api.eterniasmichov.com/events"
IFP = "https://www.ifp.cz/fr/evenements/"
CROSSCLUB = "https://www.crossclub.cz/cs/program/?secured=1"
ACCUEIL = [
    "https://www.pragueaccueil.com/Visites-culturelles",
    "https://www.pragueaccueil.com/Conferences-Culturelles",
    "https://www.pragueaccueil.com/Visites-et-expositions",
]
# pragueaccueil.com answers 403 to a non-browser UA. Every other source is fine without this.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36")

MUSIC = re.compile(r"musi|concert|koncert|party|\bdj\b|rock|jazz|metal|punk|techno|klub|hudb", re.I)
CULTURE = re.compile(r"cultur|expo|vystav|theat|divad|danse|cirque|cinem|kino|film|litter|"
                     r"conferen|prednas|visite|debat|\bart\b|lecture|atelier|workshop", re.I)
SCIENCE = re.compile(r"scien|vedec|\bveda\b|technic|technolog|research|vyzkum|medic|astronom|"
                     r"biolog|chemi|fyzik|physic|matemat", re.I)
SPORT = re.compile(r"\bhc \b|hockey|hokej|\bsparta\b|davis cup|tennis|tenis|basket|volejbal|"
                   r"football|fotbal|\bmatch\b|zapas|playoff|championship|champions|gladiator|"
                   r"\bfmx\b|\bufc\b|\bmma\b|olympi|maraton|marathon", re.I)


def categorize(raw: str | None, default: str) -> str:
    """Coarse bucket from a source's own label; `default` when it says nothing useful."""
    t = dates.slug(raw)
    if not t:
        return default
    # science first: "Technical Science" also matches techno, and a lecture is not automatically
    # culture. Sport next, so "HC Sparta" is not read as a concert at a concert arena.
    if SCIENCE.search(t):
        return "science"
    if SPORT.search(t):
        return "sport"
    if MUSIC.search(t):
        return "music"
    if CULTURE.search(t):
        return "culture"
    return default


def clip(s: str | None, n: int = 240) -> str | None:
    """Description trimmed to a card-sized blurb; None when there is nothing to say."""
    s = (s or "").strip()
    return (s[:n] + "…") if len(s) > n else (s or None)


def uid(venue: str, title: str, day: str) -> str:
    return hashlib.sha1(f"{venue}|{title}|{day}".encode()).hexdigest()[:12]


# The same venue is spelled differently by different sources; without this the dedup key
# treats them as two places and the event shows twice. Keyed on dates.slug().
VENUE_ALIAS = {
    "meetfactory": "Meet Factory",
    "futurum music bar": "Futurum",
    "palac akropolis": "Palác Akropolis",
    "o2 universum": "O2 universum",
    "klub 007 strahov": "007 Strahov",
    # Subzero is a room inside Eternia Smíchov, which the venue's own feed reports as a room
    "subzero": "Eternia Smíchov",
    "sasazu": "SaSaZu",
}


def canonical_venue(name: str) -> str:
    return VENUE_ALIAS.get(dates.slug(name), name)


def event(title, day, venue, *, category, end=None, room=None, url=None, info=None,
          tag=None, start_time=None, image=None, source="") -> dict:
    # a run that began before today has its start clamped to today, which can leave an end
    # on or before it -- that is a single-day event now, not a range
    if end and end <= day:
        end = None
    venue = canonical_venue(venue)
    return {
        "uid": uid(venue, title, day.isoformat()), "title": title,
        "date": day.isoformat(), "end": end.isoformat() if end else None,
        "time": start_time.strftime("%H:%M") if start_time else None,
        "venue": venue, "room": room, "url": url, "info": info, "image": image,
        "category": category, "tag": tag, "source": source,
    }


def img_of(node, sel: str, base: str) -> str | None:
    """Absolute URL of a card's own picture, lazy-loading and srcset attributes included."""
    el = node.select_one(sel)
    if not el:
        return None
    src = el.get("src") or el.get("data-src") or el.get("data-original") or ""
    if not src:
        # "url-360w, url-640w, ..." -- take the first candidate
        sset = el.get("data-srcset") or el.get("srcset") or ""
        src = sset.split(",")[0].strip().split(" ")[0] if sset else ""
    if not src or src.startswith("data:") or "svg+xml" in src:
        return None
    return urljoin(base, src)


BG_URL = re.compile(r"url\(\s*['\"]?(.*?)['\"]?\s*\)")


def bg_image_of(node, sel: str, base: str) -> str | None:
    """Picture set as a CSS background on a style attribute rather than an <img>."""
    el = node.select_one(sel)
    m = BG_URL.search(el.get("style", "")) if el else None
    return urljoin(base, m.group(1)) if m else None


def drop_shared_images(events: list[dict]) -> None:
    """A picture used by more than half of a source's events is a logo, not an event visual.

    Marseille's rule, and it matters: a wrong thumbnail is worse than none.
    """
    per_source: dict[str, list[dict]] = {}
    for e in events:
        per_source.setdefault(e["source"], []).append(e)
    for group in per_source.values():
        counts = Counter(e["image"] for e in group if e.get("image"))
        shared = {u for u, n in counts.items() if n > len(group) / 2}
        for e in group:
            if e.get("image") in shared:
                e["image"] = None


def soup_of(client: httpx.Client, url: str, *, browser_ua: bool = False) -> BeautifulSoup:
    headers = {"Accept-Language": "fr-FR,fr;q=0.9,cs;q=0.8"}
    if browser_ua:
        headers["User-Agent"] = BROWSER_UA
    r = client.get(url, headers=headers)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")


def text_of(node, sel: str) -> str:
    el = node.select_one(sel)
    return el.get_text(" ", strip=True) if el else ""


# --------------------------------------------------------------------------- praguerocks


def firestore_all(client: httpx.Client, collection: str) -> list[dict]:
    out, token = [], None
    for _ in range(20):
        params = {"pageSize": 300} | ({"pageToken": token} if token else {})
        r = client.get(f"{FIRESTORE}/{collection}", params=params)
        r.raise_for_status()
        body = r.json()
        for doc in body.get("documents", []):
            out.append({k: next(iter(v.values())) for k, v in doc.get("fields", {}).items()})
        token = body.get("nextPageToken")
        if not token:
            break
    return out


def from_praguerocks(client: httpx.Client, today: date) -> list[dict]:
    places = {p["slug"]: p for p in firestore_all(client, "places") if p.get("slug")}
    out = []
    for e in firestore_all(client, "events"):
        raw = str(e.get("date") or "")
        if len(raw) != 8 or not raw.isdigit():
            continue
        try:
            day = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        except ValueError:
            continue
        title = (e.get("title") or "").strip()
        if not (title and dates.plausible(day, today)):
            continue
        place = places.get(e.get("place_slug", ""), {})
        # `link` is absolute for some venues and a path for others -- and that path belongs to
        # the VENUE's site (places[].url), not to praguerocks. Joining it to praguerocks.cz
        # produced 404s for Roxy and MeetFactory.
        link = e.get("link") or ""
        url = urljoin(place.get("url") or "", link) if link else None
        out.append(event(
            title, day, place.get("name") or e.get("place_slug") or "?",
            category="music", url=url or None, source="praguerocks"))
    return out


# --------------------------------------------------------------------------- eternia


def from_eternia(client: httpx.Client, today: date) -> list[dict]:
    r = client.get(ETERNIA, headers={"Accept": "application/json"})
    r.raise_for_status()
    out = []
    for e in r.json():
        if not e.get("zobrazeno"):
            continue
        title = (e.get("nazev") or "").strip()
        if not title:
            continue
        try:
            p = dates.parse(e.get("datum") or "", today)
        except dates.DateError:
            continue
        if not dates.plausible(p.start, today):
            continue
        out.append(event(
            title, p.start, "Eternia Smíchov", category="music", end=p.end,
            room=e.get("misto") or None, url=e.get("link") or None,
            info=(e.get("info") or "").strip() or None, source="eternia"))
    return out


# --------------------------------------------------------------------------- ifp.cz


def from_ifp(client: httpx.Client, today: date) -> list[dict]:
    soup = soup_of(client, IFP)
    out = []
    for item in soup.select(".event-item"):
        link = item.select_one("h3 a")
        title = link.get_text(" ", strip=True) if link else ""
        raw = text_of(item, ".date-loc")
        if not (title and raw):
            continue
        try:
            p = dates.parse(raw, today)
        except dates.DateError:
            continue
        # an exhibition that started in the past but runs past today still counts
        if not dates.plausible(p.end or p.start, today):
            continue
        tag = text_of(item, ".event-sticker") or None
        out.append(event(
            title, max(p.start, today), "Institut français",
            category=categorize(tag, "culture"), end=p.end, tag=tag,
            url=link.get("href") if link else None,
            start_time=p.start_time, image=img_of(item, ".image img", IFP), source="ifp"))
    return out


# --------------------------------------------------------------------------- pragueaccueil


def from_pragueaccueil(client: httpx.Client, today: date) -> list[dict]:
    out = []
    for page in ACCUEIL:
        slug = page.rsplit("/", 1)[-1]
        tag = slug.replace("-", " ")
        try:
            soup = soup_of(client, page, browser_ua=True)
        except httpx.HTTPError as exc:
            print(f"  ! pragueaccueil {slug}: {type(exc).__name__}")
            continue
        # Past events are a separate list (.item.evenement, all flagged evenement_passe);
        # the upcoming ones are li.evenements_avenir. No per-event permalink, so link the page.
        for item in soup.select("li.evenements_avenir"):
            title = text_of(item, "h3")
            raw = text_of(item, ".programmation .date") or text_of(item, ".date")
            if not (title and raw):
                continue
            try:
                p = dates.parse(raw, today)
            except dates.DateError:
                continue
            if not dates.plausible(p.start, today):
                continue
            info = text_of(item, ".texte")
            out.append(event(
                title, p.start, "Prague Accueil", category=categorize(tag, "culture"),
                end=p.end, tag=tag, start_time=p.start_time,
                info=clip(info),
                url=page, image=img_of(item, ".logo_evenement_avenir img", page),
                source="pragueaccueil"))
    return out


# --------------------------------------------------------------------------- cross club


def from_crossclub(client: httpx.Client, today: date) -> list[dict]:
    """Day-run layout: a .predel date header, then the .article blocks belonging to that day."""
    soup = soup_of(client, CROSSCLUB)
    out, day, end = [], None, None
    for node in soup.select(".predel, .article"):
        classes = node.get("class") or []
        if "predel" in classes:
            try:
                p = dates.parse(node.get_text(" ", strip=True), today)
                day, end = p.start, p.end
            except dates.DateError:
                day = None
            continue
        if not day or not dates.plausible(day, today):
            continue
        link = node.select_one("h2 a")
        title = link.get_text(" ", strip=True) if link else ""
        if not title:
            continue
        # "Koncert / Party - Hlavní stage / Kavárna:" -> kind before the dash, room after
        raw = text_of(node, ".category").rstrip(":")
        kind, _, room = raw.partition(" - ")
        info = text_of(node, ".cast") or text_of(node, ".text")
        out.append(event(
            title, day, "Cross Club", category=categorize(kind, "music"), end=end,
            room=room.strip() or None, tag=kind.strip() or None,
            url=link.get("href") if link else None,
            info=clip(info),
            image=img_of(node, ".photo153 img", CROSSCLUB),
            source="crossclub"))
    return out


# --------------------------------------------------------------------------- sasazu


SASAZU = "https://www.sasazu-club.com/kalendar"
# The visible rows show only a day number and weekday ("24 THU") under a month heading, but
# the Next.js payload carries whole ISO dates. Read that instead of reassembling the columns.
SASAZU_ENTRY = re.compile(
    r'"id\\?":\\?"([0-9a-f-]{36})\\?","?\\?"?date\\?":\\?"(\d{4}-\d{2}-\d{2})\\?",\\?"?title\\?":\\?"((?:[^"\\]|\\.)*?)\\?"')


def from_sasazu(client: httpx.Client, today: date) -> list[dict]:
    r = client.get(SASAZU, headers={"User-Agent": BROWSER_UA})
    r.raise_for_status()
    out, seen = [], set()
    for uuid, iso, raw_title in SASAZU_ENTRY.findall(r.text):
        if uuid in seen:
            continue
        seen.add(uuid)
        try:
            day = date.fromisoformat(iso)
            title = json.loads(f'"{raw_title}"').strip()
        except ValueError:
            continue
        if not title or not dates.plausible(day, today):
            continue
        out.append(event(
            title, day, "SaSaZu", category="music",
            url=f"https://www.sasazu-club.com/akce/{uuid}", source="sasazu"))
    return out


# --------------------------------------------------------------------------- rfp concerts


RFP = "https://rfpconcerts.cz/en/concerts/"


def from_rfpconcerts(client: httpx.Client, today: date) -> list[dict]:
    """A promoter, not a venue: each card names the venue and city, and the tour leaves Prague
    (2 of 66 cards are Brno and Hradec Králové), so the city has to be filtered."""
    soup = soup_of(client, RFP)
    out = []
    for card in soup.select(".card--lineup"):
        title = text_of(card, ".card__header h3")
        rows = [d.get_text(" ", strip=True) for d in card.select(".card__footer div")]
        if not (title and len(rows) >= 2):
            continue
        where = rows[-1]
        if not PRAGUE.search(where):
            continue
        try:
            p = dates.parse(rows[0], today)
        except dates.DateError:
            continue
        if not dates.plausible(p.start, today):
            continue
        # "Café V lese, Praha" -> the venue is everything before the city
        venue = where.rsplit(",", 1)[0].strip() or "Praha"
        out.append(event(
            title, p.start, venue, category="music", start_time=p.start_time,
            tag="RFP", info=clip(text_of(card, ".card__header p")),
            url=card.get("href") or RFP,
            image=img_of(card, ".card__img img", RFP), source="rfpconcerts"))
    return out


# --------------------------------------------------------------------------- facebook events


# Groups that organise events but keep no website. Facebook blocks anonymous clients, so this
# goes through Apify -- and its events tab is structured, so no model has to read a flyer.
FB_PAGES = [("somelikeitczech", "Some Like It Czech")]


def from_facebook(client: httpx.Client, today: date) -> list[dict]:
    import social

    out = []
    for page, organiser in FB_PAGES:
        for it in social.facebook_events(page):
            if it.get("isCanceled"):
                continue
            start = social.local_start(it.get("utcStartDate") or "")
            title = (it.get("name") or "").strip()
            if not (start and title) or not dates.plausible(start.date(), today):
                continue
            loc = it.get("location") or {}
            out.append(event(
                title, start.date(), loc.get("name") or organiser, category="music",
                start_time=start.time() if start.time() != time(0, 0) else None,
                tag=organiser, info=clip(it.get("description")),
                url=it.get("url"), image=it.get("imageUrl") or None,
                source="facebook"))
    return out


# --------------------------------------------------------------------------- praha 2 (dvojka)


DVOJKA = "https://dvojka.praha2.cz/events"


def from_dvojka(client: httpx.Client, today: date) -> list[dict]:
    """Prague 2 district calendar (Drupal). Each teaser carries smartdate <time> attributes,
    its own category and the actual place, so nothing has to be guessed."""
    soup = soup_of(client, DVOJKA)
    out = []
    for card in soup.select(".node-teaser-akce"):
        title = text_of(card, ".teaser-title")
        stamps = [t.get("datetime") for t in card.select("time[datetime]") if t.get("datetime")]
        if not (title and stamps):
            continue
        try:
            start = datetime.fromisoformat(stamps[0])
            finish = datetime.fromisoformat(stamps[-1])
        except ValueError:
            continue
        if not dates.plausible(finish.date(), today):
            continue
        kind = text_of(card, ".teaser-kategorie")
        # T00:00 is the all-day convention in machine dates, not a midnight start
        begins = start.time() if start.time() != time(0, 0) else None
        out.append(event(
            title, max(start.date(), today), text_of(card, ".misto-akce") or "Praha 2",
            category=categorize(kind or title, "culture"),
            end=finish.date() if finish.date() != start.date() else None,
            tag=kind or None, start_time=begins,
            info=clip(text_of(card, ".teaser-price")),
            url=urljoin(DVOJKA, card.get("href") or ""),
            image=bg_image_of(card, ".teaser-img", DVOJKA), source="dvojka"))
    return out


# --------------------------------------------------------------------------- kasarna karlin


KASARNA = "https://www.kasarnakarlin.cz/en/program"


def from_kasarnakarlin(client: httpx.Client, today: date) -> list[dict]:
    """Drupal tiles carrying an ISO datetime attribute.

    Read the attribute, never the visible text: the English page prints "9. 18. 2026", which
    is month-first and would parse as day 9 of month 18 under the Czech D. M. rule.
    Every time is 12:00:00Z, a placeholder, so no start time is emitted.
    """
    soup = soup_of(client, KASARNA)
    out = []
    for tile in soup.select(".node--type--event"):
        title = text_of(tile, ".event-title")
        stamp = tile.select_one(".event-date time[datetime]")
        if not (title and stamp):
            continue
        # the venue posts closure days as tiles too ("CLOSED" / "ZAVŘENO")
        if dates.slug(title) in {"closed", "zavreno"}:
            continue
        try:
            day = date.fromisoformat((stamp.get("datetime") or "")[:10])
        except ValueError:
            continue
        if not dates.plausible(day, today):
            continue
        link = tile.select_one("a.tile-inside[href]")
        out.append(event(
            title, day, "Kasárna Karlín", category=categorize(title, "culture"),
            url=urljoin(KASARNA, link["href"]) if link else KASARNA,
            image=img_of(tile, "img", KASARNA), source="kasarnakarlin"))
    return out


# --------------------------------------------------------------------------- rock cafe


ROCKCAFE = "https://rockcafe.cz/program/"


def from_rockcafe(client: httpx.Client, today: date) -> list[dict]:
    """The venue's own listing. praguerocks lists Rock Cafe too, but without images or type."""
    soup = soup_of(client, ROCKCAFE)
    out = []
    for card in soup.select("a.list-item"):
        title = text_of(card, "h2")
        raw = text_of(card, ".date")
        if not (title and raw):
            continue
        try:
            p = dates.parse(raw, today)
        except dates.DateError:
            continue
        if not dates.plausible(p.end or p.start, today):
            continue
        kind = text_of(card, ".type")
        out.append(event(
            title, max(p.start, today), "Rock Café",
            category=categorize(kind or title, "music"), end=p.end, tag=kind or None,
            start_time=p.start_time, url=urljoin(ROCKCAFE, card.get("href") or ""),
            image=img_of(card, "img", ROCKCAFE), source="rockcafe"))
    return out


# --------------------------------------------------------------------------- expats.cz weekend picks


EXPATS = "https://www.expats.cz"
EXPATS_TAG = f"{EXPATS}/czech-news/tag/weekend-events"
# ".../best-events-for-september-18-20" -- the slug carries the weekend the article covers
EXPATS_SLUG = re.compile(r"best-events-for-([a-z]+)-(\d{1,2})-(\d{1,2})/?$", re.I)
EN_WD = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


def latest_expats_article(client: httpx.Client) -> str | None:
    soup = soup_of(client, EXPATS_TAG, browser_ua=True)
    for a in soup.select('a[href*="what-to-do-this-weekend"]'):
        if EXPATS_SLUG.search(a.get("href") or ""):
            return urljoin(EXPATS, a["href"])
    return None


def from_expats(client: httpx.Client, today: date) -> list[dict]:
    """Weekly "what to do this weekend" roundup: prose, not a listing.

    ponytail: heuristic. The weekend window comes from the URL slug, an explicit date in the
    prose wins when there is one, and a bare weekday ("gather ... on Saturday") is mapped into
    that window. This is the one source here whose shape really wants an LLM extractor --
    marseille routes exactly this case (prose, no repeated cards) to its generator agent.
    """
    url = latest_expats_article(client)
    if not url:
        print("  ! expats: no weekend article found on the tag page")
        return []

    m = EXPATS_SLUG.search(url)
    month = dates.EN_MONTHS.get((m.group(1) or "").lower())
    if not month:
        return []
    start = dates.infer_year(month, int(m.group(2)), today - timedelta(days=7))
    end = start + timedelta(days=max(0, int(m.group(3)) - int(m.group(2))))

    soup = soup_of(client, url, browser_ua=True)
    out: list[dict] = []
    title, prose = None, []

    def flush() -> None:
        if not title:
            return
        text = " ".join(prose)
        day, until = start, None
        try:
            p = dates.parse(text, today)
            # only trust a parsed date near this weekend; the prose is full of other numbers
            if start - timedelta(days=7) <= p.start <= start + timedelta(days=180):
                day, until = p.start, p.end
        except dates.DateError:
            if (wd := EN_WD.search(text)):
                idx = dates.WEEKDAY_INDEX[wd.group(1).lower()]
                for i in range((end - start).days + 1):
                    if (start + timedelta(days=i)).weekday() == idx:
                        day = start + timedelta(days=i)
                        break
        if not dates.plausible(until or day, today):
            return
        # No venue: "at the St. Wenceslas Crown" is not a place, and matching a venue name
        # anywhere in the prose also miscategorised a yoga class as music because the
        # paragraph happened to mention Jazz Dock. Category comes from the title alone.
        out.append(event(
            title, max(day, today), "expats.cz",
            category=categorize(title, "culture"), end=until,
            tag="Weekend pick", info=clip(text),
            url=url, source="expats"))

    for widget in soup.select(".widget"):
        classes = widget.get("class") or []
        if "headinglevel2" in classes:
            head = widget.select_one("h3")
            if not head:
                continue
            # section labels ("Best eats") are italicised; event names are not
            if head.find("em"):
                flush()
                title, prose = None, []
                continue
            flush()
            title, prose = head.get_text(" ", strip=True), []
        elif "text" in classes and title:
            prose.append(widget.get_text(" ", strip=True))
    flush()
    return out


# --------------------------------------------------------------------------- praha.eu / DOX


PRAHA_EU = "https://praha.eu/kalendar-akci"
DOX = "https://www.dox.cz/en/whats-on"


def from_praha_eu(client: httpx.Client, today: date) -> list[dict]:
    """City calendar. Each card carries its start and end as two .box-card-date__num blocks."""
    soup = soup_of(client, PRAHA_EU)
    out = []
    for card in soup.select(".box-card"):
        title = text_of(card, ".box-card__title")
        nums = [n.get_text(" ", strip=True) for n in card.select(".box-card-date__num")]
        if not (title and nums):
            continue
        try:
            p = dates.parse(" ".join(dict.fromkeys(nums)), today)
        except dates.DateError:
            continue
        if not dates.plausible(p.end or p.start, today):
            continue
        link = card.select_one("a[href]")
        perex = text_of(card, ".box-card__perex") or text_of(card, ".box-card__text")
        out.append(event(
            title, max(p.start, today), "Praha.eu",
            category=categorize(f"{title} {perex}", "culture"), end=p.end,
            start_time=p.start_time,
            info=clip(perex),
            url=urljoin(PRAHA_EU, link["href"]) if link else PRAHA_EU,
            image=img_of(card, ".box-card-thumb img, .box-card-thumb source", PRAHA_EU),
            source="praha.eu"))
    return out


def from_dox(client: httpx.Client, today: date) -> list[dict]:
    """DOX centre for contemporary art: .entry-events cards, English date ranges."""
    soup = soup_of(client, DOX)
    out = []
    for card in soup.select(".entry-events"):
        title = text_of(card, ".entry-title")
        if not title:
            continue
        parsed = None
        for value in card.select(".entry-meta-value"):
            try:
                parsed = dates.parse(value.get_text(" ", strip=True), today)
                break
            except dates.DateError:
                continue
        if not parsed or not dates.plausible(parsed.end or parsed.start, today):
            continue
        link = card.select_one(".entry-media a[href], .entry-title a[href], a[href]")
        kind = text_of(card, ".entry-meta-category")
        out.append(event(
            title, max(parsed.start, today), "DOX",
            category=categorize(kind or title, "culture"), end=parsed.end,
            tag=kind or None, start_time=parsed.start_time,
            url=urljoin(DOX, link["href"]) if link else DOX,
            image=img_of(card, ".entry-media img", DOX),
            source="dox"))
    return out


# --------------------------------------------------------------------------- o2 arena


O2ARENA = "https://www.o2arena.cz/en/events/"


def from_o2arena(client: httpx.Client, today: date) -> list[dict]:
    """Concerts and sport in one listing; .time can hold one date per day of a multi-day event."""
    soup = soup_of(client, O2ARENA)
    out = []
    for item in soup.select(".event_preview"):
        link = item.select_one("h3 a")
        title = link.get_text(" ", strip=True) if link else ""
        raw = text_of(item, ".time")
        if not (title and raw):
            continue
        try:
            p = dates.parse(raw, today)
        except dates.DateError:
            continue
        if not dates.plausible(p.end or p.start, today):
            continue
        perex = text_of(item, ".perex")
        # each line carries its own start time, so the second one is a start, not an end

        out.append(event(
            title, max(p.start, today), "O2 arena",
            category=categorize(f"{title} {perex}", "music"), end=p.end,
            start_time=p.start_time, info=clip(perex),
            url=link.get("href") if link else O2ARENA,
            image=bg_image_of(item, ".eye_catcher", O2ARENA),
            source="o2arena"))
    return out


# --------------------------------------------------------------------------- prague congress centre


PRAGUECC = "https://www.praguecc.cz/en/prehled-akci/culture"


def from_praguecc(client: httpx.Client, today: date) -> list[dict]:
    """Bootstrap accordion cards; dates are English with a year, so dates.py needs nothing new."""
    soup = soup_of(client, PRAGUECC)
    out = []
    for item in soup.select(".blog-item"):
        link = item.select_one(".h4 a, h4 a")
        title = link.get_text(" ", strip=True) if link else ""
        raw = text_of(item, ".d-flex small")
        if not (title and raw):
            continue
        try:
            p = dates.parse(raw, today)
        except dates.DateError:
            continue
        # a run that started earlier but is still on counts as upcoming
        if not dates.plausible(p.end or p.start, today):
            continue
        info = text_of(item, ".collapse p")
        out.append(event(
            title, max(p.start, today), "Prague Congress Centre", category="culture",
            end=p.end, tag="Culture", start_time=p.start_time,
            info=clip(info),
            url=PRAGUECC, image=img_of(item, ".blog-img img", PRAGUECC),
            source="praguecc"))
    return out


# --------------------------------------------------------------------------- noc vedcu


NOCVEDY = "https://www.nocvedy.cz"
# The API refuses a bare /api/events (403) -- it wants at least one filter, and these four
# topic ids ARE the whole taxonomy, so fetching them covers everything exactly once.
NV_TOPICS = (20, 21, 22, 23)
# Both nationwide sources (Noc vedcu, Mountains on Stage) need the same city filter --
# marseille calls this Venue.location_filter.
PRAGUE = re.compile(r"\b(praha|prague)\b", re.I)


def from_nocvedy(client: httpx.Client, today: date) -> list[dict]:
    """Noc vedcu / Researchers' Night: one nationwide night, filtered to Prague."""
    # the API is cookie-gated; loading the page once sets what it wants
    client.get(f"{NOCVEDY}/en/schedule", headers={"User-Agent": BROWSER_UA})
    headers = {"Accept": "application/json", "Accept-Language": "en",
               "Referer": f"{NOCVEDY}/en/schedule", "User-Agent": BROWSER_UA}

    seen: set[str] = set()
    out = []
    for topic in NV_TOPICS:
        r = client.get(f"{NOCVEDY}/api/events", params={"topics": topic}, headers=headers)
        r.raise_for_status()
        for it in r.json().get("data", {}).get("events", []):
            if not PRAGUE.search(it.get("cityName") or ""):
                continue
            detail = it.get("detailUrl") or ""
            if detail in seen:
                continue
            seen.add(detail)
            try:
                p = dates.parse(it.get("time") or "", today)
            except dates.DateError:
                continue
            if not dates.plausible(p.start, today):
                continue
            topics = [t.get("title") for t in it.get("topics") or [] if t.get("title")]
            chips = [c.get("title") for c in it.get("chips") or [] if c.get("title")]
            out.append(event(
                it.get("title") or "", p.start,
                it.get("placeTitle") or it.get("organizationName") or "Noc vědců",
                category="science", end=p.end, start_time=p.start_time,
                tag=(topics or chips or [None])[0],
                info=" · ".join(filter(None, [it.get("organizationName"), ", ".join(chips)])) or None,
                url=(NOCVEDY + detail) if detail.startswith("/") else (detail or None),
                image=it.get("imageUrl") if it.get("hasEventImage") else None,
                source="nocvedy"))
    return out


# --------------------------------------------------------------------------- mountains on stage


MOS = "https://www.mountainsonstage.com/czech-slovenia-poland-dates"
# "Tuesday, December 8 / Praha - Kino 35 / <address> / From 7pm to 10:15pm"
MOS_BLOCK = re.compile(
    r"^((?:mon|tues|wednes|thurs|fri|satur|sun)day,\s*[a-z]+ \d{1,2})\s*\n"
    r"([^\n]+?)\s*-\s*([^\n]+)\n"
    r"([^\n]*)\n"
    r"(from[^\n]*)?", re.I | re.M)


def from_mountainsonstage(client: httpx.Client, today: date) -> list[dict]:
    """A Wix page: no usable markup, but the rendered text is a rigid repeating block.

    ponytail: text-level scan rather than selectors -- Wix div soup carries no stable classes.
    Breaks if they restyle the list; the regex anchors on the weekday line so it fails loudly.
    """
    soup = soup_of(client, MOS)
    for tag in soup.select("script, style"):
        tag.decompose()
    text = re.sub(r"\n{2,}", "\n", soup.get_text("\n", strip=True))

    out = []
    for m in MOS_BLOCK.finditer(text):
        when, city, place, address, hours = (g or "" for g in m.groups())
        if not PRAGUE.search(city) and not PRAGUE.search(address):
            continue
        try:
            p = dates.parse(f"{when} {hours}", today)
        except dates.DateError:
            continue
        if not dates.plausible(p.start, today):
            continue
        out.append(event(
            "Mountains on Stage", p.start, place.strip() or "Mountains on Stage",
            category="culture", tag="Film", start_time=p.start_time,
            info=address.strip() or None, url=MOS, source="mountainsonstage"))
    return out


# --------------------------------------------------------------------------- posters (opt-in)

CACHE = Path(__file__).parent / ".image_cache.json"


def add_posters(events: list[dict], workers: int = 8) -> None:
    """Fill `image` from each event's own page via og:image. Opt-in: one fetch per event.

    Results are cached on disk by URL (including the misses, as null), so a second run costs
    nothing and a venue that has no poster is not re-fetched every day.
    """
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}

    todo = [e for e in events if not e.get("image") and e.get("url")
            and e["url"] not in cache and "facebook.com" not in e["url"]]
    print(f"  posters: {len(todo)} to fetch, {len(cache)} cached")

    def fetch(url: str) -> tuple[str, str | None]:
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as c:
                r = c.get(url, headers={"User-Agent": BROWSER_UA})
                r.raise_for_status()
                tag = BeautifulSoup(r.text, "lxml").find("meta", property="og:image")
                return url, (urljoin(url, tag["content"]) if tag and tag.get("content") else None)
        except (httpx.HTTPError, ValueError, KeyError):
            return url, None

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            cache |= dict(pool.map(fetch, [e["url"] for e in todo]))
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    for e in events:
        if not e.get("image") and e.get("url"):
            e["image"] = cache.get(e["url"])


# --------------------------------------------------------------------------- local image cache

IMG_DIR = Path(__file__).parent / "img"
THUMB_PX = 320


def localize_images(events: list[dict], workers: int = 10) -> None:
    """Download every remote image and serve it from img/.

    Hotlinking is not viable: klub007strahov.cz inspects the Referer and swaps the poster for a
    "this image was hotlinked" placeholder -- with a 200, so the page's onerror never fires and
    the broken image just shows. Fetching server-side returns the real file. This also survives
    CDN URLs that expire, and keeps the page from leaking the reader's IP to every venue.
    """
    todo = [e for e in events if (e.get("image") or "").startswith("http")]
    if not todo:
        return
    IMG_DIR.mkdir(exist_ok=True)

    def grab(e: dict) -> bool:
        dest = IMG_DIR / f"{e['uid']}.webp"
        if dest.exists() and dest.stat().st_size > 0:
            e["image"] = f"img/{dest.name}"
            return True
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as c:
                r = c.get(e["image"], headers={"User-Agent": BROWSER_UA,
                                               "Referer": e.get("url") or e["image"]})
                r.raise_for_status()
                if "image" not in r.headers.get("content-type", ""):
                    return False
                img = Image.open(io.BytesIO(r.content))
                img.thumbnail((THUMB_PX, THUMB_PX))
                img.convert("RGB").save(dest, "WEBP", quality=80)
        except (httpx.HTTPError, OSError, ValueError):
            return False  # keep the remote URL and let the page's placeholder handle it
        e["image"] = f"img/{dest.name}"
        return True

    with ThreadPoolExecutor(max_workers=workers) as pool:
        ok = sum(pool.map(grab, todo))
    print(f"  images: {ok}/{len(todo)} cached locally")

    # files of events that are gone (past, dropped source) have no reason to stay
    live = {f"{e['uid']}.webp" for e in events}
    for stale in IMG_DIR.glob("*.webp"):
        if stale.name not in live:
            stale.unlink(missing_ok=True)


# --------------------------------------------------------------------------- main


def load_places() -> dict[str, list[float] | None]:
    """Venue coordinates from geocode.py, for the map view. Empty until it has been run."""
    try:
        return json.loads((Path(__file__).parent / "venues.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest Prague events into events.json.")
    ap.add_argument("--posters", action="store_true",
                    help="fetch og:image for events whose source gives no picture (cached)")
    ap.add_argument("--social", action="store_true",
                    help="match Instagram flyers to events that still have no image (needs APIFY_API_KEY)")
    args = ap.parse_args()

    today = date.today()
    events: list[dict] = []
    sources = [("crossclub", from_crossclub), ("rockcafe", from_rockcafe),
               ("kasarnakarlin", from_kasarnakarlin), ("dvojka", from_dvojka),
               ("eternia", from_eternia), ("ifp", from_ifp),
               ("pragueaccueil", from_pragueaccueil),
               ("mountainsonstage", from_mountainsonstage),
               ("nocvedy", from_nocvedy), ("praguecc", from_praguecc),
               ("o2arena", from_o2arena), ("dox", from_dox),
               ("praha.eu", from_praha_eu), ("expats", from_expats),
               ("sasazu", from_sasazu), ("rfpconcerts", from_rfpconcerts), ("praguerocks", from_praguerocks)]
    # Facebook costs Apify credit per event, so it rides along with --social
    if args.social:
        sources.insert(0, ("facebook", from_facebook))

    with httpx.Client(timeout=40, follow_redirects=True) as client:
        # A venue's own page beats an aggregator's copy of it (room, price, line-up), and dedup
        # keeps whichever source is seen first -- so venue-native sources run before praguerocks.
        for name, fn in sources:
            try:
                got = fn(client, today)
            except (httpx.HTTPError, ValueError) as exc:
                print(f"  ! {name} failed: {type(exc).__name__}: {exc}")
                continue
            print(f"  {name:14} {len(got):4}")
            events += got

    seen: dict[tuple[str, str, str], dict] = {}
    for e in events:
        seen.setdefault((dates.slug(e["venue"]), e["date"], dates.slug(e["title"])), e)
    events = sorted(seen.values(), key=lambda e: (e["date"], e["venue"], e["title"]))

    drop_shared_images(events)
    if args.posters:
        add_posters(events)
    if args.social:
        import social
        # Venues that publish no image of their own, only flyers on Instagram.
        for handle, venue in (("eternia.smichov", "Eternia Smíchov"),):
            social.attach_images(events, handle, venue, today)
    localize_images(events)

    def tally(key: str) -> dict[str, int]:
        return dict(Counter(e[key] for e in events).most_common())

    OUT.write_text(json.dumps({
        "generated": today.isoformat(),
        "count": len(events),
        "by_source": tally("source"),
        "by_category": tally("category"),
        "venues": sorted({e["venue"] for e in events}),
        "categories": sorted({e["category"] for e in events}),
        # {venue: [lat, lng]} for the map view; written by geocode.py, absent until it runs
        "places": {k: v for k, v in load_places().items() if v},
        "events": events,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{len(events)} upcoming -> {OUT.name}")
    print(f"  categories: {tally('category')}")
    print(f"  venues:     {len(set(e['venue'] for e in events))}")
    print(f"  images:     {sum(1 for e in events if e.get('image'))}/{len(events)}"
          + ("" if args.posters else "   (--posters fetches the rest)"))
    if events:
        print(f"  range:      {events[0]['date']} .. {events[-1]['date']}")
    print("\n  python3 -m http.server 8000")


if __name__ == "__main__":
    main()
