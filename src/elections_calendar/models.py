"""Core data types: date periods, source candidates and calendar events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

LEVELS = ("national", "regional")
ELECTION_TYPES = ("parliamentary", "presidential", "upper_house", "regional", "european_parliament")

# Ordered from most to least precise; used for display and tie-breaking.
PRECISIONS = ("day", "range", "month", "season", "quarter", "half", "deadline", "year")

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Period:
    """A known date or the interval an election is expected to fall in.

    ``precision`` says how to read the interval: ``day`` means the election is
    on ``start`` (``end`` > ``start`` only for multi-day polling, e.g. Czech
    Friday-Saturday votes); anything else means "somewhere in [start, end]".
    """

    start: date
    end: date
    precision: str
    text: str = ""

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"period ends before it starts: {self.start} > {self.end}")
        if self.precision not in PRECISIONS:
            raise ValueError(f"unknown precision {self.precision!r}")

    @property
    def width(self) -> int:
        return (self.end - self.start).days

    @property
    def is_exact(self) -> bool:
        return self.precision == "day"

    def overlaps(self, other: "Period", slack_days: int = 0) -> bool:
        return (
            self.start.toordinal() - slack_days <= other.end.toordinal()
            and other.start.toordinal() - slack_days <= self.end.toordinal()
        )

    def intersect(self, other: "Period") -> "Period | None":
        start, end = max(self.start, other.start), min(self.end, other.end)
        if start > end:
            return None
        # The narrower input decides how the intersection is labelled.
        narrow = self if self.width <= other.width else other
        if (start, end) == (narrow.start, narrow.end):
            return narrow
        precision = "day" if start == end else "range"
        return Period(start, end, precision, narrow.text)

    def sort_key(self) -> tuple:
        return (self.start, self.width)


@dataclass
class Candidate:
    """One source's claim that an election will take place."""

    source: str
    country_code: str
    level: str
    election_type: str
    period: Period
    region: str = ""
    region_id: str = ""
    aggregate: bool = False  # covers every region of the country at once
    indirect: bool = False
    title: str = ""
    second_round: date | None = None
    url: str = ""
    wikidata_id: str = ""
    wikipedia_title: str = ""
    confidence: str = "medium"
    notes: str = ""
    # False for analysts' estimates: they describe the election but must not
    # narrow dates that other sources give.
    narrows: bool = True


@dataclass
class Event:
    """A row of the calendar: one election, reconciled across sources and runs."""

    event_id: str
    country_code: str
    country: str
    level: str
    election_type: str
    indirect: bool
    region: str
    title: str
    date_start: date
    date_end: date
    date_precision: str
    date_text: str
    status: str
    confidence: str
    second_round: date | None = None
    region_id: str = ""
    aggregate: bool = False
    sources: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    wikidata_id: str = ""
    wikipedia_title: str = ""
    evidence: str = ""
    notes: str = ""
    first_seen: date | None = None
    last_updated: date | None = None
    last_seen: date | None = None
    seen_in_last_run: bool = True

    @property
    def period(self) -> Period:
        return Period(self.date_start, self.date_end, self.date_precision, self.date_text)

    def set_period(self, period: Period) -> None:
        self.date_start, self.date_end = period.start, period.end
        self.date_precision, self.date_text = period.precision, period.text


@dataclass
class Change:
    run_date: date
    event_id: str
    change: str  # added | refined | rescheduled | updated | held | not_seen
    field: str = ""
    old: str = ""
    new: str = ""
    description: str = ""
