"""For a JS-shell page: find the API it calls, and fetch it.

Same idea as marseille-agenda's discover.py, minus the agent -- scan the page's
same-origin JavaScript for API routes, then probe them and report what came back.
A 200 with a non-empty JSON list means no headless browser is needed.

    python3 apihunt.py URL ...
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
ROUTE_RE = re.compile(r"""["'`](/?(?:api|wp-json|graphql|rest|feed|data)/[^"'`\s<>]{1,160})["'`]""")
# The API is often on another host entirely (api.<venue>.com), reached by an absolute URL -- a
# path-only regex misses it. Skip the framework/spec hosts every bundle mentions.
ABS_RE = re.compile(r"""["'`](https?://[a-zA-Z0-9.-]+\.[a-z]{2,}(?:/[^"'`\s<>]{0,120})?)["'`]""")
NOISE = re.compile(r"w3\.org|nextjs\.org|reactjs\.org|fb\.me|schema\.org|googleapis|gstatic|"
                   r"jquery|cloudflare|sentry|googletagmanager|facebook\.net|doubleclick", re.I)
CHUNK_RE = re.compile(r"""["']\./([\w-]+\.js)["']""")
EVENTISH = re.compile(r"even|akce|program|predstaveni|představení|koncert|calend|show|session", re.I)


async def js_sources(client: httpx.AsyncClient, url: str) -> tuple[str, list[tuple[str, str]]]:
    """(origin, [(name, code)]) for the page's inline + same-origin scripts, one chunk level deep."""
    r = await client.get(url)
    soup = BeautifulSoup(r.text, "lxml")
    o = urlparse(str(r.url))
    origin = f"{o.scheme}://{o.netloc}"

    out = [("inline", " ".join(s.string or "" for s in soup.find_all("script") if not s.get("src")))]
    srcs = [urljoin(str(r.url), s["src"]) for s in soup.find_all("script", src=True)]
    srcs += [urljoin(str(r.url), l["href"]) for l in soup.find_all("link", rel="modulepreload", href=True)]
    srcs = list(dict.fromkeys(s for s in srcs if s.startswith(origin)))[:20]

    sem = asyncio.Semaphore(8)

    async def get(u: str) -> tuple[str, str]:
        async with sem:
            try:
                return u, (await client.get(u)).text
            except httpx.HTTPError:
                return u, ""

    fetched = [e for e in await asyncio.gather(*(get(s) for s in srcs)) if e[1]]
    out += fetched
    # one level of lazily-imported chunks: the agenda component is usually not in the entry bundle
    chunks = {urljoin(src, c) for src, code in fetched for c in CHUNK_RE.findall(code)}
    chunks -= {s for s, _ in fetched}
    out += [e for e in await asyncio.gather(*(get(c) for c in list(chunks)[:60])) if e[1]]
    return origin, out


async def hunt(client: httpx.AsyncClient, url: str) -> str:
    try:
        origin, sources = await js_sources(client, url)
    except httpx.HTTPError as exc:
        return f"\n### {url}\n  fetch failed: {type(exc).__name__}: {exc}"

    routes = {m.group(1) for _, code in sources for m in ROUTE_RE.finditer(code)}
    routes = {"/" + r.lstrip("/") for r in routes if "${" not in r and "{" not in r}
    absolute = {m.group(1) for _, code in sources for m in ABS_RE.finditer(code)}
    absolute = {u for u in absolute if not NOISE.search(u) and not u.rstrip("/").endswith(origin.split("//")[-1])}
    # An api.* / *.api host is worth probing even when the path looks unremarkable.
    absolute = {u for u in absolute if EVENTISH.search(u) or re.search(r"\bapi\b", u)}

    cands = sorted([r for r in routes if EVENTISH.search(r)]) or sorted(routes)
    cands = [origin + p for p in cands] + sorted(absolute)

    lines = [f"\n### {url}",
             f"  {len(sources)} js sources, {len(routes)} paths, {len(absolute)} external, {len(cands)} candidates"]
    if not cands:
        lines.append("  no API in the JS -- server-rendered after all, or a real browser job")
        return "\n".join(lines)

    sem = asyncio.Semaphore(4)

    async def probe(u: str) -> str:
        async with sem:
            try:
                r = await client.get(u, headers={"Accept": "application/json"}, timeout=25)
            except httpx.HTTPError as exc:
                return f"  {u[:70]:70} ERROR {type(exc).__name__}"
        out = f"  {u[:70]:70} {r.status_code} {len(r.content):>7}b"
        if r.status_code < 400:
            try:
                j = json.loads(r.text)
            except json.JSONDecodeError:
                return out + " (not JSON)"
            if isinstance(j, list):
                keys = sorted(j[0])[:8] if j and isinstance(j[0], dict) else []
                out += f"  -> list[{len(j)}] keys={keys}"
            elif isinstance(j, dict):
                sizes = {k: len(v) for k, v in j.items() if isinstance(v, list)}
                out += f"  -> keys={list(j)[:8]} lists={sizes}"
        return out

    lines += await asyncio.gather(*(probe(u) for u in cands[:15]))
    return "\n".join(lines)


async def main() -> None:
    urls = sys.argv[1:]
    if not urls:
        raise SystemExit(__doc__)
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.5"},
        follow_redirects=True, timeout=30,
    ) as client:
        for out in await asyncio.gather(*(hunt(client, u) for u in urls)):
            print(out)


if __name__ == "__main__":
    asyncio.run(main())
