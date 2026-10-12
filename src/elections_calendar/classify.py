"""Keyword rules that map titles/descriptions to the calendar's vocabulary.

Sources describe elections in free text ("Senate (1st round)", "Landtag",
"state election in Saarland", "Federal presidential election - Federal
Convention"). These helpers decide whether a description is in scope and
which ``election_type`` / ``level`` it belongs to.
"""

from __future__ import annotations

import re

from .config import Config, Country

_EXCLUDE = {
    "referendum": re.compile(r"referend|plebiscit|ballot measure|popular vote on|people'?s initiative", re.I),
    "by_election": re.compile(r"by-?elections?\b|special elections?\b|supplementary election|partial by", re.I),
    "local": re.compile(
        r"municipal|\blocal (?:elections?|councils?|government)|\bmayor|city council|town council|"
        r"communal|\bparish|\bborough|kommunal|gemeinderat|direct election \(|district council|"
        r"local and regional elections in|council of the city|city (?:assembly|elections?)|"
        r"bezirksverordnet|b[uü]rgermeister|landrat|gemeindevertret|kreistag|stadtrat",
        re.I,
    ),
    "other": re.compile(
        r"primar(?:y|ies)\b|leadership election|party (?:congress|leader)|supreme leader|recall|"
        r"retention|speaker|prime minister election|chancellor election|investiture|"
        r"european commission|council of the european union|parteivorsitz|vorsitzwahl|urwahl|"
        r"\bdesignat|\bappoint|\bselection of\b|delegation to",
        re.I,
    ),
}

_REGIONAL = re.compile(
    r"\bstate elections?\b|\bregional\b|\bprovincial\b|\blandtag|\bb[uü]rgerschaft|abgeordnetenhaus|"
    r"autonomous communit|regional (?:parliament|assembly|council)|county assembl|county council|"
    r"\bterritorial (?:council|assembly|election)|legislative assembly of|lagting|\bcortes (?!generales)|"
    r"generalitat|\bkraj|\bvoivodeship|sejmik|wellbeing services count|"
    r"regional president|\bgovernor\b|provinciale staten|consiglio regionale",
    re.I,
)

# Words that describe a set of territories rather than name one.
_AGGREGATE_WORDS = {
    "all", "every", "the", "and", "of", "nationwide", "regional", "regions", "region", "provinces",
    "provincial", "counties", "county", "departments", "departmental", "states", "state", "lander",
    "länder", "voivodeships", "assemblies", "assembly", "councils", "council", "self", "governing",
    "self-governing", "autonomous", "communities", "wellbeing", "services", "excluding", "municipalities",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
}

_INDIRECT = re.compile(
    r"\bindirect|federal convention|bundesversammlung|electoral college|elected by (?:the )?"
    r"(?:parliament|saeima|riigikogu|national assembly|assembly|provincial states)",
    re.I,
)

_PRESIDENTIAL = re.compile(r"presiden", re.I)
_NOT_HEAD_OF_STATE = re.compile(
    r"regional president|president of the (?:region|regional|government|generalitat|junta|council)|"
    r"regional (?:chair|governor)|presidents? of (?:the )?(?:provinces|regions)",
    re.I,
)
_LOWER_HOUSE = re.compile(
    r"chamber of deputies|house of representatives|congress|\bsejm\b|national assembly|"
    r"\bparliament\b|assembly of the republic|riigikogu|general election|legislative election",
    re.I,
)
_EUROPEAN_PARLIAMENT = re.compile(r"european parliament", re.I)


def exclusion_reason(text: str, config: Config) -> str | None:
    """Why a description is out of scope (``None`` if it is in scope)."""
    for reason, pattern in _EXCLUDE.items():
        if pattern.search(text):
            if reason == "referendum" and config.scope("include_referendums", False):
                continue
            if reason == "by_election" and config.scope("include_by_elections", False):
                continue
            if reason == "local" and config.scope("include_local", False):
                continue
            return reason
    return None


def not_an_election(text: str, country: Country) -> bool:
    """Delegations such as Austria's Federal Council (named in config)."""
    return any(re.search(rf"\b{re.escape(b)}\b", text, re.I) for b in country.not_elected)


def looks_regional(text: str) -> bool:
    return bool(_REGIONAL.search(text))


def is_aggregate_territory(text: str) -> bool:
    """'12 provinces', 'Four regions', 'County assemblies (19 counties)', 'nationwide'..."""
    if not text:
        return False
    text = re.sub(r"\((?:excluding|except|without)[^)]*\)", " ", text.lower())  # "(excluding Prague)"
    words = re.findall(r"[a-zäöüß\-]+|\d+", text)
    return bool(words) and all(w.isdigit() or w in _AGGREGATE_WORDS for w in words)


def is_upper_house(text: str, country: Country) -> bool:
    names = [n.lower() for n in country.upper_house] + ["upper house"]
    low = text.lower()
    if not any(re.search(rf"\b{re.escape(n)}\b", low) for n in names):
        return False
    if country.concurrent_upper:
        return False
    # "Sejm and Senate", "Senate and Congress of Deputies": a general election.
    without_upper = low
    for n in names:
        without_upper = re.sub(rf"\b{re.escape(n)}\b", " ", without_upper)
    return not _LOWER_HOUSE.search(without_upper)


def election_type(text: str, country: Country, level: str) -> str:
    if _EUROPEAN_PARLIAMENT.search(text):
        return "european_parliament"
    if level == "regional":
        return "regional"
    if _PRESIDENTIAL.search(text) and not _NOT_HEAD_OF_STATE.search(text):
        return "presidential"
    if is_upper_house(text, country):
        return "upper_house"
    return "parliamentary"


def is_indirect(text: str, country: Country, etype: str) -> bool:
    return etype in country.indirect or bool(_INDIRECT.search(text))


def in_scope(level: str, indirect: bool, config: Config) -> bool:
    if level not in config.scope("levels", ["national", "regional"]):
        return False
    if indirect and not config.scope("include_indirect", True):
        return False
    return True
