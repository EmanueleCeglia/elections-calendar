"""Optional LLM research with OpenAI (Responses API + web search).

One request per member state: the model searches the web and returns the
elections it finds as JSON constrained by a schema. It catches what the
structured sources miss (snap elections called last week, regional votes
without a Wikipedia article yet) and confirms dates with official sources.

Runs only when enabled in settings and OPENAI_API_KEY is set. LLM findings
are capped at "medium" confidence: on their own they never override a
structured source, but they corroborate and fill gaps.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from ..config import Country
from ..dates import last_day
from ..models import ELECTION_TYPES, Candidate, Period
from .base import Source

SCHEMA = {
    "type": "object",
    "properties": {
        "elections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "English name, e.g. '2027 Saarland state election'"},
                    "level": {"type": "string", "enum": ["national", "regional"]},
                    "region": {"type": "string", "description": "English name of the first-level region; empty for national elections"},
                    "election_type": {"type": "string", "enum": list(ELECTION_TYPES)},
                    "indirect": {"type": "boolean", "description": "true when chosen by parliament or an electoral college, not by voters"},
                    "date_start": {"type": "string", "description": "YYYY-MM-DD; first day of the expected interval or the election day"},
                    "date_end": {"type": "string", "description": "YYYY-MM-DD; equal to date_start when the exact day is known"},
                    "date_text": {"type": "string", "description": "how the date is described, e.g. '18 April 2027', 'autumn 2027', 'by 22 December 2027'"},
                    "second_round_date": {"type": "string", "description": "YYYY-MM-DD of a scheduled run-off, or empty"},
                    "status": {"type": "string", "enum": ["confirmed", "expected", "speculative"]},
                    "date_basis": {
                        "type": "string",
                        "enum": ["fixed", "official_statement", "legal_window", "estimate"],
                        "description": "fixed = set by decree/law; official_statement = announced by a "
                                       "government or electoral body; legal_window = follows from the "
                                       "constitution/electoral law; estimate = analysts' or media expectation",
                    },
                    "source_urls": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
                "required": [
                    "title", "level", "region", "election_type", "indirect", "date_start", "date_end",
                    "date_text", "second_round_date", "status", "date_basis", "source_urls", "notes",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["elections"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You are a meticulous election analyst. Use web search to build an accurate, \
up-to-date list of elections. Prefer official sources (electoral commissions, official gazettes, \
government and parliament websites) and recent reputable news; newer information (snap elections, \
postponements, dates fixed by decree) overrides older plans. Never invent dates."""

PROMPT = """Today is {today}. List every election in {country} that takes place between {start} and \
{end} (inclusive).

Include:
- national: parliament (lower/unicameral house, or both chambers together), head of state, \
European Parliament, separately elected upper house (incl. partial renewals);
- national indirect elections: head of state or upper house chosen by parliament or an electoral \
college (set indirect=true);
- regional: legislature or executive of FIRST-LEVEL subnational regions only (German/Austrian \
states, Spanish autonomous communities and autonomous cities, Italian regions, Dutch provinces, \
Åland, overseas collectivities and outermost regions...). Do not list bodies below that tier \
(island councils, provincial or foral assemblies inside a region, counties/districts of a region). \
List each region as its own entry when the regions hold separate elections on the same day; \
when a whole tier votes in one nationwide election (e.g. all provinces), one entry with region \
"All provinces" is fine.

Exclude municipal/local elections, referendums, by-elections, party primaries, and the election \
of a head of government or regional governor by an assembly (government formation). Include an \
election whose first round is before {start} only if a later round falls inside the period.

Dates: if the exact day is known set date_start = date_end = that day (multi-day polling: first and \
last day). Otherwise give the plausible interval (month, season, year, or from today to a legal \
deadline) and describe it in date_text. status: "confirmed" = officially called or fixed; \
"expected" = follows from law/regular cycle or announced but not formalised; "speculative" = only \
discussed (include only if credible). date_basis says where the date or interval comes from; use \
"estimate" when it is only an expectation (e.g. "likely in October"). Return an empty list if there \
are none."""


class OpenAIResearchSource(Source):
    name = "llm"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.settings = self.config.source_settings("llm")
        self.model = os.environ.get("ELECTIONS_LLM_MODEL") or self.settings.get("model", "gpt-6-luna")
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "web_searches": 0}

    @staticmethod
    def available(config) -> bool:
        enabled = config.source_settings("llm").get("enabled", "auto")
        if enabled is False or str(enabled).lower() == "false":
            return False
        return bool(os.environ.get("OPENAI_API_KEY"))

    def fetch(self) -> list[Candidate]:
        from openai import OpenAI  # optional dependency

        client = OpenAI()
        countries = list(self.config.countries.values())
        workers = int(self.settings.get("max_parallel_requests", 4))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda c: self._research(client, c), countries))
        self.log.info(
            "LLM (%s): %d requests, %d input / %d output tokens, %d web searches",
            self.model, self.usage["requests"], self.usage["input_tokens"],
            self.usage["output_tokens"], self.usage["web_searches"],
        )
        return [c for batch in results for c in batch]

    def _research(self, client, country: Country) -> list[Candidate]:
        window = self.ctx.window
        prompt = PROMPT.format(
            today=window.start.isoformat(), country=country.name,
            start=window.start.isoformat(), end=window.end.isoformat(),
        )
        try:
            response = client.responses.create(
                model=self.model,
                instructions=INSTRUCTIONS,
                input=prompt,
                tools=[{
                    "type": "web_search",
                    "search_context_size": self.settings.get("search_context_size", "medium"),
                    "user_location": {"type": "approximate", "country": country.code},
                }],
                reasoning={"effort": self.settings.get("reasoning_effort", "low")},
                text={"format": {"type": "json_schema", "name": "elections", "schema": SCHEMA, "strict": True}},
            )
        except Exception as exc:  # one country failing must not stop the others
            self.log.warning("LLM research failed for %s: %s", country.name, exc)
            return []
        self._count(response)
        try:
            data = json.loads(response.output_text)
        except (json.JSONDecodeError, TypeError) as exc:
            self.log.warning("LLM returned invalid JSON for %s: %s", country.name, exc)
            return []
        out = []
        for item in data.get("elections", []):
            cand = self._candidate(country, item)
            if cand:
                out.append(cand)
        self.log.info("LLM %s: %d elections", country.name, len(out))
        return out

    def _count(self, response) -> None:
        self.usage["requests"] += 1
        usage = getattr(response, "usage", None)
        if usage:
            self.usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self.usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        self.usage["web_searches"] += sum(
            1 for item in getattr(response, "output", []) or [] if getattr(item, "type", "") == "web_search_call"
        )

    def _candidate(self, country: Country, item: dict) -> Candidate | None:
        try:
            start = date.fromisoformat(item["date_start"])
            end = date.fromisoformat(item["date_end"] or item["date_start"])
        except (ValueError, KeyError):
            return None
        if end < start:
            start, end = end, start
        if (end - start).days <= 2:
            precision = "day"
        elif start.day == 1 and end == last_day(end.year, end.month) and start.month == end.month:
            precision = "month"
        elif (start.month, start.day, end.month, end.day) == (1, 1, 12, 31):
            precision = "year"
        else:
            precision = "range"
        if item.get("level") not in ("national", "regional") or item.get("election_type") not in ELECTION_TYPES:
            return None
        second = None
        if item.get("second_round_date"):
            try:
                second = date.fromisoformat(item["second_round_date"])
            except ValueError:
                pass
        if self.config.is_excluded_territory(item.get("region", "")):
            return None
        status = item.get("status", "expected")
        basis = item.get("date_basis", "estimate")
        urls = [u for u in item.get("source_urls", []) if u.startswith("http")]
        return Candidate(
            source=self.name,
            country_code=country.code,
            level=item["level"],
            election_type="regional" if item["level"] == "regional" else item["election_type"],
            period=Period(start, end, precision, item.get("date_text") or ""),
            region=item.get("region", "") if item["level"] == "regional" else "",
            indirect=bool(item.get("indirect")),
            title=item.get("title", ""),
            second_round=second,
            url=urls[0] if urls else "",
            confidence="low" if status == "speculative" else "medium",
            notes=f"LLM ({self.model}, {status}, {basis}): {item.get('notes', '')}".strip(),
            narrows=precision == "day" or basis != "estimate",
        )
