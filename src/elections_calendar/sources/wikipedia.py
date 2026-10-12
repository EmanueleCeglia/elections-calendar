"""English Wikipedia's yearly election overviews.

Pages (configurable, ``{year}`` = every year touched by the window):

* "YYYY national electoral calendar"   - month sections of bullet lists
* "YYYY local electoral calendar"      - same layout, sub-national elections
* "YYYY elections in the European Union" - tables per member state + lists

All pages of a run are downloaded in a single API request. Each bullet/table
row yields *mentions* (a date text + linked election articles); the linked
articles are resolved to Wikidata items, which tell us country, region and
kind of election. Dates come from the page itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..classify import election_type, exclusion_reason, is_indirect, looks_regional
from ..config import Country
from ..dates import parse_period, year_period, SECOND_ROUND_RE
from ..models import Candidate, Period
from ..wikidata import WIKIPEDIA_API
from .base import Source, candidate_from_resolved

LINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]*))?\]\]")
HEADING_RE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$")
BULLET_RE = re.compile(r"^(\*+)\s*(.*)$")
DATE_PREFIX_RE = re.compile(r"^(?P<date>[^\[\]:]{1,80}?)\s*:\s*(?P<rest>.*)$")
ELECTION_TARGET_RE = re.compile(r"election|referend|convention|electoral college", re.I)
NOT_ELECTION_TARGET_RE = re.compile(r"^(elections|politics|electoral system|electoral calendar) (in|of)\b|calendar", re.I)
SKIP_SECTION_RE = re.compile(r"see also|references|notes|external links|by-elections|further reading|sources", re.I)
DATE_HINT_RE = re.compile(
    r"\d|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\bby\b|spring|summer|autumn|fall|winter|tbd",
    re.I,
)


@dataclass
class Link:
    target: str
    label: str

    @property
    def is_election(self) -> bool:
        return bool(ELECTION_TARGET_RE.search(self.target)) and not NOT_ELECTION_TARGET_RE.search(self.target)


@dataclass
class Mention:
    page: str
    year: int
    sections: tuple[str, ...]
    date_text: str
    article: str | None
    label: str
    text: str
    country: Country | None = None
    indirect_section: bool = False
    table_heading: str = ""
    second_round: bool = False

    @property
    def section_text(self) -> str:
        return " / ".join(self.sections)


# ----------------------------------------------------------------- cleaning
def clean_wikitext(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>/]*/>", "", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    text = re.sub(r"<nowiki\s*/>", "", text)
    # Drop templates from the innermost outwards ({{efn|..}}, {{Party name..}}).
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"'''?", "", text)
    return text


def links_of(text: str) -> list[Link]:
    out = []
    for m in LINK_RE.finditer(text):
        target = m[1].split("#", 1)[0].strip()
        if not target or re.match(r"^(file|image|category|:?[a-z]{2,3}):", target, re.I):
            continue
        out.append(Link(target, (m[2] or m[1]).strip()))
    return out


def plain(text: str) -> str:
    text = LINK_RE.sub(lambda m: m[2] if m[2] is not None else m[1], text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_cell_attributes(cell: str) -> str:
    # `colspan="2" |Party` -> `Party` (only when the left part is attributes)
    depth, idx = 0, None
    for i, ch in enumerate(cell):
        if cell.startswith("[[", i):
            depth += 1
        elif cell.startswith("]]", i):
            depth -= 1
        elif ch == "|" and depth == 0:
            idx = i
            break
    if idx is not None and "=" in cell[:idx]:
        return cell[idx + 1 :].strip()
    return cell.strip()


# ------------------------------------------------------------------ parsing
class PageParser:
    def __init__(self, page: str, wikitext: str, find_country) -> None:
        self.page = page
        m = re.match(r"^(\d{4})", page)
        self.year = int(m[1]) if m else date.today().year
        self.text = clean_wikitext(wikitext)
        self.find_country = find_country
        self.mentions: list[Mention] = []

    def parse(self) -> list[Mention]:
        sections: list[tuple[int, str]] = []
        bullet_stack: list[tuple[int, str, Country | None]] = []  # depth, date, country
        table_lines: list[str] | None = None

        for raw in self.text.splitlines():
            line = raw.rstrip()
            if table_lines is not None:
                if line.startswith("|}"):
                    self._parse_table(table_lines, sections)
                    table_lines = None
                else:
                    table_lines.append(line)
                continue
            if line.startswith("{|"):
                table_lines = []
                continue
            h = HEADING_RE.match(line)
            if h:
                level = len(h[1])
                sections = [s for s in sections if s[0] < level] + [(level, plain(h[2]))]
                bullet_stack = []
                continue
            section_names = tuple(s[1] for s in sections)
            if any(SKIP_SECTION_RE.search(s) for s in section_names):
                continue
            b = BULLET_RE.match(line)
            if not b:
                continue
            depth, content = len(b[1]), b[2].strip()
            bullet_stack = [x for x in bullet_stack if x[0] < depth]
            inherited_date = next((x[1] for x in reversed(bullet_stack) if x[1]), "")
            inherited_country = next((x[2] for x in reversed(bullet_stack) if x[2]), None)

            date_text = ""
            m = DATE_PREFIX_RE.match(content)
            if m and DATE_HINT_RE.search(m["date"]) and parse_period(m["date"], self.year):
                date_text, content = m["date"].strip(), m["rest"].strip()
            country = self._country_of(content) or inherited_country or self._section_country(section_names)
            if country is None and self._foreign(content):
                bullet_stack.append((depth, date_text or inherited_date, None))
                continue
            line_date = date_text or inherited_date
            if content:
                # "* [[2027 Spanish regional elections]], 23 May" dates its sub-items too
                line_date = self._emit_line(content, line_date, country, section_names) or line_date
            bullet_stack.append((depth, line_date, country))
        return self.mentions

    def _section_country(self, sections: tuple[str, ...]) -> Country | None:
        for s in reversed(sections):
            c = self.find_country(s)
            if c:
                return c
        return None

    def _foreign(self, content: str) -> bool:
        """The line names a country ("Elections in X") that is not an EU member."""
        return any(
            re.match(r"^(Elections|Politics) (in|of) ", link.target) and not self.find_country(link.target)
            for link in links_of(content)
        )

    def _country_of(self, content: str) -> Country | None:
        for link in links_of(content):
            if link.is_election:
                continue
            for candidate in (link.label, re.sub(r"^(Elections|Politics) (in|of) (the )?", "", link.target)):
                c = self.find_country(candidate)
                if c:
                    return c
        return None

    def _emit_line(self, content: str, date_text: str, country, sections, table_heading: str = "") -> str:
        """One mention per election article; a date after a link belongs to it.

        Returns the line's date when all its elections share one (for sub-items).
        """
        indirect_section = any(re.search(r"indirect", s, re.I) for s in sections)
        pieces = re.split(r"(\[\[[^\[\]]+\]\])", content)
        election_links: list[tuple[Link, str]] = []  # link, trailing text
        for i, piece in enumerate(pieces):
            if piece.startswith("[[") and piece.endswith("]]"):
                links = links_of(piece)
                if links and links[0].is_election:
                    trailing = ""
                    for nxt in pieces[i + 1 :]:
                        if nxt.startswith("[["):
                            ls = links_of(nxt)
                            if ls and ls[0].is_election:
                                break
                            continue
                        trailing += nxt
                    election_links.append((links[0], trailing))
        text = plain(content)
        if not election_links:
            if country:
                self.mentions.append(
                    Mention(self.page, self.year, sections, date_text, None, text, text, country,
                            indirect_section, table_heading, bool(SECOND_ROUND_RE.search(text)))
                )
            return date_text
        dates = []
        for link, trailing in election_links:
            own_date = date_text or self._trailing_date(trailing)
            dates.append(own_date)
            self.mentions.append(
                Mention(
                    self.page, self.year, sections, own_date, link.target, link.label, text, country,
                    indirect_section, table_heading,
                    bool(SECOND_ROUND_RE.search(link.label)),
                )
            )
        return dates[0] if len(set(dates)) == 1 else date_text

    def _trailing_date(self, trailing: str) -> str:
        for segment in re.split(r"[,;]", plain(trailing)):
            segment = segment.strip(" .")
            if segment and DATE_HINT_RE.search(segment) and parse_period(segment, self.year):
                return segment
        return ""

    def _parse_table(self, lines: list[str], sections) -> None:
        section_names = tuple(s[1] for s in sections)
        if any(SKIP_SECTION_RE.search(s) for s in section_names):
            return
        header: list[str] = []
        rows: list[list[str]] = []
        current: list[str] = []
        for line in lines:
            if line.startswith("|-"):
                if current:
                    rows.append(current)
                current = []
            elif line.startswith("!"):
                header += [_strip_cell_attributes(c) for c in re.split(r"!!", line[1:])]
            elif line.startswith("|+"):
                continue
            elif line.startswith("|"):
                current += [_strip_cell_attributes(c) for c in re.split(r"\|\|", line[1:])]
            elif current:
                current[-1] += " " + line
        if current:
            rows.append(current)
        names = [plain(h).lower() for h in header]
        try:
            c_col = next(i for i, h in enumerate(names) if re.search(r"member state|country|state", h))
            e_col = next(i for i, h in enumerate(names) if re.search(r"election|date", h))
        except StopIteration:
            return
        heading = section_names[-1] if section_names else ""
        for row in rows:
            if len(row) <= max(c_col, e_col):
                continue
            country = self.find_country(plain(row[c_col]))
            if not country:
                continue
            cell = row[e_col]
            links = [l for l in links_of(cell) if l.is_election]
            if links:
                date_text = links[0].label if DATE_HINT_RE.search(links[0].label) else ""
                self.mentions.append(
                    Mention(self.page, self.year, section_names, date_text, links[0].target,
                            links[0].label, plain(cell), country, False, heading)
                )
            else:
                text = plain(cell)
                self.mentions.append(
                    Mention(self.page, self.year, section_names, text if DATE_HINT_RE.search(text) else "",
                            None, heading, text, country, False, heading)
                )


# ------------------------------------------------------------------- source
class WikipediaSource(Source):
    name = "wikipedia"

    def fetch(self) -> list[Candidate]:
        pages = [
            p.format(year=y)
            for y in self.ctx.window.years
            for p in self.config.source_settings("wikipedia").get("pages", [])
        ]
        texts = self.download(pages)
        mentions: list[Mention] = []
        for page, wikitext in texts.items():
            parsed = PageParser(page, wikitext, self.config.find_country_in_text).parse()
            self.log.info("%s: %d mentions", page, len(parsed))
            mentions += parsed
        return self.to_candidates(self._relevant(mentions))

    def download(self, pages: list[str]) -> dict[str, str]:
        data = self.ctx.http.get_json(
            WIKIPEDIA_API,
            params={
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": "|".join(pages),
                "redirects": "1",
                "format": "json",
                "formatversion": "2",
            },
            min_interval=3,
        )
        out = {}
        for page in data.get("query", {}).get("pages", []):
            if page.get("missing"):
                self.log.warning("page not found: %s", page.get("title"))
                continue
            out[page["title"]] = page["revisions"][0]["slots"]["main"]["content"]
        return out

    def _period(self, m: Mention) -> Period:
        p = parse_period(m.date_text, m.year) if m.date_text else None
        return p or year_period(m.year, f"{m.year} (date not given)")

    def _relevant(self, mentions: list[Mention]) -> list[Mention]:
        """Drop elections that are entirely over before the window starts.

        An article is kept when any of its mentions (first round or run-off)
        is undated or reaches into the window, so a first round held just
        before the window still comes with its upcoming run-off.
        """
        start = self.ctx.window.start

        def upcoming(m: Mention) -> bool:
            p = self._period(m)
            return p.end >= start or not m.date_text

        live_articles = {m.article for m in mentions if m.article and upcoming(m)}
        return [
            m
            for m in mentions
            if (m.article in live_articles) or (m.article is None and m.country and upcoming(m))
        ]

    def to_candidates(self, mentions: list[Mention]) -> list[Candidate]:
        titles = {m.article for m in mentions if m.article}
        qids = self.ctx.wikidata.titles_to_qids(titles) if titles else {}
        hints = {}
        for m in mentions:
            qid = qids.get(m.article.replace("_", " ")) if m.article else None
            if qid and m.country:
                hints.setdefault(qid, m.country)
        resolved = self.ctx.resolver.resolve(qids.values(), hints)

        out: list[Candidate] = []
        second_rounds: dict[str, date] = {}
        for m in mentions:
            qid = qids.get(m.article.replace("_", " ")) if m.article else None
            election = resolved.get(qid) if qid else None
            if election:
                parent = self.ctx.resolver.parent_election(election)
                election = parent or election
                self.ctx.register_region(election)
                period = self._period(m)
                if m.second_round:
                    if period.is_exact:
                        second_rounds[election.qid] = period.start
                    continue
                out.append(
                    candidate_from_resolved(
                        self.name,
                        election,
                        period,
                        confidence="high" if period.is_exact else "medium",
                        notes=f"{m.page}: {m.date_text or 'no date'}",
                    )
                )
            elif m.article and qid is None:
                cand = self._redlink_candidate(m)
                if cand:
                    out.append(cand)
            elif not m.article:
                cand = self._text_candidate(m)
                if cand:
                    out.append(cand)
        for c in out:
            if c.wikidata_id in second_rounds and second_rounds[c.wikidata_id] > c.period.end:
                c.second_round = second_rounds[c.wikidata_id]
        return out

    def _redlink_candidate(self, m: Mention) -> Candidate | None:
        """A linked article that exists neither on Wikipedia nor on Wikidata
        (e.g. "2027 Sicilian regional election"): classify from its title."""
        if m.country is None or m.second_round:
            return None
        title = m.article
        if exclusion_reason(f"{title} {m.label}", self.config):
            return None
        level = "regional" if looks_regional(title) else "national"
        region, region_id, aggregate = "", "", False
        plural = bool(re.search(r"\belections\b", title, re.I))
        names_country = self.config.find_country_in_text(title) is not None
        match = None if plural else self.ctx.regions.infer(m.country.code, [title])
        if match and (level == "regional" or not names_country):
            # "2027 Ålandic legislative election": a region, not the country
            level, (region_id, region) = "regional", match
        elif level == "regional":
            region, aggregate = "All regions", True
        etype = election_type(f"{title} {m.label}", m.country, level)
        period = self._period(m)
        self.log.info("article %r has no Wikidata item; classified from its title", title)
        return Candidate(
            source=self.name,
            country_code=m.country.code,
            level=level,
            election_type=etype,
            period=period,
            region=region,
            region_id=region_id,
            aggregate=aggregate,
            indirect=is_indirect(f"{title} {m.label}", m.country, etype) or m.indirect_section,
            title=title,
            url=f"https://en.wikipedia.org/wiki/{m.page.replace(' ', '_')}",
            confidence="high" if period.is_exact else "medium",
            notes=f"{m.page}: {m.date_text or 'no date'}",
        )

    def _text_candidate(self, m: Mention) -> Candidate | None:
        """Fallback for rows without a resolvable article (e.g. "Latvia | By May")."""
        if m.country is None or m.second_round:
            return None
        text = " ".join([m.label, m.text, m.table_heading, m.article or ""])
        if "local electoral calendar" in m.page.lower() or any(
            re.search(r"local|regional|municipal", sec, re.I) for sec in m.sections
        ):
            return None  # without an article we cannot tell region from municipality
        if exclusion_reason(text, self.config):
            return None
        etype = election_type(text, m.country, "national")
        indirect = is_indirect(text, m.country, etype) or m.indirect_section
        period = self._period(m)
        title = f"{period.start.year} {m.country.name} {etype.replace('_', ' ')} election"
        return Candidate(
            source=self.name,
            country_code=m.country.code,
            level="national",
            election_type=etype,
            period=period,
            indirect=indirect,
            title=title,
            url=f"https://en.wikipedia.org/wiki/{m.page.replace(' ', '_')}",
            confidence="medium",
            notes=f"{m.page}: {m.date_text or 'no date'} ({m.text})",
        )
