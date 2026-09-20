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

# venue spellings must collapse to one name, or the same gig shows twice
assert harvest.canonical_venue("MeetFactory") == "Meet Factory"
assert harvest.canonical_venue("Futurum Music Bar") == "Futurum"
assert harvest.canonical_venue("Kino Aero") == "Kino Aero"

# a promoter's title and the venue's title for one gig
w = harvest.title_words
assert harvest.same_event(w("Rock for People presents: Vundabar (USA), Yot Club (USA)"),
                          w("Vundabar + Yot Club"))
assert harvest.same_event(w("Beartooth"), w("Beartooth (USA)"))
# ...but two different nights that merely share a genre word must stay apart
assert not harvest.same_event(w("Techno Night"), w("Techno Party"))
assert not harvest.same_event(w("Spiritbox"), w("Beartooth"))
# two distinct talks at one institute, sharing only that venue's boilerplate
assert not harvest.same_event(
    w("COMMUNICATION OF SCIENCE - LIGHT SHOW - festive illumination of the building"),
    w("SPECTROSCOPE – Heyrovsky Institute - Science Trail of the Institute"))
# near-identical phrasings of one event still merge
assert harvest.same_event(w("Signal from Earth and Deep Space"),
                          w("Signals from Earth and Deep Space"))

def _e(title, venue="Rock Café", day="2026-09-27", source="a", **kw):
    return {"title": title, "venue": venue, "date": day, "source": source,
            "time": None, "image": None, "info": None, "url": None, "tag": None,
            "room": None, "end": None} | kw

merged = harvest.dedupe([
    _e("Rock for People presents: Vundabar (USA), Yot Club (USA)", source="rockcafe", time="19:00"),
    _e("Vundabar + Yot Club", source="rfpconcerts", info="+ Midnight Honeycake"),
    _e("Vundabar + Yot Club", source="rfpconcerts", venue="Roxy"),      # other venue
    _e("Vundabar + Yot Club", source="rfpconcerts", day="2026-10-01"),  # other day
])
assert len(merged) == 3, [m["title"] for m in merged]
assert merged[0]["time"] == "19:00", "the venue's start time is kept"
assert merged[0]["info"] == "+ Midnight Honeycake", "the promoter's line-up fills the gap"

# One source listing two similar things at one venue on one night means two events.
both = harvest.dedupe([
    _e("CTU ROBOTICS STUDENT TEAM", venue="Faculty of Mechanical Engineering", source="nocvedy"),
    _e("THE AEROLAB STUDENT TEAM", venue="Faculty of Mechanical Engineering", source="nocvedy"),
])
assert len(both) == 2, [m["title"] for m in both]

print("smoke: all checks pass")
