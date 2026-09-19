"""Date parsing for the formats Prague listings actually use.

Two shapes cover every source probed so far:

    "6. 8. - 24. 9."                     Czech dotted, year omitted  (ifp.cz)
    "14. 9. - 18. 1. 2027"               Czech dotted, year on the end
    "Vendredi 18 septembre de 10h00"     French, weekday + optional times (pragueaccueil)

A missing year is resolved to the nearest occurrence on or after `today`, constrained by
the weekday when one is written -- "vendredi 18 septembre" must be a Friday, which pins the
year and rejects a listing that is actually a year old.

    python3 dates.py     # self-check
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, time

FR_WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
EN_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# One lookup for both languages: a written weekday pins the year, whatever it is written in.
WEEKDAY_INDEX = {n: i for i, n in enumerate(FR_WEEKDAYS)} | {n: i for i, n in enumerate(EN_WEEKDAYS)}

FR_MONTHS: dict[str, int] = {}
for _i, _names in enumerate(
    [("janvier", "janv"), ("fevrier", "fevr"), ("mars",), ("avril", "avr"), ("mai",), ("juin",),
     ("juillet", "juil"), ("aout",), ("septembre", "sept"), ("octobre", "oct"),
     ("novembre", "nov"), ("decembre", "dec")], start=1):
    for _n in _names:
        FR_MONTHS[_n] = _i

EN_MONTHS: dict[str, int] = {}
for _i, _names in enumerate(
    [("january", "jan"), ("february", "feb"), ("march",), ("april", "apr"), ("may",), ("june", "jun"),
     ("july", "jul"), ("august", "aug"), ("september", "sept", "sep"), ("october", "oct"),
     ("november", "nov"), ("december", "dec")], start=1):
    for _n in _names:
        EN_MONTHS[_n] = _i

# A month name must not match the prefix of a longer word: without this the French
# abbreviation "sept" swallows the English "September" and the day/month order flips.
_MONTH_RE = "(" + "|".join(sorted(FR_MONTHS, key=len, reverse=True)) + r")\.?(?![a-z])"
_EN_MONTH_RE = "(" + "|".join(sorted(EN_MONTHS, key=len, reverse=True)) + r")\.?(?![a-z])"
_WD_RE = "(" + "|".join(FR_WEEKDAYS) + r")"
_EN_WD_RE = "(" + "|".join(EN_WEEKDAYS) + r")"

# "6. 8. 2026", "6. 8.", "6.8."
DOTTED = re.compile(r"\b(\d{1,2})\.\s*(\d{1,2})\.(?:\s*(\d{4}))?")
# "vendredi 18 septembre 2026", "18 septembre"  -- day before month
FRENCH = re.compile(rf"(?:\b{_WD_RE}\s+)?\b(\d{{1,2}})(?:er)?\s+{_MONTH_RE}(?:\s+(\d{{4}}))?")
# "Tuesday, December 8", "December 8 2026"  -- month before day. The (?!\d) stops the day
# capture from eating the first digits of a following year ("December 2026" -> day 20).
ENGLISH = re.compile(
    rf"(?:\b{_EN_WD_RE},?\s+)?\b{_EN_MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)"
    rf"(?:,?\s+(\d{{4}}))?")
# "8 December", "16 September 2026"  -- day before month
ENGLISH_DM = re.compile(
    rf"(?:\b{_EN_WD_RE},?\s+)?\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_EN_MONTH_RE}(?:,?\s+(\d{{4}}))?")
TIME = re.compile(r"\b(\d{1,2})\s*[h:.]\s*(\d{2})?\s*(am|pm)?|\b(\d{1,2})\s*(am|pm)\b", re.I)

# A listing that claims a date this far out is a parse error, not an event.
MAX_HORIZON_DAYS = 18 * 30


class DateError(ValueError):
    pass


@dataclass
class Parsed:
    start: date
    end: date | None
    start_time: time | None
    end_time: time | None
    year_inferred: bool


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.replace(" ", " ").replace(" ", " ")).strip().lower()


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def slug(s: str) -> str:
    """Accent- and punctuation-free key, for comparing titles and venue names."""
    return re.sub(r"[^a-z0-9]+", " ", norm(s)).strip()


def infer_year(month: int, day: int, today: date, weekday: str | None = None) -> date:
    """Nearest occurrence of day/month on or after today, weekday-constrained when named."""
    cands = [d for y in (today.year, today.year + 1, today.year - 1) if (d := _safe(y, month, day))]
    if weekday and (idx := WEEKDAY_INDEX.get(weekday)) is not None:
        cands = [d for d in cands if d.weekday() == idx]
    upcoming = sorted(d for d in cands if d >= today)
    if upcoming:
        return upcoming[0]
    if not cands:
        raise DateError(f"{day:02d}/{month:02d} is not a real date near {today.year}")
    return max(cands)


def parse_times(text: str) -> tuple[time | None, time | None]:
    """Start/end times written as "10h00", "20:30", "7pm", "7:30pm"."""
    found: list[time] = []
    for m in TIME.finditer(text):
        h12, mins, ap1, h24, ap2 = m.groups()
        hour = int(h12 or h24 or 0)
        minute = int(mins or 0)
        ampm = (ap1 or ap2 or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            found.append(time(hour, minute))
    return (found[0] if found else None, found[1] if len(found) > 1 else None)


def _resolve(triples: list[tuple[int, int, int | None, str | None]], today: date) -> list[date]:
    """Day/month/year?/weekday? in document order -> real dates.

    The last date anchors the range: a year written once sits at the end ("14. 9. - 18. 1. 2027"),
    and an earlier date whose month is later belongs to the year before ("14. 9." is 2026).
    """
    out: list[date] = []
    for day, month, year, weekday in reversed(triples):
        if year:
            got = _safe(year, month, day)
        elif out:
            nxt = out[0]
            got = _safe(nxt.year - 1 if month > nxt.month else nxt.year, month, day)
        else:
            try:
                got = infer_year(month, day, today, weekday)
            except DateError:
                got = None
        if got:
            out.insert(0, got)
    if not out:
        raise DateError("no resolvable date")
    return out


def parse(text: str, today: date) -> Parsed:
    """First date in `text` as start, last as end. Raises DateError if none."""
    t = norm(text)
    st, et = parse_times(t)

    dotted = DOTTED.findall(t)
    if dotted:
        triples = [(int(d), int(m), int(y) if y else None, None) for d, m, y in dotted]
    elif (fr := FRENCH.findall(t)):
        triples = [(int(d), FR_MONTHS[mon], int(y) if y else None, wd or None)
                   for wd, d, mon, y in fr]
    elif (en := ENGLISH.findall(t)):
        triples = [(int(d), EN_MONTHS[mon], int(y) if y else None, wd or None)
                   for wd, mon, d, y in en]
    elif (en := ENGLISH_DM.findall(t)):
        triples = [(int(d), EN_MONTHS[mon], int(y) if y else None, wd or None)
                   for wd, d, mon, y in en]
    else:
        triples = []
    if not triples:
        raise DateError(f"no date in {text!r}")

    days = _resolve(triples, today)
    inferred = any(y is None for _, _, y, _ in triples)
    start, end = days[0], (days[-1] if len(days) > 1 else None)
    if end == start:
        end = None
    return Parsed(start, end, st, et, inferred)


def plausible(d: date, today: date) -> bool:
    """Guard against source typos -- eterniasmichov.com really does publish "10. 10. 0020"."""
    return today <= d <= date.fromordinal(today.toordinal() + MAX_HORIZON_DAYS)


def _selfcheck() -> None:
    t = date(2026, 9, 19)  # a Saturday

    p = parse("14. 9. – 18. 1. 2027", t)
    assert (p.start, p.end) == (date(2026, 9, 14), date(2027, 1, 18)), p

    # no year anywhere: both ends inferred forward from today
    p = parse("6. 8. – 24. 9.", t)
    assert p.end == date(2026, 9, 24) and p.year_inferred, p

    # a single trailing year applies to the whole range
    p = parse("2. 1. – 5. 1. 2027", t)
    assert (p.start, p.end) == (date(2027, 1, 2), date(2027, 1, 5)), p

    # French, weekday-constrained: 18 September 2026 is a Friday, so that is the year meant
    p = parse("Vendredi 18 septembre de 10h00 à 12h00", t)
    assert p.start == date(2026, 9, 18), p
    assert (p.start_time, p.end_time) == (time(10, 0), time(12, 0)), p

    # the weekday rules out 2026 (a Saturday) and picks the year where 20 June is a Friday
    p = parse("Vendredi 20 juin", t)
    assert FR_WEEKDAYS[p.start.weekday()] == "vendredi", p

    p = parse("1er mai 2027", t)
    assert p.start == date(2027, 5, 1), p

    # English, month before day, weekday-constrained (8 Dec 2026 is a Tuesday)
    p = parse("Tuesday, December 8", t)
    assert p.start == date(2026, 12, 8), p

    p = parse("8 December 2026", t)
    assert p.start == date(2026, 12, 8), p

    # 12-hour clock, with and without minutes
    p = parse("Tuesday, December 8 From 7pm to 10:15pm", t)
    assert (p.start_time, p.end_time) == (time(19, 0), time(22, 15)), p

    p = parse("16 September 2026", t)
    assert p.start == date(2026, 9, 16), p

    assert parse("26. 9. 2026", t).end is None
    assert not plausible(date(20, 10, 10), t)      # the real Eternia typo
    assert not plausible(date(2026, 1, 1), t)      # past
    assert plausible(date(2027, 1, 1), t)

    for junk in ("", "à venir", "99. 99."):
        try:
            parse(junk, t)
        except DateError:
            pass
        else:
            raise AssertionError(f"{junk!r} should not parse")

    print("dates.py: all checks pass")


if __name__ == "__main__":
    _selfcheck()
