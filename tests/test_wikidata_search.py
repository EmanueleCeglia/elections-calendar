"""The Wikidata source queries country by country and isolates failures."""

from datetime import date
from types import SimpleNamespace

import pytest

from elections_calendar.config import Config
from elections_calendar.sources.base import Window
from elections_calendar.sources.wikidata_source import WikidataSource


class FakeWikidata:
    def __init__(self, failing=()):
        self.failing = set(failing)
        self.queried = []

    def dated_elections(self, qids, start, end, retries=None):
        self.queried.append(qids[0])
        if qids[0] in self.failing:
            raise TimeoutError("504 Gateway Timeout")
        return [f"E-{qids[0]}"]


class FakeResolver:
    def __init__(self, resolved=None):
        self.resolved = resolved or {}
        self.asked = []

    def resolve(self, qids):
        self.asked += list(qids)
        return {}


def source(wd, resolver=None):
    config = Config()
    ctx = SimpleNamespace(config=config, window=Window(date(2026, 10, 12), date(2027, 12, 31)),
                          wikidata=wd, resolver=resolver or FakeResolver())
    return WikidataSource(ctx), config


def test_one_query_per_country_with_aliases():
    wd = FakeWikidata()
    src, config = source(wd)
    qids, failed = src.search()
    assert len(wd.queried) == len(config.countries) == 27
    assert failed == [] and len(qids) == 27


def test_a_failing_country_does_not_stop_the_others():
    config = Config()
    wd = FakeWikidata(failing={config.country("DE").wikidata})
    src, _ = source(wd)
    qids, failed = src.search()
    assert failed == ["DE"]
    assert len(qids) == 26 and len(wd.queried) == 27


def test_service_down_stops_after_three_failures_in_a_row():
    config = Config()
    wd = FakeWikidata(failing={c.wikidata for c in config.countries.values()})
    src, _ = source(wd, FakeResolver(resolved={"Q1": object()}))
    qids, failed = src.search()
    assert len(wd.queried) == 3          # gave up after three failures in a row
    assert len(failed) == 27 and qids == []


def test_everything_failing_with_nothing_else_found_raises():
    config = Config()
    wd = FakeWikidata(failing={c.wikidata for c in config.countries.values()})
    src, _ = source(wd)
    with pytest.raises(RuntimeError):
        src.fetch()


def test_everything_failing_falls_back_to_items_found_via_wikipedia():
    config = Config()
    wd = FakeWikidata(failing={c.wikidata for c in config.countries.values()})
    src, _ = source(wd, FakeResolver(resolved={"Q1": SimpleNamespace()}))
    src.candidate = lambda election: None
    assert src.fetch() == []
