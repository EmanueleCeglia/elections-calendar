"""Wikidata access and the logic that turns an election item into calendar terms.

Every Wikipedia election article has a Wikidata item, so both Wikipedia-based
and Wikidata-based candidates are classified the same way here:

* country      - P17, mapped to the EU-27 config
* jurisdiction - P1001; when it is a first-level subdivision (its P131 is the
                 country itself) the election is *regional*; when it sits
                 deeper (municipality, district...) it is *local* and dropped
* kind         - labels of P31 classes + item label + Wikipedia title
* dates        - P585 values with their precision (day / month / year)
* part of      - P361, used to fold "Italian Senate election" into the
                 "Italian general election" it belongs to
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from . import classify
from .config import Config, Country
from .dates import last_day
from .http import HttpClient
from .models import Period
from .textutil import normalize, region_tokens, same_region_name, similar

log = logging.getLogger(__name__)

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

ELECTION_CLASS = "Q40231"
# Languages used for region labels/aliases, so names written in English,
# German, Spanish... by different sources resolve to the same region.
LABEL_LANGUAGES = (
    "en|de|fr|es|it|nl|pt|pl|sv|fi|cs|sk|sl|hr|hu|ro|bg|el|da|et|lv|lt|ca|eu|gl|ga|mt|mul"
)


@dataclass
class Entity:
    qid: str
    labels: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    claims: dict[str, list] = field(default_factory=dict)
    enwiki: str = ""
    complete: bool = True  # fetched with claims and sitelinks

    def label(self) -> str:
        for lang in ("en", "mul"):
            if lang in self.labels:
                return self.labels[lang]
        return next(iter(self.labels.values()), self.qid)

    def names(self) -> set[str]:
        out = set(self.labels.values())
        for values in self.aliases.values():
            out.update(values)
        return out

    def _statements(self, prop: str) -> list[dict]:
        statements = [s for s in self.claims.get(prop, []) if s.get("rank") != "deprecated"]
        preferred = [s for s in statements if s.get("rank") == "preferred"]
        return preferred or statements

    def ids(self, prop: str) -> list[str]:
        out = []
        for s in self._statements(prop):
            value = s.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(value, dict) and value.get("id"):
                out.append(value["id"])
        return out

    def times(self, prop: str) -> list[Period]:
        out = []
        for s in self.claims.get(prop, []):  # all ranks: rounds are often normal + preferred
            if s.get("rank") == "deprecated":
                continue
            value = s.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(value, dict) and "time" in value:
                p = time_to_period(value["time"], int(value.get("precision", 11)))
                if p:
                    out.append(p)
        return sorted(set(out), key=lambda p: p.sort_key())


def time_to_period(time_str: str, precision: int) -> Period | None:
    m = re.match(r"^\+?(\d{4})-(\d{2})-(\d{2})", time_str)
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    if precision >= 11 and mo and d:
        day = date(y, mo, d)
        return Period(day, day, "day", day.isoformat())
    if precision == 10 and mo:
        return Period(date(y, mo, 1), last_day(y, mo), "month", f"{y}-{mo:02d}")
    if precision == 9:
        return Period(date(y, 1, 1), date(y, 12, 31), "year", str(y))
    return None


class Wikidata:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._entities: dict[str, Entity] = {}

    # -------------------------------------------------------------- titles
    def titles_to_qids(self, titles: Iterable[str]) -> dict[str, str]:
        """Map English Wikipedia titles (redirects allowed) to Wikidata ids."""
        titles = sorted({t.replace("_", " ").strip() for t in titles if t})
        result: dict[str, str] = {}
        for i in range(0, len(titles), 50):
            batch = titles[i : i + 50]
            data = self.http.get_json(
                WIKIPEDIA_API,
                params={
                    "action": "query",
                    "titles": "|".join(batch),
                    "redirects": "1",
                    "prop": "pageprops",
                    "ppprop": "wikibase_item",
                    "format": "json",
                    "formatversion": "2",
                },
                min_interval=3,
            )
            query = data.get("query", {})
            mapping = {t: t for t in batch}
            for step in ("normalized", "redirects"):
                for item in query.get(step, []):
                    for original, current in mapping.items():
                        if current == item["from"]:
                            mapping[original] = item["to"]
            by_title = {
                p["title"]: p.get("pageprops", {}).get("wikibase_item")
                for p in query.get("pages", [])
                if not p.get("missing")
            }
            for original, final in mapping.items():
                if by_title.get(final):
                    result[original] = by_title[final]
        # Red links (no article yet) often already have a Wikidata item.
        for title in titles:
            if title not in result:
                qid = self.search_label(title)
                if qid:
                    result[title] = qid
        return result

    def search_label(self, label: str) -> str | None:
        data = self.http.get_json(
            WIKIDATA_API,
            params={"action": "wbsearchentities", "search": label, "language": "en",
                    "type": "item", "limit": "5", "format": "json"},
            min_interval=1,
        )
        for hit in data.get("search", []):
            candidates = [hit.get("label", ""), hit.get("match", {}).get("text", "")]
            if any(normalize(c) == normalize(label) or similar(c, label) >= 0.9 for c in candidates if c):
                return hit["id"]
        return None

    # ------------------------------------------------------------ entities
    def entities(
        self, qids: Iterable[str], languages: str = "en|mul", props: str = "labels|aliases|claims|sitelinks"
    ) -> dict[str, Entity]:
        qids = list(qids)
        full = "claims" in props
        wanted = sorted({
            q for q in qids
            if q and (q not in self._entities or (full and not self._entities[q].complete))
        })
        for i in range(0, len(wanted), 50):
            batch = wanted[i : i + 50]
            data = self.http.get_json(
                WIKIDATA_API,
                params={
                    "action": "wbgetentities",
                    "ids": "|".join(batch),
                    "props": props,
                    "languages": languages,
                    "sitefilter": "enwiki",
                    "format": "json",
                },
                min_interval=1,
            )
            for qid, raw in data.get("entities", {}).items():
                if "missing" in raw:
                    continue
                self._entities[qid] = Entity(
                    qid=qid,
                    labels={k: v["value"] for k, v in raw.get("labels", {}).items()},
                    aliases={k: [a["value"] for a in v] for k, v in raw.get("aliases", {}).items()},
                    claims=raw.get("claims", {}),
                    enwiki=raw.get("sitelinks", {}).get("enwiki", {}).get("title", ""),
                    complete=full,
                )
        return {q: self._entities[q] for q in qids if q in self._entities}

    # -------------------------------------------------------------- SPARQL
    def sparql(self, query: str) -> list[dict]:
        data = self.http.get_json(
            SPARQL_ENDPOINT,
            params={"query": query, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
            min_interval=2,
            timeout=150,
        )
        return [
            {k: v.get("value", "") for k, v in row.items()} for row in data["results"]["bindings"]
        ]

    def subdivisions(self, country_qids: Iterable[str]) -> dict[str, list[str]]:
        """First-level subdivisions (P150) of each country."""
        values = " ".join(f"wd:{q}" for q in country_qids)
        rows = self.sparql(f"SELECT ?country ?sub WHERE {{ VALUES ?country {{ {values} }} ?country wdt:P150 ?sub . }}")
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["country"].rsplit("/", 1)[-1], []).append(row["sub"].rsplit("/", 1)[-1])
        return out

    def dated_elections(self, country_qids: Iterable[str], start: date, end: date) -> list[str]:
        """Election items in the given countries dated inside [start, end].

        Year-precision dates are stored as 1 January, so "<start year>-01-01"
        is matched too. The query returns each item's jurisdiction and the
        jurisdiction's parent; items held in a municipality or district (the
        parent is not the country) are dropped here, which keeps the number
        of follow-up entity requests small. (Doing this filter inside SPARQL
        makes the query time out.)
        """
        values = " ".join(f"wd:{q}" for q in country_qids)
        year_start = date(start.year, 1, 1)
        query = f"""
SELECT ?e ?country ?jur ?jurParent WHERE {{
  VALUES ?country {{ {values} }}
  ?e wdt:P17 ?country ; wdt:P31 ?class .
  ?class wdt:P279* wd:{ELECTION_CLASS} .
  ?e wdt:P585 ?date .
  FILTER(?date <= "{end.isoformat()}T00:00:00Z"^^xsd:dateTime &&
         (?date >= "{start.isoformat()}T00:00:00Z"^^xsd:dateTime ||
          ?date = "{year_start.isoformat()}T00:00:00Z"^^xsd:dateTime))
  OPTIONAL {{ ?e wdt:P1001 ?jur . OPTIONAL {{ ?jur wdt:P131 ?jurParent . }} }}
}}"""
        items: dict[str, dict[str, set[str] | str]] = {}
        for row in self.sparql(query):
            qid = row["e"].rsplit("/", 1)[-1]
            item = items.setdefault(qid, {"country": row["country"].rsplit("/", 1)[-1], "jur": set(), "parents": set()})
            if row.get("jur"):
                item["jur"].add(row["jur"].rsplit("/", 1)[-1])
            if row.get("jurParent"):
                item["parents"].add(row["jurParent"].rsplit("/", 1)[-1])
        return [
            qid for qid, it in items.items()
            if not it["jur"] or it["country"] in it["jur"] or it["country"] in it["parents"] or not it["parents"]
        ]


@dataclass
class ResolvedElection:
    qid: str
    title: str
    enwiki: str
    country: Country
    level: str
    election_type: str
    indirect: bool
    region: str = ""
    region_id: str = ""
    region_names: set[str] = field(default_factory=set)
    aggregate: bool = False
    dates: list[Period] = field(default_factory=list)
    part_of: list[str] = field(default_factory=list)
    description: str = ""


class ElectionResolver:
    """Classifies Wikidata election items (with caching across sources)."""

    def __init__(self, wikidata: Wikidata, config: Config, regions: "RegionIndex | None" = None) -> None:
        self.wd = wikidata
        self.config = config
        self.regions = regions
        self._cache: dict[str, ResolvedElection | None] = {}
        self.dropped: dict[str, str] = {}

    def resolve(
        self, qids: Iterable[str], hints: dict[str, Country] | None = None
    ) -> dict[str, ResolvedElection]:
        """Classify election items. ``hints`` maps an item to the country a
        page lists it under, used when Wikidata itself has no country."""
        qids = [q for q in dict.fromkeys(qids) if q]
        hints = hints or {}
        todo = [q for q in qids if q not in self._cache]
        if todo:
            # Election labels in many languages help to find the region when
            # the item has no jurisdiction ("Elecciones ... de Asturias").
            items = self.wd.entities(todo, languages=LABEL_LANGUAGES)
            refs: set[str] = set()
            for e in items.values():
                for prop in ("P17", "P31", "P1001", "P361", "P155"):
                    refs.update(e.ids(prop))
            # Labels of classes/parents in English; jurisdictions in many
            # languages so regions can be matched by their local names.
            self.wd.entities(refs, languages=LABEL_LANGUAGES)
            # Items missing a country or jurisdiction inherit them from the
            # previous election of the same series ("follows", P155).
            pred_refs: set[str] = set()
            for e in items.values():
                if not e.ids("P17") or not e.ids("P1001"):
                    for pred in (self.entity(q) for q in e.ids("P155")):
                        if pred:
                            pred_refs.update(pred.ids("P17") + pred.ids("P1001"))
            self.wd.entities(pred_refs, languages=LABEL_LANGUAGES)
            for q in todo:
                self._cache[q] = self._classify(items.get(q), hints.get(q))
        return {q: self._cache[q] for q in qids if self._cache.get(q)}

    @property
    def resolved(self) -> dict[str, ResolvedElection]:
        return {q: r for q, r in self._cache.items() if r}

    def entity(self, qid: str) -> Entity | None:
        return self.wd._entities.get(qid)

    def _drop(self, qid: str, reason: str) -> None:
        self.dropped[qid] = reason
        return None

    def _classify(self, item: Entity | None, hint: Country | None = None) -> ResolvedElection | None:
        if item is None:
            return None
        get = self.entity
        eu = lambda qs: [c for c in (self.config.country_by_qid(q) for q in qs) if c]  # noqa: E731
        predecessors = [p for p in (get(q) for q in item.ids("P155")) if p]
        jurisdictions = [j for j in (get(q) for q in item.ids("P1001")) if j]
        if not jurisdictions:
            for pred in predecessors:
                jurisdictions += [j for j in (get(q) for q in pred.ids("P1001")) if j]

        if item.ids("P17") and not eu(item.ids("P17")):
            return self._drop(item.qid, "not in an EU member state")
        countries = eu(item.ids("P17"))
        for source in (jurisdictions, predecessors):
            if not countries:
                for e in source:
                    countries += eu(e.ids("P17")) + eu([e.qid])
        if not countries and hint:
            countries = [hint]
        if not countries:
            found = self.config.find_country_in_text(f"{item.label()} {item.enwiki}")
            countries = [found] if found else []
        if not countries:
            return self._drop(item.qid, "no EU country found")
        country = countries[0]

        classes = [get(q).label() for q in item.ids("P31") if get(q)]
        text = " | ".join([item.label(), item.enwiki, *classes])
        reason = classify.exclusion_reason(text, self.config)
        if reason:
            return self._drop(item.qid, reason)

        level, region, region_id, region_names, aggregate = "national", "", "", set(), False
        sub = [j for j in jurisdictions if j.qid != country.wikidata]
        if sub:
            j = sub[0]
            if self.config.is_excluded_territory(j.label()):
                return self._drop(item.qid, f"outside the EU: {j.label()}")
            first_level = self._first_level(j, country)
            if first_level is None:
                return self._drop(item.qid, f"local jurisdiction {j.label()}")
            if first_level.qid != j.qid and not classify.looks_regional(text):
                # e.g. a city council election: the city sits inside a region.
                return self._drop(item.qid, f"local jurisdiction {j.label()}")
            j = first_level
            level, region, region_id, region_names = "regional", j.label(), j.qid, j.names()
        else:
            title = f"{item.label()} {item.enwiki}"
            regional_words = classify.looks_regional(text)
            match = None
            if self.regions is not None and not re.search(r"\belections\b", title, re.I):
                match = self.regions.infer(country.code, [item.enwiki, *item.labels.values()])
            if match and (regional_words or self.config.find_country_in_text(title) is None):
                level, (region_id, region) = "regional", match
                region_names = self.regions.names(country.code, region_id)
            elif regional_words:
                level, aggregate, region = "regional", True, "All regions"

        etype = classify.election_type(text, country, level)
        indirect = classify.is_indirect(text, country, etype)
        if not classify.in_scope(level, indirect, self.config):
            return self._drop(item.qid, f"out of scope ({level}, indirect={indirect})")
        return ResolvedElection(
            qid=item.qid,
            title=item.enwiki or item.label(),
            enwiki=item.enwiki,
            country=country,
            level=level,
            election_type=etype,
            indirect=indirect,
            region=region,
            region_id=region_id,
            region_names=region_names,
            aggregate=aggregate,
            dates=item.times("P585"),
            part_of=item.ids("P361"),
            description=text,
        )

    def _first_level(self, j: Entity, country: Country, depth: int = 3) -> Entity | None:
        """Walk up P131 until reaching a subdivision whose parent is the country."""
        current = j
        for _ in range(depth + 1):
            parents = current.ids("P131")
            if not parents or country.wikidata in parents or self._is_outermost(current):
                return current
            self.wd.entities(parents, languages=LABEL_LANGUAGES)
            nxt = next((self.entity(q) for q in parents if self.entity(q)), None)
            if nxt is None:
                return None
            current = nxt
        return None

    def _is_outermost(self, j: Entity) -> bool:
        # Overseas collectivities such as Saint-Martin are not "located in"
        # France on Wikidata but are part of the EU.
        return any("outermost" in (self.entity(q).label().lower() if self.entity(q) else "")
                   for q in j.ids("P31"))

    def parent_election(self, election: ResolvedElection) -> ResolvedElection | None:
        """The national election of the same kind this item is a component of
        ("2027 Italian Senate election" -> "2027 Italian general election").
        A presidential vote held together with a parliamentary one stays a
        separate election."""
        for parent_qid in election.part_of:
            parent = self.resolve([parent_qid]).get(parent_qid)
            if (
                parent
                and parent.country.code == election.country.code
                and parent.level == election.level == "national"
                and parent.election_type == election.election_type
            ):
                return parent
        return None


class RegionIndex:
    """First-level regions of each country, with names in many languages.

    Filled from Wikidata (P150 of each country, loaded lazily on first use)
    and from the jurisdictions of resolved elections. Used to give every
    region one identity, whatever language or form a source writes it in,
    and to infer the region of an election from its title.
    """

    def __init__(self, wikidata: Wikidata | None = None, config: Config | None = None) -> None:
        self._regions: dict[str, list[tuple[str, str, set[str]]]] = {}
        self._wd = wikidata
        self._config = config
        self._loaded = wikidata is None
        self._unknown: dict[tuple[str, str], tuple[str, str] | None] = {}

    def add(self, country_code: str, region_id: str, label: str, names: set[str]) -> None:
        bucket = self._regions.setdefault(country_code, [])
        for i, (rid, lbl, existing) in enumerate(bucket):
            if rid == region_id:
                bucket[i] = (rid, lbl, existing | names | {label})
                return
        bucket.append((region_id, label, {label, *names}))

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            subs = self._wd.subdivisions(c.wikidata for c in self._config.countries.values())
            entities = self._wd.entities(
                [q for qs in subs.values() for q in qs], languages=LABEL_LANGUAGES, props="labels|aliases"
            )
        except Exception as exc:  # the index still works with resolved jurisdictions
            log.warning("could not load first-level regions from Wikidata: %s", exc)
            return
        for country_qid, qids in subs.items():
            country = self._config.country_by_qid(country_qid)
            for q in qids:
                e = entities.get(q)
                if country and e and not self._config.is_excluded_territory(e.label()):
                    self.add(country.code, q, e.label(), e.names())

    def resolve_unknown(self, country: Country, name: str) -> tuple[str, str] | None:
        """Look a region name up on Wikidata; accept it only if it is a
        first-level subdivision of the country (its P131 is the country, or
        it has no parent, like overseas collectivities). Rejects island
        councils, provinces, counties below the regional tier..."""
        key = (country.code, normalize(name))
        if key in self._unknown:
            return self._unknown[key]
        result = None
        try:
            data = self._wd.http.get_json(
                WIKIDATA_API,
                params={"action": "wbsearchentities", "search": name, "language": "en",
                        "type": "item", "limit": "5", "format": "json"},
                min_interval=1,
            )
            hits = [h["id"] for h in data.get("search", [])]
            for qid, e in self._wd.entities(hits, languages=LABEL_LANGUAGES).items():
                codes = {c.code for c in (self._config.country_by_qid(q) for q in e.ids("P17")) if c}
                if country.code not in codes:
                    continue
                parents = e.ids("P131")
                if not parents or country.wikidata in parents or any(q in parents for q in country.wikidata_aliases):
                    result = (qid, e.label())
                    self.add(country.code, qid, e.label(), e.names() | {name})
                    break
        except Exception as exc:
            log.warning("region lookup for %r failed: %s", name, exc)
        self._unknown[key] = result
        return result

    @property
    def can_search(self) -> bool:
        return self._wd is not None and self._config is not None

    def names(self, country_code: str, region_id: str) -> set[str]:
        return next((n for rid, _, n in self._regions.get(country_code, []) if rid == region_id), set())

    def lookup(self, country_code: str, name: str) -> tuple[str, str] | None:
        """Return (region_id, canonical label) for a name, or None."""
        self._ensure_loaded()
        n = normalize(name)
        bucket = self._regions.get(country_code, [])
        for region_id, label, names in bucket:
            if n in {normalize(x) for x in names}:
                return region_id, label
        for region_id, label, names in bucket:
            if any(same_region_name(name, x) for x in names):
                return region_id, label
        return None

    def infer(self, country_code: str, texts: Iterable[str]) -> tuple[str, str] | None:
        """Find the region a title talks about: '2027 Asturian regional election'
        -> Asturias, 'Elecciones a las Cortes de Castilla-La Mancha' -> Castilla-La Mancha.

        A region matches when every distinctive word of one of its names
        appears in a text, allowing adjectival endings (Asturias/Asturian,
        Sicily/Sicilian, Åland/Ålandic). The most specific match wins
        ("Lower Saxony" over "Saxony").
        """
        self._ensure_loaded()
        words = {w for t in texts if t for w in normalize(t).split()}
        best, best_score = None, 0
        for region_id, label, names in self._regions.get(country_code, []):
            for name in names:
                tokens = [t for t in region_tokens(name) if len(t) >= 4]
                if tokens and all(any(_same_stem(t, w) for w in words) for t in tokens):
                    score = sum(len(t) for t in tokens)
                    if score > best_score:
                        best, best_score = (region_id, label), score
        return best


def _same_stem(a: str, b: str) -> bool:
    if a == b:
        return True
    prefix = 0
    for x, y in zip(a, b):
        if x != y:
            break
        prefix += 1
    return prefix >= 5 and prefix >= 0.7 * min(len(a), len(b))
