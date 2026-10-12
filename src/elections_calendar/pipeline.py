"""Run the whole pipeline: fetch -> normalise -> merge -> reconcile -> write."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .classify import exclusion_reason, is_aggregate_territory, not_an_election
from .config import Config
from .http import HttpClient
from .merge import build_events, cross_check_with_llm, in_window, reconcile, same_identity, split_by_confidence, SAME_ELECTION_SLACK_DAYS
from .models import Candidate, Change, Event
from .sources.base import Context, Window
from .sources.cor_pdf import CorPdfSource
from .sources.llm_openai import OpenAIResearchSource
from .sources.manual import ManualSource
from .sources.wikidata_source import WikidataSource
from .sources.wikipedia import WikipediaSource
from . import store

log = logging.getLogger(__name__)

# Wikipedia and Wikidata run first: they build the region index that lets
# names coming from the CoR PDF and the LLM resolve to the same regions.
SOURCES = {
    "wikipedia": WikipediaSource,
    "wikidata": WikidataSource,
    "cor_pdf": CorPdfSource,
    "manual": ManualSource,
    "llm": OpenAIResearchSource,
}


@dataclass
class RunResult:
    window: Window
    upcoming: list[Event]
    past: list[Event]
    review: list[Event]
    changes: list[Change]
    source_report: dict[str, str] = field(default_factory=dict)


def default_window(today: date, config: Config) -> Window:
    years_ahead = int(config.settings.get("window", {}).get("years_ahead", 1))
    return Window(today, date(today.year + years_ahead, 12, 31))


def source_enabled(name: str, config: Config) -> bool:
    if name == "llm":
        return OpenAIResearchSource.available(config)
    return bool(config.source_settings(name).get("enabled", True))


def normalise(candidates: list[Candidate], ctx: Context) -> list[Candidate]:
    out = []
    window = ctx.window
    for c in candidates:
        upcoming = c.period.end >= window.start or (c.second_round and c.second_round >= window.start)
        if not upcoming or c.period.start > window.end:
            continue
        if c.level not in ctx.config.scope("levels", ["national", "regional"]):
            continue
        country = ctx.config.country(c.country_code)
        if country is None or exclusion_reason(c.title, ctx.config) or not_an_election(c.title, country):
            continue
        if ctx.config.mentions_excluded_territory(f"{c.title} {c.region}"):
            continue
        if c.indirect and not ctx.config.scope("include_indirect", True):
            continue
        if c.level == "regional":
            if ctx.config.is_excluded_territory(c.region):
                continue
            if not c.region or is_aggregate_territory(c.region):
                c.aggregate = True
                c.region = c.region if c.region.lower().startswith("all") else f"All {c.region or 'regions'}".strip()
            elif not c.region_id:
                match = ctx.regions.lookup(c.country_code, c.region)
                if match is None and ctx.regions.can_search:
                    match = ctx.regions.resolve_unknown(country, c.region)
                    if match is None:
                        log.info("dropping %s (%s): %r is not a first-level region", c.title, c.source, c.region)
                        continue
                if match:
                    c.region_id, c.region = match
        out.append(c)
    return out


def run(
    config: Config,
    today: date | None = None,
    window_end: date | None = None,
    only_sources: list[str] | None = None,
    cache_dir: str | Path | None = None,
    dry_run: bool = False,
) -> RunResult:
    today = today or date.today()
    window = default_window(today, config)
    if window_end:
        window = Window(today, window_end)
    log.info("Window: %s .. %s", window.start, window.end)

    ctx = Context.build(config, HttpClient(cache_dir=cache_dir), window)
    candidates: list[Candidate] = []
    report: dict[str, str] = {}
    for name, cls in SOURCES.items():
        if only_sources and name not in only_sources:
            continue
        if not source_enabled(name, config):
            report[name] = "disabled" if name != "llm" else "disabled (set OPENAI_API_KEY to enable)"
            continue
        try:
            found = cls(ctx).fetch()
        except Exception as exc:  # isolate failures: other sources still count
            log.exception("source %s failed", name)
            report[name] = f"FAILED: {exc}"
            continue
        report[name] = f"{len(found)} candidates"
        log.info("%s: %d candidates", name, len(found))
        candidates += found

    succeeded = [n for n, r in report.items() if r.endswith("candidates")]
    if not succeeded:
        raise RuntimeError(f"every source failed or is disabled, calendar left untouched: {report}")

    candidates = normalise(candidates, ctx)
    fresh = [e for e in build_events(candidates, config) if in_window(e, window.start, window.end)]
    cross_check_with_llm(fresh)

    calendar_path = config.output_path("calendar_csv")
    previous = store.read_calendar(calendar_path)
    keep, review = split_by_confidence(fresh)
    # Weak evidence for something already in the calendar still counts as "seen".
    corroborating = [
        r for r in review
        if any((r.wikidata_id and r.wikidata_id == p.wikidata_id)
               or (same_identity(p, r) and p.period.overlaps(r.period, SAME_ELECTION_SLACK_DAYS))
               for p in previous)
    ]
    review = [r for r in review if r not in corroborating]
    stale_days = int(config.settings.get("window", {}).get("stale_after_days", 21))
    events, changes = reconcile(previous, keep + corroborating, today, stale_days)

    on_hold = [e for e in events if e.status == "unconfirmed" or (e.status != "held" and e.confidence == "low")]
    upcoming = [e for e in events if e.status != "held" and e not in on_hold]
    past = [e for e in events if e.status == "held"]
    review += on_hold
    result = RunResult(window, upcoming, past, review, changes, report)
    if not dry_run:
        write_outputs(config, result, events, today)
    return result


def write_outputs(config: Config, result: RunResult, events: list[Event], today: date) -> None:
    store.write_calendar(config.output_path("calendar_csv"), events)
    # Rows of the calendar itself stay in the calendar CSV; this file lists
    # the weak candidates that never made it in.
    store.write_review(config.output_path("review_csv"), [e for e in result.review if not e.event_id])
    changes_path = config.output_path("changes_csv")
    store.append_changes(changes_path, result.changes)
    about = [
        ("Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        ("Run date", today.isoformat()),
        ("Window", f"{result.window.start.isoformat()} to {result.window.end.isoformat()}"),
        ("Scope", "EU-27; national (parliament, president, upper house, European Parliament, "
                  "indirect elections of head of state/upper house) and regional (first-level "
                  "subdivisions). Local elections, referendums and by-elections are excluded."),
        *[(f"Source: {name}", status) for name, status in result.source_report.items()],
        ("Upcoming events", str(len(result.upcoming))),
        ("Changes this run", str(len(result.changes))),
        ("Status", "confirmed = exact date backed by two sources or an official 'confirmed' listing; "
                   "expected = exact date from one source; estimated = only a period is known "
                   "(see 'As announced' and 'Date precision'); held = the date has passed; "
                   "unconfirmed = no source has reported it for several weeks (sheet 'Needs review')."),
        ("Confidence", "high = two or more independent sources, or one authoritative listing; "
                       "medium = one source; low = weak evidence only (sheet 'Needs review')."),
        ("Dates", "'Date / from' and 'To' bound the period the election is expected in; "
                  "they are equal when the day is known (multi-day polls span their voting days)."),
    ]
    store.write_workbook(
        config.output_path("calendar_xlsx"), result.upcoming, result.past, result.review,
        store.read_changes(changes_path), about,
    )
