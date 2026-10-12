"""Turn the many ways sources write election dates into :class:`Period` objects.

Handles e.g. "18 April 2027", "9–10 October", "By 25 July", "by October",
"March 2028", "late May", "Spring 2027", "September-October 2028", "Q2 2027",
"first half of 2027", "2027", "TBD", and second rounds written as
"18 April 2027; 2 May 2027 if a second round is required" or
"25 October (run-off 1 November)".
"""

from __future__ import annotations

import calendar
import re
from datetime import date

from .models import Period

MONTHS = {
    name: i
    for i, names in enumerate(
        [
            (),
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ]
    )
    for name in names
}
_MONTH_RE = "(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
_YEAR_RE = r"((?:19|20)\d\d)"

SEASONS = {
    "spring": ((3, 1), (5, 31)),
    "summer": ((6, 1), (8, 31)),
    "autumn": ((9, 1), (11, 30)),
    "fall": ((9, 1), (11, 30)),
    "winter": ((1, 1), (2, 28)),
}

SECOND_ROUND_RE = re.compile(r"run-?off|second round|2nd round|second ballot|ballottaggio", re.I)


def last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _clean(text: str) -> str:
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    text = text.replace("\xa0", " ")
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _year(match_year: str | None, year_hint: int | None) -> int | None:
    if match_year:
        return int(match_year)
    return year_hint


def parse_period(text: str, year_hint: int | None = None) -> Period | None:
    """Parse the *first* date expression in ``text``."""
    if not text:
        return None
    raw = text.strip()
    t = _clean(raw)

    # ISO date or ISO range
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:\s*(?:-|to|/)\s*(\d{4})-(\d{2})-(\d{2}))?", t)
    if m:
        start = date(int(m[1]), int(m[2]), int(m[3]))
        end = date(int(m[4]), int(m[5]), int(m[6])) if m[4] else start
        return Period(start, end, "day" if start == end or (end - start).days <= 2 else "range", raw)

    deadline = bool(re.match(r"^(by|before|no later than|not later than|until|due by)\b", t))

    # 30 September - 1 October 2026
    m = re.search(rf"\b(\d{{1,2}}) {_MONTH_RE}\s*-\s*(\d{{1,2}}) {_MONTH_RE}(?: {_YEAR_RE})?", t)
    if m:
        year = _year(m[5], year_hint)
        if year:
            start = date(year, MONTHS[m[2]], int(m[1]))
            end = date(year, MONTHS[m[4]], int(m[3]))
            if end >= start:
                return Period(start, end, "day" if (end - start).days <= 2 else "range", raw)

    # 9-10 October 2026 / 9 and 10 October
    m = re.search(rf"\b(\d{{1,2}})\s*(?:-|and|&)\s*(\d{{1,2}}) {_MONTH_RE}(?:,? {_YEAR_RE})?", t)
    if m:
        year = _year(m[4], year_hint)
        if year:
            month = MONTHS[m[3]]
            start, end = date(year, month, int(m[1])), date(year, month, int(m[2]))
            if end >= start:
                return Period(start, end, "day" if (end - start).days <= 2 else "range", raw)

    # 18 April 2027 / April 18, 2027
    m = re.search(rf"\b(\d{{1,2}}) {_MONTH_RE}(?:,? {_YEAR_RE})?", t)
    m2 = re.search(rf"\b{_MONTH_RE} (\d{{1,2}})(?:,? {_YEAR_RE})?\b", t)
    if m or m2:
        if m:
            day, month, year = int(m[1]), MONTHS[m[2]], _year(m[3], year_hint)
        else:
            day, month, year = int(m2[2]), MONTHS[m2[1]], _year(m2[3], year_hint)
        if year and 1 <= day <= calendar.monthrange(year, month)[1]:
            d = date(year, month, day)
            if deadline:
                return Period(date(year, 1, 1), d, "deadline", raw)
            return Period(d, d, "day", raw)

    # September-October 2028
    m = re.search(rf"\b{_MONTH_RE}\s*(?:-|to|/|or)\s*{_MONTH_RE}(?: {_YEAR_RE})?", t)
    if m:
        year = _year(m[3], year_hint)
        if year and MONTHS[m[2]] >= MONTHS[m[1]]:
            return Period(date(year, MONTHS[m[1]], 1), last_day(year, MONTHS[m[2]]), "range", raw)

    # early / mid / late May 2027
    m = re.search(rf"\b(early|beginning of|mid|middle of|late|end of) {_MONTH_RE}(?: {_YEAR_RE})?", t)
    if m:
        year = _year(m[3], year_hint)
        if year:
            month = MONTHS[m[2]]
            part = m[1]
            if part in ("early", "beginning of"):
                return Period(date(year, month, 1), date(year, month, 10), "range", raw)
            if part in ("mid", "middle of"):
                return Period(date(year, month, 11), date(year, month, 20), "range", raw)
            return Period(date(year, month, 21), last_day(year, month), "range", raw)

    # (by) March 2028 / March
    m = re.search(rf"\b{_MONTH_RE}(?: {_YEAR_RE})?\b", t)
    if m and m[1] == "may" and not m[2] and not re.fullmatch(r"(by |in |before )?may\.?", t):
        m = None  # "may" as a verb ("elections may be held ...")
    if m:
        year = _year(m[2], year_hint)
        if year:
            month = MONTHS[m[1]]
            if deadline:
                return Period(date(year, 1, 1), last_day(year, month), "deadline", raw)
            return Period(date(year, month, 1), last_day(year, month), "month", raw)

    # Spring 2027 / autumn
    m = re.search(rf"\b(spring|summer|autumn|fall|winter)(?: of)?(?: {_YEAR_RE})?\b", t)
    if m:
        year = _year(m[2], year_hint)
        if year:
            (sm, sd), (em, ed) = SEASONS[m[1]]
            end_day = min(ed, calendar.monthrange(year, em)[1])
            return Period(date(year, sm, sd), date(year, em, end_day), "season", raw)

    # Q2 2027
    m = re.search(rf"\bq([1-4])(?: {_YEAR_RE})?\b", t)
    if m:
        year = _year(m[2], year_hint)
        if year:
            q = int(m[1])
            return Period(date(year, 3 * q - 2, 1), last_day(year, 3 * q), "quarter", raw)

    # first half of 2027 / H2 2027 / early 2027 / late 2027
    m = re.search(rf"\b(first half|1st half|h1|second half|2nd half|h2|early|late|end of)(?: of)? {_YEAR_RE}\b", t)
    if m:
        year = int(m[2])
        if m[1] in ("first half", "1st half", "h1"):
            return Period(date(year, 1, 1), date(year, 6, 30), "half", raw)
        if m[1] in ("second half", "2nd half", "h2"):
            return Period(date(year, 7, 1), date(year, 12, 31), "half", raw)
        if m[1] == "early":
            return Period(date(year, 1, 1), date(year, 4, 30), "season", raw)
        return Period(date(year, 9, 1), date(year, 12, 31), "season", raw)

    # 2027
    m = re.search(rf"\b{_YEAR_RE}\b", t)
    if m:
        year = int(m[1])
        return Period(date(year, 1, 1), date(year, 12, 31), "year", raw)

    # TBD / unknown date: only the year of the page is known
    if year_hint and re.search(r"\b(tbd|tbc|tba|unknown|unspecified|not yet)\b", t):
        return Period(date(year_hint, 1, 1), date(year_hint, 12, 31), "year", raw)
    return None


def year_period(year: int, text: str = "") -> Period:
    return Period(date(year, 1, 1), date(year, 12, 31), "year", text or str(year))


def split_rounds(text: str, year_hint: int | None = None) -> tuple[Period | None, date | None]:
    """Return (first-round period, second-round date) from a date text."""
    if not text:
        return None, None
    parts = re.split(r";|\(|\)|,(?=\s*(?:run|second|2nd))", text)
    first: Period | None = None
    second: date | None = None
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if SECOND_ROUND_RE.search(part):
            p = parse_period(part, year_hint)
            if p and p.precision == "day" and second is None:
                second = p.start
        elif first is None:
            first = parse_period(part, year_hint)
    if first is None:
        first = parse_period(text, year_hint)
    if first is not None and second is not None and second <= first.start:
        second = None
    return first, second
