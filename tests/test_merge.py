"""Merging candidates into events and updating the calendar across runs."""

from datetime import date

import pytest

from elections_calendar.config import Config
from elections_calendar.dates import parse_period
from elections_calendar.merge import build_events, reconcile
from elections_calendar.models import Candidate, Period


@pytest.fixture(scope="module")
def config():
    return Config()


def cand(source, period, code="DE", level="national", etype="presidential", region="", **kw):
    if isinstance(period, str):
        period = parse_period(period, 2027)
    return Candidate(source=source, country_code=code, level=level, election_type=etype,
                     period=period, region=region, title=kw.pop("title", f"{code} {etype} {region}"), **kw)


def day(y, m, d):
    return Period(date(y, m, d), date(y, m, d), "day", f"{y}-{m:02d}-{d:02d}")


# ------------------------------------------------------------ build_events
def test_sources_narrow_each_other(config):
    events = build_events([
        cand("wikipedia", "By 7 March", code="EE", etype="parliamentary"),
        cand("wikipedia", "March", code="EE", etype="parliamentary"),
        cand("wikidata", day(2027, 3, 7), code="EE", etype="parliamentary"),
        cand("cor_pdf", "7 March 2027", code="EE", etype="parliamentary"),
    ], config)
    assert len(events) == 1
    ev = events[0]
    assert (ev.date_start, ev.date_end, ev.date_precision) == (date(2027, 3, 7), date(2027, 3, 7), "day")
    assert ev.sources == ["cor_pdf", "wikidata", "wikipedia"]
    assert ev.confidence == "high"


def test_a_deadline_echoed_as_a_date_by_one_source_stays_a_deadline(config):
    ev = build_events([
        cand("wikipedia", "28 September", code="SK", etype="parliamentary"),
        cand("wikipedia", "By 28 September", code="SK", etype="parliamentary"),
        cand("cor_pdf", "2027", code="SK", etype="parliamentary"),
    ], config)[0]
    assert (ev.date_precision, ev.date_end, ev.status) == ("deadline", date(2027, 9, 28), "estimated")


def test_low_confidence_evidence_does_not_set_the_date(config):
    ev = build_events([
        cand("wikipedia", "By 25 July", code="GR", etype="parliamentary"),
        cand("wikidata", day(2027, 6, 30), code="GR", etype="parliamentary", confidence="low"),
    ], config)[0]
    assert (ev.date_end, ev.date_precision) == (date(2027, 7, 25), "deadline")
    assert ev.sources == ["wikidata", "wikipedia"]


def test_conflicting_lower_priority_date_is_noted_not_applied(config):
    events = build_events([
        cand("wikipedia", day(2027, 4, 18), code="FR"),
        cand("llm", day(2027, 4, 25), code="FR"),
    ], config)
    assert len(events) == 1
    assert events[0].date_start == date(2027, 4, 18)
    assert "llm says" in events[0].notes


def test_status(config):
    one = build_events([cand("wikipedia", day(2027, 4, 18))], config)[0]
    two = build_events([cand("wikipedia", day(2027, 4, 18)), cand("wikidata", day(2027, 4, 18))], config)[0]
    vague = build_events([cand("wikipedia", "Autumn 2027")], config)[0]
    official = build_events([cand("cor_pdf", day(2027, 4, 18), confidence="high")], config)[0]
    assert (one.status, two.status, vague.status, official.status) == ("expected", "confirmed", "estimated", "confirmed")


def test_regions_are_kept_apart_and_matched_by_name(config):
    events = build_events([
        cand("wikipedia", day(2027, 4, 18), level="regional", etype="regional", region="Saarland", region_id="Q1201"),
        cand("wikipedia", day(2027, 4, 18), level="regional", etype="regional", region="Schleswig-Holstein", region_id="Q1194"),
        cand("cor_pdf", "18 April 2027", level="regional", etype="regional", region="Saarland"),
    ], config)
    assert sorted((e.region, len(e.sources)) for e in events) == [("Saarland", 2), ("Schleswig-Holstein", 1)]


def test_aggregate_dropped_when_regions_are_listed(config):
    events = build_events([
        cand("wikidata", day(2027, 5, 23), code="ES", level="regional", etype="regional", region="All regions", aggregate=True),
        cand("wikipedia", day(2027, 5, 23), code="ES", level="regional", etype="regional", region="Asturias"),
        cand("wikipedia", day(2027, 3, 17), code="NL", level="regional", etype="regional", region="All provinces", aggregate=True),
    ], config)
    assert sorted((e.country_code, e.region) for e in events) == [("ES", "Asturias"), ("NL", "All provinces")]


def test_llm_only_regions_fold_into_a_nationwide_round(config):
    reg = dict(code="SK", level="regional", etype="regional")
    events = build_events([
        cand("wikipedia", day(2026, 10, 24), region="All regions", aggregate=True, **reg),
        cand("cor_pdf", day(2026, 10, 24), region="All 8 self-governing regions", aggregate=True, **reg),
        cand("llm", day(2026, 10, 24), region="Nitra Region", **reg),
        cand("llm", day(2026, 10, 24), region="Košice Region", **reg),
    ], config)
    assert len(events) == 1 and events[0].aggregate
    assert "Regions: Košice Region, Nitra Region" in events[0].notes


def test_same_wikidata_item_always_merges(config):
    events = build_events([
        cand("wikipedia", "2027", code="IT", etype="parliamentary", wikidata_id="Q1"),
        cand("wikidata", "September 2027", code="IT", etype="parliamentary", wikidata_id="Q1"),
    ], config)
    assert len(events) == 1 and events[0].date_precision == "month"


def test_one_source_with_two_far_apart_dates_means_two_elections(config):
    events = build_events([
        cand("wikipedia", day(2027, 1, 24), code="BG", etype="parliamentary"),
        cand("wikipedia", day(2027, 10, 10), code="BG", etype="parliamentary"),
    ], config)
    assert len(events) == 2


# --------------------------------------------------------------- reconcile
RUN1, RUN2 = date(2026, 10, 12), date(2026, 10, 19)


def run(config, previous, candidates, run_date):
    return reconcile(previous, build_events(candidates, config), run_date)


def test_first_run_creates_then_refines(config):
    events, changes = run(config, [], [cand("wikipedia", "Autumn 2027", code="AT", level="regional",
                                            etype="regional", region="Tyrol")], RUN1)
    assert [c.change for c in changes] == ["added"]
    ev = events[0]
    assert ev.event_id == "AT-REG-tyrol-2027"
    assert ev.status == "estimated" and ev.first_seen == RUN1

    events, changes = run(config, events, [cand("cor_pdf", "26 September 2027", code="AT", level="regional",
                                                etype="regional", region="Tyrol")], RUN2)
    assert [c.change for c in changes if c.field == "date"] == ["refined"]
    ev = events[0]
    assert ev.event_id == "AT-REG-tyrol-2027"  # same row, updated in place
    assert (ev.date_start, ev.date_precision, ev.status) == (date(2027, 9, 26), "day", "expected")
    assert ev.first_seen == RUN1 and ev.last_updated == RUN2


def test_vaguer_information_does_not_overwrite_a_known_date(config):
    events, _ = run(config, [], [cand("wikipedia", day(2027, 4, 18), code="FR")], RUN1)
    events, changes = run(config, events, [cand("cor_pdf", "2027", code="FR")], RUN2)
    assert changes == []
    assert events[0].date_start == date(2027, 4, 18) and events[0].date_precision == "day"


def test_reschedule_is_detected(config):
    events, _ = run(config, [], [cand("wikipedia", day(2027, 9, 26), code="SK", etype="parliamentary")], RUN1)
    events, changes = run(config, events, [cand("wikipedia", day(2027, 1, 24), code="SK", etype="parliamentary")], RUN2)
    assert len(events) == 1
    assert [c.change for c in changes] == ["rescheduled"]
    assert events[0].date_start == date(2027, 1, 24)


def test_new_election_is_added_next_to_existing_ones(config):
    events, _ = run(config, [], [cand("wikipedia", day(2027, 4, 18), code="FR")], RUN1)
    events, changes = run(config, events, [
        cand("wikipedia", day(2027, 4, 18), code="FR"),
        cand("wikipedia", day(2027, 6, 13), code="FR", etype="parliamentary"),
    ], RUN2)
    assert [c.change for c in changes] == ["added"]
    assert len(events) == 2


def test_events_not_reported_any_more_are_kept_but_flagged(config):
    events, _ = run(config, [], [cand("wikipedia", day(2027, 4, 18), code="FR")], RUN1)
    events, changes = run(config, events, [cand("wikipedia", day(2027, 1, 30))], RUN2)
    fr = next(e for e in events if e.country_code == "FR")
    assert not fr.seen_in_last_run and fr.last_seen == RUN1


def test_past_events_become_held(config):
    events, _ = run(config, [], [cand("wikipedia", day(2026, 10, 25), code="BG")], RUN1)
    events, changes = run(config, events, [], date(2026, 11, 2))
    assert events[0].status == "held"
    assert [c.change for c in changes] == ["held"]


def test_run_off_keeps_event_alive(config):
    events, _ = run(config, [], [cand("wikipedia", Period(date(2026, 10, 9), date(2026, 10, 10), "day"),
                                      code="CZ", etype="upper_house", second_round=date(2026, 10, 16))], RUN1)
    events, changes = run(config, events, [], date(2026, 10, 13))
    assert events[0].status != "held"


def test_events_no_source_reports_for_weeks_become_unconfirmed(config):
    events, _ = run(config, [], [cand("cor_pdf", day(2027, 5, 23), code="ES", level="regional",
                                      etype="regional", region="Aragon")], RUN1)
    events, changes = run(config, events, [], date(2026, 11, 9))
    assert events[0].status == "unconfirmed"
    assert [c.change for c in changes] == ["unconfirmed"]


def test_llm_cross_check_downgrades_single_source_events(config):
    from elections_calendar.merge import cross_check_with_llm

    reg = dict(code="ES", level="regional", etype="regional")
    events = build_events([
        cand("cor_pdf", day(2027, 5, 23), region="Aragon", **reg),
        cand("cor_pdf", day(2027, 5, 23), region="Asturias", **reg),
        cand("llm", day(2027, 5, 23), region="Asturias", **reg),
    ], config)
    cross_check_with_llm(events)
    by_region = {e.region: e.confidence for e in events}
    assert by_region == {"Aragon": "low", "Asturias": "high"}


def test_overlapping_answer_from_the_same_sources_is_followed(config):
    nov = Period(date(2027, 11, 1), date(2027, 11, 30), "month", "November 2027")
    wider = Period(date(2027, 10, 12), date(2027, 11, 27), "range", "12 October-27 November")
    events, _ = run(config, [], [cand("llm", nov, code="SI", etype="upper_house")], RUN1)
    events, changes = run(config, events, [cand("llm", wider, code="SI", etype="upper_house")], RUN2)
    assert [c.change for c in changes] == ["revised"]
    assert events[0].date_text == "12 October-27 November"


def test_narrow_date_without_support_is_revised(config):
    sept = Period(date(2027, 9, 1), date(2027, 9, 30), "month", "September 2027")
    first = [cand("cor_pdf", "2027", code="AT", level="regional", etype="regional", region="Tyrol"),
             cand("llm", sept, code="AT", level="regional", etype="regional", region="Tyrol")]
    events, _ = run(config, [], first, RUN1)
    assert events[0].date_text == "September 2027"
    # The LLM did not run: keep the better date.
    events, changes = run(config, events, first[:1], RUN2)
    assert changes == [] and events[0].date_text == "September 2027"
    # Both sources ran and now only support "autumn": follow them.
    autumn = cand("llm", "Autumn 2027", code="AT", level="regional", etype="regional", region="Tyrol")
    events, changes = run(config, events, [first[0], autumn], date(2026, 10, 26))
    assert [c.change for c in changes] == ["revised"] and events[0].date_precision == "season"


def test_regional_events_are_never_indirect(config):
    ev = build_events([cand("llm", "2027", code="AT", level="regional", etype="regional",
                            region="Tyrol", indirect=True)], config)[0]
    assert ev.indirect is False


def test_estimates_do_not_narrow_other_sources(config):
    ev = build_events([
        cand("wikipedia", "By 22 December", code="IT", etype="parliamentary"),
        cand("llm", "October 2027", code="IT", etype="parliamentary", narrows=False),
    ], config)[0]
    assert ev.date_text == "By 22 December"
    alone = build_events([cand("llm", "October 2027", code="IT", etype="parliamentary", narrows=False)], config)[0]
    assert alone.date_precision == "month"


def test_small_shifts_of_estimated_windows_are_ignored(config):
    a = Period(date(2027, 10, 17), date(2027, 11, 11), "range", "17 October-11 November")
    b = Period(date(2027, 10, 14), date(2027, 11, 11), "range", "14 October-11 November")
    events, _ = run(config, [], [cand("llm", a, code="PL", etype="parliamentary")], RUN1)
    events, changes = run(config, events, [cand("llm", b, code="PL", etype="parliamentary")], RUN2)
    assert changes == []
