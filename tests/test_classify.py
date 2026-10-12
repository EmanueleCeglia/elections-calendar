import pytest

from elections_calendar.classify import (
    election_type,
    exclusion_reason,
    is_aggregate_territory,
    is_indirect,
)
from elections_calendar.config import Config
from elections_calendar.textutil import same_region_name


@pytest.fixture(scope="module")
def config():
    return Config()


@pytest.mark.parametrize(
    "text, code, expected",
    [
        ("2027 French presidential election | President", "FR", "presidential"),
        ("2026 Czech Senate election | Senate (1st round)", "CZ", "upper_house"),
        ("2027 Dutch Senate election", "NL", "upper_house"),
        ("Next Polish parliamentary election | Sejm and Senate", "PL", "parliamentary"),
        ("2027 Italian Senate election", "IT", "parliamentary"),  # always elected with the Chamber
        ("2026 Spanish general election | Senate and Congress of Deputies", "ES", "parliamentary"),
        ("National Council election", "SK", "parliamentary"),
        ("2027 Slovenian National Council election", "SI", "upper_house"),
        ("Regional assembly and regional president elections", "SK", "parliamentary"),
    ],
)
def test_election_type(config, text, code, expected):
    assert election_type(text, config.country(code), "national") == expected


def test_regional_level_type(config):
    assert election_type("Landtag election", config.country("DE"), "regional") == "regional"


@pytest.mark.parametrize(
    "text, reason",
    [
        ("2027 Spanish local elections", "local"),
        ("2027 Barcelona municipal election", "local"),
        ("2026 Seanad by-election", "by_election"),
        ("2026 Slovenian referendums", "referendum"),
        ("2027 Saarland state election", None),
        ("2027 German presidential election", None),
        ("2027 indirect selection of Spanish senators by autonomous-community parliaments", "other"),
    ],
)
def test_exclusions(config, text, reason):
    assert exclusion_reason(text, config) == reason


def test_indirect(config):
    assert is_indirect("2027 German presidential election", config.country("DE"), "presidential")
    assert is_indirect("Election of the President by the Saeima", config.country("LV"), "presidential")
    assert not is_indirect("2027 French presidential election", config.country("FR"), "presidential")
    assert is_indirect("Federal Convention", config.country("AT"), "presidential")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("12 provinces", True), ("Four regions", True), ("13 regions (excluding Prague)", True),
        ("County assemblies (19 counties)", True), ("Saarland", False), ("Bavaria and Hesse", False),
    ],
)
def test_aggregate_territory(text, expected):
    assert is_aggregate_territory(text) == expected


def test_region_names():
    assert same_region_name("Community of Madrid", "Madrid")
    assert same_region_name("Castile-La Mancha", "Castilla–La Mancha")
    assert same_region_name("Valencian Community", "Comunitat Valenciana")
    assert not same_region_name("Saxony", "Lower Saxony")
    assert not same_region_name("Upper Austria", "Lower Austria")


def test_country_lookup(config):
    assert config.country_by_name("Czechia").code == "CZ"
    assert config.country_by_name("Elections in the Netherlands").code == "NL"
    assert config.find_country_in_text("Next Greek parliamentary election").code == "GR"
    assert config.country_by_qid("Q29999").code == "NL"


def test_split_territories():
    from elections_calendar.sources.cor_pdf import split_territories

    known = {"Andalusia", "Castile and León"}.__contains__
    assert split_territories("Aragon, Asturias and La Rioja") == ["Aragon", "Asturias", "La Rioja"]
    assert split_territories("Andalusia and Castile and León", known) == ["Andalusia", "Castile and León"]
    assert split_territories("13 regions (excluding Prague)") == ["13 regions"]
