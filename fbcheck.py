"""Does this Facebook page host upcoming events worth harvesting?"""
import json
import sys
from pathlib import Path

import httpx

ENV = Path(__file__).parent / ".env"
tok = next(l.split("=", 1)[1].strip() for l in ENV.read_text(encoding="utf-8").splitlines()
           if l.startswith("APIFY_API_KEY="))

page = sys.argv[1]
r = httpx.post("https://api.apify.com/v2/acts/apify~facebook-events-scraper/run-sync-get-dataset-items",
               params={"token": tok},
               json={"startUrls": [f"https://www.facebook.com/{page}/upcoming_hosted_events"],
                     "maxEvents": 20},
               timeout=420)
items = [it for it in r.json() if not it.get("error")]
errs = [it for it in r.json() if it.get("error")]
print(f"HTTP {r.status_code}  {len(items)} events, {len(errs)} error records")
for it in errs[:1]:
    print("  ", it.get("errorDescription"))
for it in items:
    loc = it.get("location") or {}
    print(f"  {it.get('utcStartDate','')[:16]}  {loc.get('name','?')[:24]}  {(it.get('name') or '')[:46]}")
