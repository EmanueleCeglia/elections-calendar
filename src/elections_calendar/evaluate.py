"""Compare the calendar with a hand-checked ground-truth list.

Reports recall (ground-truth elections found), precision (calendar events
that are in the ground truth) and, for matched pairs, whether the dates agree.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .merge import SAME_ELECTION_SLACK_DAYS, same_identity
from .models import Event, Period
from .store import read_calendar


@dataclass
class TruthRow:
    country_code: str
    level: str
    election_type: str
    region: str
    title: str
    period: Period
    second_round: date | None
    aggregate: bool
    wikidata_id: str = ""
    region_id: str = ""


def read_truth(path: Path) -> list[TruthRow]:
    rows = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            start, end = date.fromisoformat(r["date_start"]), date.fromisoformat(r["date_end"])
            precision = r["date_precision"] if r["date_precision"] in ("day", "range", "month", "season", "year") else "range"
            rows.append(TruthRow(
                country_code=r["country_code"], level=r["level"], election_type=r["election_type"],
                region=r["region"], title=r["title"],
                period=Period(start, end, precision, r.get("date_text", "")),
                second_round=date.fromisoformat(r["second_round"]) if r.get("second_round") else None,
                aggregate=r["level"] == "regional" and r["region"].lower().startswith("all"),
            ))
    return rows


def date_verdict(truth: Period, found: Period) -> str:
    if truth.is_exact:
        if found.is_exact:
            return "exact" if found.start == truth.start else "wrong day"
        return "consistent, less precise" if found.start <= truth.start <= found.end else "inconsistent"
    if not found.overlaps(truth):
        return "inconsistent"
    if found.is_exact:
        return "consistent, more precise"
    return "consistent"


def evaluate(truth_path: Path, calendar_path: Path, start: date, end: date) -> dict:
    truth = read_truth(truth_path)
    events = [
        e for e in read_calendar(calendar_path)
        if e.status != "held" or e.date_end >= start
    ]
    events = [e for e in events if (e.date_end >= start and e.date_start <= end)
              or (e.second_round and start <= e.second_round <= end)]
    used: set[str] = set()
    matches: list[tuple[TruthRow, Event | None, str]] = []
    for t in sorted(truth, key=lambda t: t.period.width):
        options = [
            e for e in events
            if e.event_id not in used and same_identity(t, e)
            and e.period.overlaps(t.period, SAME_ELECTION_SLACK_DAYS)
        ]
        if not options:
            matches.append((t, None, "missing"))
            continue
        best = min(options, key=lambda e: abs((e.date_start - t.period.start).days))
        used.add(best.event_id)
        matches.append((t, best, date_verdict(t.period, best.period)))
    extras = [e for e in events if e.event_id not in used]
    found = [m for m in matches if m[1] is not None]
    verdicts: dict[str, int] = {}
    for _, _, v in found:
        verdicts[v] = verdicts.get(v, 0) + 1
    return {
        "truth": len(truth),
        "calendar": len(events),
        "matched": len(found),
        "recall": len(found) / len(truth) if truth else 0.0,
        "precision": len(found) / len(events) if events else 0.0,
        "date_verdicts": verdicts,
        "missing": [t for t, e, _ in matches if e is None],
        "extras": extras,
        "pairs": [(t, e, v) for t, e, v in matches if e is not None],
    }


def print_report(result: dict) -> None:
    print(f"Ground truth: {result['truth']}  |  calendar events in window: {result['calendar']}  |  matched: {result['matched']}")
    print(f"Recall:    {result['recall']:.1%}")
    print(f"Precision: {result['precision']:.1%}")
    print("Dates of matched events: " + ", ".join(f"{k}: {v}" for k, v in sorted(result["date_verdicts"].items())))
    if result["missing"]:
        print("\nMissing (in ground truth, not in calendar):")
        for t in result["missing"]:
            print(f"  - {t.country_code} {t.level:8} {t.region or '-':28} {t.title}  [{t.period.text}]")
    if result["extras"]:
        print("\nExtra (in calendar, not in ground truth):")
        for e in result["extras"]:
            print(f"  - {e.country_code} {e.level:8} {e.region or '-':28} {e.title}  [{e.date_text}]  sources={','.join(e.sources)} conf={e.confidence}")
    off = [(t, e, v) for t, e, v in result["pairs"] if v in ("wrong day", "inconsistent")]
    if off:
        print("\nDate disagreements:")
        for t, e, v in off:
            print(f"  - {t.title}: truth {t.period.text} vs calendar {e.date_text} ({e.date_start}..{e.date_end}) -> {v}")
