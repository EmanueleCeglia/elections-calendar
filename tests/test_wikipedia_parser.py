from pathlib import Path

import pytest

from elections_calendar.config import Config
from elections_calendar.sources.wikipedia import PageParser, clean_wikitext

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def config():
    return Config()


def parse(name, page, config):
    return PageParser(page, (FIXTURES / name).read_text(), config.find_country_in_text).parse()


def by_article(mentions):
    return {m.article: m for m in mentions if m.article}


def test_clean_wikitext_drops_refs_and_templates():
    text = "a<ref name=x>{{cite web|url=u}}</ref> b{{efn|note {{nested}}}} <!-- c -->d"
    assert clean_wikitext(text) == "a b d"


def test_national_calendar(config):
    mentions = parse("national_calendar_2027.wikitext", "2027 national electoral calendar", config)
    m = by_article(mentions)
    # nested bullets inherit the parent's date
    assert m["2027 French presidential election"].date_text == "18 April"
    assert m["2027 French presidential election"].country.code == "FR"
    assert m["2027 Finnish parliamentary election"].date_text == "18 April"
    # month-only dates and the "Unknown date" section
    assert m["Next Estonian parliamentary election"].date_text == "March"
    assert m["Next Greek parliamentary election"].date_text == ""
    assert m["Next Greek parliamentary election"].country.code == "GR"
    # indirect elections section
    assert m["2027 German presidential election"].indirect_section
    # non-EU countries are skipped, "See also" is ignored
    assert "2027 Micronesian general election" not in m
    assert "2027 local electoral calendar" not in m


def test_run_off_mentions(config):
    mentions = parse("national_calendar_2026.wikitext", "2026 national electoral calendar", config)
    senate = [x for x in mentions if x.article == "2026 Czech Senate election"]
    assert [x.second_round for x in senate] == [False, True]
    assert senate[1].date_text == "16–17 October"
    assert not any(x.article == "2026 Serbian parliamentary election" for x in mentions)


def test_eu_page_tables_and_lists(config):
    mentions = parse("eu_elections_2027.wikitext", "2027 elections in the European Union", config)
    m = by_article(mentions)
    assert m["Next Estonian parliamentary election"].date_text == "By 7 March"
    assert m["Next Greek parliamentary election"].table_heading == "Parliamentary elections"
    latvia = [x for x in mentions if x.country and x.country.code == "LV"][0]
    assert latvia.article is None and latvia.date_text == "By May"
    # a date after a link belongs to that link only
    assert m["2027 Dutch provincial elections"].date_text == "17 March"
    assert m["2027 Dutch Senate election"].date_text == ""
    assert m["2027 Ålandic legislative election"].date_text == "by October"
    assert m["2027 Ålandic legislative election"].country.code == "FI"
    # nested items inherit the date and the country of the section
    assert m["2027 Asturian regional election"].date_text == "23 May"
    assert m["Next Balearic regional election"].country.code == "ES"
