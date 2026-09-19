"""How much machine-readable event data do Prague venues already publish?

Answers the only question that matters before building anything: which venues need
zero LLM (schema.org JSON-LD or machine date attributes) and which need real extraction.

    python3 probe.py            # probe the built-in venue list
    python3 probe.py URL ...    # probe specific URLs
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import date

import httpx
from bs4 import BeautifulSoup

VENUES = [
    ("MeetFactory", "https://www.meetfactory.cz/cs/program"),
    ("DOX", "https://www.dox.cz/program"),
    ("Jazz Dock", "https://www.jazzdock.cz/cs/program"),
    ("Kino Aero", "https://www.kinoaero.cz/program"),
    ("Palac Akropolis", "https://www.palacakropolis.cz/program"),
    ("Lucerna Music Bar", "https://musicbar.cz/program/"),
    ("Cross Club", "https://www.crossclub.cz/cs/program"),
    ("Forum Karlin", "https://www.forumkarlin.cz/program"),
    ("Narodni divadlo", "https://www.narodni-divadlo.cz/cs/program"),
    ("Ceska filharmonie", "https://www.ceskafilharmonie.cz/program/"),
    ("Divadlo Archa", "https://www.archatheatre.cz/cs/program"),
    ("GoOut Praha", "https://goout.net/cs/praha/akce/"),
]

# dox.cz serves 164 bytes to a Chrome UA and 141 kB to an honest one; musicbar.cz does the
# opposite. Default to httpx's own UA and only override per-venue when a site demands it.
UA = "python-httpx/0.28 (prague-agenda-probe)"
EVENT_TYPES = {"Event", "MusicEvent", "TheaterEvent", "ExhibitionEvent", "ScreeningEvent", "Festival"}

CZ_MONTHS = ("ledna|unora|února|brezna|března|dubna|kvetna|května|cervna|června|"
             "cervence|července|srpna|zari|září|rijna|října|listopadu|prosince")
EN_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
             "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
# Prague has French-speaking venues too (Institut francais, Prague Accueil). These parse with
# marseille's dates.py verbatim -- French is the language it was written for.
FR_MONTHS = ("janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre|"
             "janv|f[ée]vr|avr|juil|sept")
# "15. 9. 2026" / "15.9." -- the dominant Czech format, dots not slashes
CZ_NUMERIC = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.(\s?\d{4})?")
CZ_WORDY = re.compile(rf"\b\d{{1,2}}\.?\s+({CZ_MONTHS})\b", re.I)
# Plenty of Prague venues and aggregators publish in English ("16 September 2026").
# These parse with marseille's dates.py as-is -- its _MONTHS already holds English names.
EN_WORDY = re.compile(rf"\b(\d{{1,2}}\s+({EN_MONTHS})|({EN_MONTHS})\s+\d{{1,2}})\b", re.I)
FR_WORDY = re.compile(rf"\b\d{{1,2}}(?:er)?\s+({FR_MONTHS})\b", re.I)
ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})")


def walk(node):
    """Every dict nested anywhere in a parsed JSON-LD blob."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def jsonld_events(html: str) -> list[dict]:
    out = []
    for script in BeautifulSoup(html, "lxml").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in walk(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if EVENT_TYPES.intersection(t for t in types if isinstance(t, str)):
                out.append(node)
    return out


def machine_dates(soup: BeautifulSoup, today: date) -> int:
    """Upcoming dates sitting in datetime= / data-date= attributes: parseable with no language."""
    els = soup.find_all(attrs={"datetime": True}) + soup.find_all(attrs={"data-date": True})
    return sum(1 for el in els
               if (m := ISO_DATE.search(el.get("datetime") or el.get("data-date") or ""))
               and m.group(1) >= today.isoformat())


async def probe(client: httpx.AsyncClient, name: str, url: str, today: date) -> str:
    try:
        r = await client.get(url)
    except httpx.HTTPError as exc:
        return f"{name:22} {type(exc).__name__}: {str(exc)[:50]}"
    if r.status_code >= 400:
        return f"{name:22} HTTP {r.status_code}  (wrong URL? discovery needed)"

    html = r.text
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    events = jsonld_events(html)
    dated = [e for e in events if e.get("startDate")]
    attrs = machine_dates(soup, today)
    # Dates often live in attributes or inline JSON, not in visible text (musicbar.cz has
    # 0 in its text and ~200 in its markup). Only "absent from the raw HTML" means JS-rendered.
    in_text = len(CZ_NUMERIC.findall(text)) + len(CZ_WORDY.findall(text))
    in_html = len(CZ_NUMERIC.findall(html)) + len(CZ_WORDY.findall(html)) + len(ISO_DATE.findall(html))
    en = len(EN_WORDY.findall(text))
    fr = len(FR_WORDY.findall(text))

    sample = ""
    if dated:
        verdict = "JSON-LD -- no LLM needed"
        sample = f"{'':22}    e.g. {str(dated[0].get('startDate', ''))[:16]} {str(dated[0].get('name', ''))[:45]}"
    elif attrs >= 3:
        verdict = "machine dates -- induction should work"
    elif fr > in_text and fr > en:
        verdict = "French dates -- parses with dates.py UNCHANGED"
    elif en > in_text:
        verdict = "English dates -- parses with dates.py UNCHANGED"
    elif in_text:
        verdict = "Czech text dates -- needs the D. M. YYYY regex"
    elif in_html:
        verdict = "dates in markup only -- parse attrs/inline JSON, still no browser"
    else:
        verdict = "no date anywhere in the HTML -- only case that may need rendering"

    return (
        f"{name:22} {len(events):3} ld+json ev ({len(dated)} dated) | {attrs:3} attrs | "
        f"{in_text:4} cz {en:4} en {fr:4} fr {in_html:5} html | {len(html):7}c\n"
        f"{'':22} -> {verdict}" + (f"\n{sample}" if sample else "")
    )


async def main() -> None:
    today = date.today()
    venues = [(u, u) for u in sys.argv[1:]] or VENUES
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.5"},
        follow_redirects=True, timeout=30,
    ) as client:
        results = await asyncio.gather(*(probe(client, n, u, today) for n, u in venues))
    print(f"Prague venue probe -- {today}\n")
    print("\n".join(results))


if __name__ == "__main__":
    asyncio.run(main())
