# agenda_prague

A calendar of upcoming events in Prague, harvested from the venues and aggregators that
publish usable data, and served as a single static page.

Inspired by [tfrere/marseille-agenda](https://github.com/tfrere/marseille-agenda), but
deliberately not a port of it: that project uses an LLM agent to discover each venue's
agenda and write an extraction schema. Here every source was found by hand with `probe.py` /
`apihunt.py`, so the daily run costs nothing and calls no model.

    python3 harvest.py                  # events.json from every source
    python3 harvest.py --posters        # + fetch og:image where a source gives no picture
    python3 harvest.py --posters --social   # + match Instagram flyers (needs APIFY_API_KEY)
    python3 -m http.server 8000         # then open localhost:8000

`python3 dates.py` and `python3 smoke.py` run the self-checks.

## Sources

| Source | Shape | Note |
|---|---|---|
| praguerocks | Firestore REST, open read | 9 rock/club venues, no auth |
| Rock Café | HTML listing | own page: images + type labels |
| Kasárna Karlín | Drupal tiles | ISO `datetime` attribute, not the visible text |
| Cross Club | HTML day-run | `.predel` date header, then its `.article`s |
| Eternia Smíchov | plain JSON API | no images anywhere; flyers only on Instagram |
| Institut français | HTML listing | labels its own category |
| Prague Accueil | SPIP listing | needs a browser UA (403 otherwise) |
| Noc vědců | cookie-gated JSON API | one nationwide night, filtered to Prague |
| O2 arena | HTML listing | concerts + sport, CSS-background images |
| Prague Congress Centre | Bootstrap accordion | English dates |
| DOX | HTML listing | English date ranges, `data-srcset` images |
| praha.eu | HTML city calendar | start/end as two date blocks |
| expats.cz | weekly prose roundup | heuristic; the one source that wants an LLM |
| Mountains on Stage | Wix text scan | multi-country tour, filtered to Prague |

`sources.json` records what was actually verified per source, so findings are not
re-investigated.

## How it works

1. **`dates.py`** parses the formats these listings really use: Czech dotted (`6. 8. – 24. 9.`,
   year often omitted), French (`Vendredi 18 septembre de 10h00`), and English in both
   `December 8` and `8 December` orders. A missing year resolves to the next occurrence,
   constrained by a written weekday. `plausible()` rejects source typos — Eternia really does
   publish `10. 10. 0020`.
2. **`harvest.py`** runs one function per source, dedupes on (venue, date, title), and writes
   `events.json`. Venue-native sources run before aggregators so the richer copy wins.
3. **Categories** come from the source's own label where there is one (ifp, Cross Club,
   Noc vědců); otherwise from keywords. Science is tested before music, because "Technical
   Science" also matches `techno`.
4. **Images** are downloaded and resized locally rather than hotlinked —
   klub007strahov.cz inspects the `Referer` and serves a "this image was hotlinked"
   placeholder with HTTP 200, so `onerror` never fires. Local copies also survive expiring
   CDN URLs and keep readers' IPs off every venue's server.

## Deploy

GitHub Actions + Pages, daily at 06:17 Prague. `img/` and the caches are kept in the Actions
cache, not committed, so the repo stays small and a run re-downloads nothing. `--social` runs
on Mondays only, because Apify bills per result. The job refuses to deploy if the harvest
returns fewer than 100 events, which leaves the previous site live rather than publishing an
empty one.

Repo secret: `APIFY_API_KEY` (optional — without it everything except Instagram flyers works).

## Tools

- `probe.py URL…` — what a venue publishes: JSON-LD, machine dates, Czech/English/French text,
  or nothing (the only case that might need a browser).
- `apihunt.py URL…` — for a JS page, find the API its bundle calls. This is what turned up
  `api.eterniasmichov.com/events`.
- `inspect_sel.py` / `inspect_text.py` — look at a saved page's markup while writing a scraper.

## Known gaps

- **Rudolfinum** renders its programme client-side and its JS names no API — the one source
  that would genuinely need a headless browser.
- **Hells Bells** publishes only a poster image and "all concerts are on Facebook".
- **Eternia** images cap at ~4/40: it posts to Instagram about five times a month, so most of
  its calendar has never been posted about.
- praha.eu, praguecc and DOX are page 1 only; none are paginated yet.
