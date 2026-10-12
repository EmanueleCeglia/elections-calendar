"""From source candidates to calendar events, and from one run to the next.

1. ``build_events``: group candidates describing the same election (same
   country/level/type/region and compatible dates, or same Wikidata item),
   then derive one event per group. The date is the intersection of the
   sources' periods, taken in priority order: "By 25 July" + "spring 2027"
   + "2027" -> spring 2027; a conflicting lower-priority date is recorded in
   the notes instead of being applied.
2. ``reconcile``: match this run's events to the stored calendar. New events
   are added, more precise dates replace vaguer ones, moved dates are
   recorded as reschedules, finished elections become "held". Nothing is
   deleted: an event no source reports any more is flagged, not dropped.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from .config import Config
from .models import CONFIDENCE_RANK, Candidate, Change, Event, Period
from .textutil import same_region_name, slugify

SAME_ELECTION_SLACK_DAYS = 45
STATUS_RANK = {"unconfirmed": -1, "estimated": 0, "expected": 1, "confirmed": 2, "held": 3}


# --------------------------------------------------------------- identity
def same_identity(a, b) -> bool:
    """Same body in the same place (dates are compared separately)."""
    if a.country_code != b.country_code:
        return False
    if a.wikidata_id and a.wikidata_id == b.wikidata_id:
        return True
    if a.level != b.level or a.election_type != b.election_type:
        return False
    if a.level == "national":
        return True
    if a.aggregate or b.aggregate:
        return a.aggregate and b.aggregate
    if a.region_id and b.region_id:
        return a.region_id == b.region_id
    return same_region_name(a.region, b.region)


# ------------------------------------------------------------- clustering
def cluster_candidates(candidates: list[Candidate], config: Config) -> list[list[Candidate]]:
    clusters: list[list[Candidate]] = []
    ordered = sorted(candidates, key=lambda c: (c.period.width, -config.source_priority(c.source)))
    for cand in ordered:
        target = None
        for cl in clusters:
            if cand.wikidata_id and any(m.wikidata_id == cand.wikidata_id for m in cl):
                target = cl
                break
        if target is None:
            for cl in clusters:
                if same_identity(cl[0], cand) and any(
                    m.period.overlaps(cand.period, SAME_ELECTION_SLACK_DAYS) for m in cl
                ):
                    target = cl
                    break
        if target is None:
            # Same body, incompatible dates, reported by a source not yet in
            # the group: sources disagree about one election (one source
            # listing two dates for the same body means two elections).
            for cl in clusters:
                if same_identity(cl[0], cand) and all(m.source != cand.source for m in cl):
                    target = cl
                    break
        if target is None:
            clusters.append([cand])
        else:
            target.append(cand)
    return clusters


# ----------------------------------------------------------- consolidation
def _display_title(title: str, period: Period) -> str:
    if title.lower().startswith("next ") and period.start.year == period.end.year:
        return f"{period.start.year} {title[5:]}"
    return title


def consolidate(cluster: list[Candidate], config: Config) -> Event:
    by_priority = sorted(
        cluster, key=lambda c: (-config.source_priority(c.source), c.period.width)
    )
    # Weak evidence (stale Wikidata placeholders, speculation) shows that an
    # election exists but only sets the date when nothing better is known.
    informative = [c for c in by_priority if c.confidence != "low" and c.narrows] or by_priority
    period = informative[0].period
    conflicts = []
    for cand in informative[1:]:
        narrowed = period.intersect(cand.period)
        if narrowed is None:
            conflicts.append(f"{cand.source} says {cand.period.text or cand.period.start}")
        else:
            period = narrowed

    sources = sorted({c.source.split(":")[0] for c in cluster})
    agreeing_exact = {c.source for c in informative if c.period.is_exact and c.period.start == period.start}
    # "28 September" in one list and "By 28 September" in another is a legal
    # deadline, not a fixed date, unless a second source gives that day too.
    deadlines = [c.period for c in informative if c.period.precision == "deadline" and c.period.end == period.start]
    if period.is_exact and deadlines and len(agreeing_exact) < 2:
        period = deadlines[0]
    if period.is_exact:
        official = any(c.confidence == "high" and c.source in ("cor_pdf", "manual") for c in cluster)
        status = "confirmed" if (len(agreeing_exact) >= 2 or official) else "expected"
    else:
        status = "estimated"

    best_conf = max(CONFIDENCE_RANK[c.confidence] for c in cluster)
    if len(sources) >= 2:
        best_conf = max(best_conf, CONFIDENCE_RANK["high"])
    confidence = {v: k for k, v in CONFIDENCE_RANK.items()}[best_conf]

    second_rounds = [c.second_round for c in by_priority if c.second_round and c.second_round > period.start]
    title_rank = lambda c: (  # noqa: E731
        0 if c.source == "manual" and c.title else
        1 if c.wikipedia_title else
        2 if c.wikidata_id else
        3 if c.source == "llm" else 4
    )
    title_source = min(by_priority, key=title_rank)
    region_names = Counter(c.region for c in cluster if c.region)
    with_region_id = [c for c in by_priority if c.region_id]
    region = with_region_id[0].region if with_region_id else (region_names.most_common(1)[0][0] if region_names else "")
    country = config.country(by_priority[0].country_code)

    evidence = "; ".join(
        f"{c.source}: {c.period.text or c.period.start.isoformat()}"
        for c in sorted(cluster, key=lambda c: -config.source_priority(c.source))
    )
    notes = [n for n in dict.fromkeys(c.notes for c in by_priority if c.source in ("manual", "llm") and c.notes)]
    if conflicts:
        notes.append("Conflicting dates: " + "; ".join(conflicts))

    first = by_priority[0]
    return Event(
        event_id="",
        country_code=first.country_code,
        country=country.name if country else first.country_code,
        level=first.level,
        election_type=first.election_type,
        # Only national bodies are "indirect"; a Landtag electing a governor is
        # government formation, not an election in this calendar.
        indirect=first.level == "national" and any(c.indirect for c in cluster),
        region=region,
        region_id=with_region_id[0].region_id if with_region_id else "",
        aggregate=first.aggregate,
        title=_display_title(title_source.title, period),
        date_start=period.start,
        date_end=period.end,
        date_precision=period.precision,
        date_text=period.text,
        second_round=second_rounds[0] if second_rounds else None,
        status=status,
        confidence=confidence,
        sources=sources,
        source_urls=list(dict.fromkeys(c.url for c in by_priority if c.url)),
        wikidata_id=next((c.wikidata_id for c in by_priority if c.wikidata_id), ""),
        wikipedia_title=next((c.wikipedia_title for c in by_priority if c.wikipedia_title), ""),
        evidence=evidence,
        notes=" | ".join(notes),
    )


def fold_aggregates(events: list[Event]) -> list[Event]:
    """One row per region, or one row for a nationwide regional round.

    When structured sources list the regions one by one (Spanish
    autonomous communities), the umbrella row ("2027 Spanish regional
    elections") is dropped. When the regions come only from the LLM while
    other sources describe one nationwide round (Slovak or Dutch regional
    elections), the umbrella row is kept and the regions are noted on it.
    """
    drop: set[int] = set()
    for agg in (e for e in events if e.aggregate):
        parts = [
            o for o in events
            if o is not agg and not o.aggregate and o.level == "regional"
            and o.country_code == agg.country_code and o.period.overlaps(agg.period)
        ]
        if not parts:
            continue
        if any(set(p.sources) - {"llm"} for p in parts):
            drop.add(id(agg))
        elif set(agg.sources) - {"llm"}:
            drop.update(id(p) for p in parts)
            names = ", ".join(sorted(p.region for p in parts))
            agg.notes = " | ".join(n for n in (agg.notes, f"Regions: {names}") if n)
    return [e for e in events if id(e) not in drop]


def cross_check_with_llm(events: list[Event]) -> None:
    """Downgrade single-source events the LLM research implicitly contradicts.

    If the LLM listed other elections of the same country and level in the
    same period (so it researched that round) but not this one, an event
    backed by one other source only goes to review instead of the calendar.
    E.g. a calendar PDF still listing two regions that left the common
    regional cycle after snap elections.
    """
    llm_events = [e for e in events if "llm" in e.sources]
    for ev in events:
        if len(ev.sources) != 1 or "llm" in ev.sources or ev.sources == ["manual"]:
            continue
        if any(o.country_code == ev.country_code and o.level == ev.level and o.period.overlaps(ev.period)
               for o in llm_events):
            ev.confidence = "low"
            ev.notes = " | ".join(n for n in (ev.notes, "Not found by the LLM research, which did list "
                                              "other elections of this round") if n)


def build_events(candidates: list[Candidate], config: Config) -> list[Event]:
    events = [consolidate(cl, config) for cl in cluster_candidates(candidates, config)]
    return fold_aggregates(events)


def in_window(ev: Event, start: date, end: date) -> bool:
    if ev.date_end >= start and ev.date_start <= end:
        return True
    return bool(ev.second_round and start <= ev.second_round <= end)


# ------------------------------------------------------------- reconcile
def make_event_id(ev: Event, taken: set[str]) -> str:
    level = "NAT" if ev.level == "national" else "REG"
    if ev.level == "regional":
        what = "all" if ev.aggregate else slugify(ev.region or "region", 30)
    else:
        what = slugify(ev.election_type, 20)
    base = f"{ev.country_code}-{level}-{what}-{ev.date_start.year}"
    event_id, n = base, 2
    while event_id in taken:
        event_id, n = f"{base}-{n}", n + 1
    return event_id


def _fmt(p: Period) -> str:
    if p.is_exact:
        return p.start.isoformat() if p.start == p.end else f"{p.start.isoformat()}..{p.end.isoformat()}"
    return f"{p.text} [{p.start.isoformat()}..{p.end.isoformat()}]"


def _match(ev: Event, previous: list[Event], used: set[str], fresh: list[Event]) -> Event | None:
    pool = [p for p in previous if p.event_id not in used]
    for p in pool:
        if ev.wikidata_id and p.wikidata_id == ev.wikidata_id:
            return p
    overlapping = [p for p in pool if same_identity(p, ev) and p.period.overlaps(ev.period, SAME_ELECTION_SLACK_DAYS)]
    if overlapping:
        return min(overlapping, key=lambda p: abs((p.date_start - ev.date_start).days))
    # Reschedule: the stored event's date no longer matches anything fresh.
    for p in pool:
        if p.status == "held" or not same_identity(p, ev):
            continue
        others = [f for f in fresh if f is not ev and same_identity(f, p) and f.period.overlaps(p.period, SAME_ELECTION_SLACK_DAYS)]
        if not others:
            return p
    return None


def reconcile(
    previous: list[Event], fresh: list[Event], run_date: date, stale_after_days: int = 21
) -> tuple[list[Event], list[Change]]:
    changes: list[Change] = []
    used: set[str] = set()
    taken = {p.event_id for p in previous}
    result: list[Event] = []

    for ev in sorted(fresh, key=lambda e: (e.period.width, e.date_start)):
        prev = _match(ev, previous, used, fresh)
        if prev is None:
            ev.event_id = make_event_id(ev, taken)
            taken.add(ev.event_id)
            ev.first_seen = ev.last_updated = ev.last_seen = run_date
            ev.seen_in_last_run = True
            changes.append(Change(run_date, ev.event_id, "added", "date", "", _fmt(ev.period),
                                  f"New: {ev.title} ({ev.country})"))
            result.append(ev)
            continue
        used.add(prev.event_id)
        changes += _update(prev, ev, run_date)
        result.append(prev)

    for prev in previous:
        if prev.event_id in used:
            continue
        prev.seen_in_last_run = False
        # Upcoming elections that no source reports any more (cancelled,
        # merged into another vote, or a source error) leave the calendar
        # after a grace period and wait in "Needs review".
        if (
            prev.status not in ("held", "unconfirmed")
            and prev.last_seen
            and (run_date - prev.last_seen).days > stale_after_days
        ):
            changes.append(Change(run_date, prev.event_id, "unconfirmed", "status", prev.status, "unconfirmed",
                                  f"Not reported by any source since {prev.last_seen}: {prev.title}"))
            prev.status = "unconfirmed"
            prev.last_updated = run_date
        result.append(prev)

    for ev in result:
        last_day = max(ev.date_end, ev.second_round or ev.date_end)
        if ev.status != "held" and last_day < run_date:
            what = "Took place" if ev.date_precision == "day" else "Expected period is over"
            changes.append(Change(run_date, ev.event_id, "held", "status", ev.status, "held",
                                  f"{what}: {ev.title}"))
            ev.status = "held"
            ev.last_updated = run_date
    return result, changes


def _update(prev: Event, ev: Event, run_date: date) -> list[Change]:
    changes: list[Change] = []
    old, new = prev.period, ev.period
    date_changed = False
    small_shift = (
        not old.is_exact and not new.is_exact
        and abs((new.start - old.start).days) <= 7 and abs((new.end - old.end).days) <= 7
    )
    if (old.start, old.end, old.precision) != (new.start, new.end, new.precision) and not small_shift:
        if new.start >= old.start and new.end <= old.end:
            kind = "refined"
        elif not new.overlaps(old) or (old.is_exact and new.is_exact):
            kind = "rescheduled"
        elif new.width < old.width:
            kind = "refined"  # overlapping and narrower ("Oct-Nov" -> "November")
        elif set(prev.sources) <= set(ev.sources):
            # Every source behind the stored date reported again and together
            # they now only support a wider period: the narrower date was
            # noise (e.g. one inconsistent LLM answer), so follow the sources.
            kind = "revised"
        else:
            kind = None  # a source is missing this run: keep the better date
        if kind:
            changes.append(Change(run_date, prev.event_id, kind, "date", _fmt(old), _fmt(new),
                                  f"{prev.title}: {_fmt(old)} -> {_fmt(new)}"))
            prev.set_period(new)
            date_changed = True
    if ev.second_round and ev.second_round != prev.second_round and ev.second_round > prev.date_start:
        changes.append(Change(run_date, prev.event_id, "updated", "second_round",
                              str(prev.second_round or ""), ev.second_round.isoformat(), prev.title))
        prev.second_round = ev.second_round
    # The status describes the date: it follows a changed date (a confirmed
    # day postponed to "autumn" is estimated again), otherwise only upgrades.
    same_date = prev.period == ev.period
    if prev.status != "held" and ev.status != prev.status and (
        date_changed or (same_date and STATUS_RANK[ev.status] > STATUS_RANK.get(prev.status, 0))
    ):
        changes.append(Change(run_date, prev.event_id, "updated", "status", prev.status, ev.status, prev.title))
        prev.status = ev.status
    prev.confidence = ev.confidence  # reflects the latest evidence
    for attr in ("wikidata_id", "wikipedia_title", "region_id"):
        if not getattr(prev, attr) and getattr(ev, attr):
            setattr(prev, attr, getattr(ev, attr))
    if prev.title != ev.title and ev.wikipedia_title and ev.title:
        prev.title = ev.title
    prev.indirect = prev.indirect or ev.indirect
    prev.sources = sorted(set(prev.sources) | set(ev.sources))
    prev.source_urls = list(dict.fromkeys(prev.source_urls + ev.source_urls))
    prev.evidence = ev.evidence
    prev.notes = ev.notes or prev.notes
    prev.last_seen = run_date
    prev.seen_in_last_run = True
    if changes:
        prev.last_updated = run_date
    return changes


def split_by_confidence(events: list[Event], minimum: str = "medium") -> tuple[list[Event], list[Event]]:
    keep, review = [], []
    for ev in events:
        (keep if CONFIDENCE_RANK[ev.confidence] >= CONFIDENCE_RANK[minimum] else review).append(ev)
    return keep, review
