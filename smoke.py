"""Import every module and assert the refactor kept behaviour. python3 smoke.py"""
from datetime import date

import dates
import harvest
import social

assert dates.slug("Křižovatka – Párty!") == "krizovatka party", dates.slug("Křižovatka – Párty!")
assert dates.slug("") == ""
assert harvest.clip(None) is None
assert harvest.clip("  ") is None
assert harvest.clip("short") == "short"
assert harvest.clip("x" * 300).endswith("…") and len(harvest.clip("x" * 300)) == 241
assert harvest.categorize("Technical Science", "music") == "science"
assert harvest.categorize("HC Sparta Praha", "music") == "sport"
assert harvest.categorize("Koncert", "culture") == "music"
assert harvest.categorize(None, "culture") == "culture"
assert harvest.PRAGUE.search("Prague") and harvest.PRAGUE.search("Praha")
assert not harvest.PRAGUE.search("Brno")

# drop_shared_images: a logo on >half a source's events goes, a genuine visual stays
evs = [{"source": "s", "image": "logo.png"} for _ in range(3)] + [{"source": "s", "image": "real.png"}]
harvest.drop_shared_images(evs)
assert [e["image"] for e in evs] == [None, None, None, "real.png"], evs

# social no longer downloads; it leaves a remote URL for localize_images
assert not hasattr(social, "download"), "social.download should be gone"
assert social.words("Tattooed Hearts Benefit v Praze") == {"tattooed", "hearts", "benefit", "praze"}

ev = harvest.event("T", date(2026, 9, 19), "V", category="music", end=date(2026, 9, 18))
assert ev["end"] is None, "end on/before start must collapse"

print("smoke: all checks pass")
