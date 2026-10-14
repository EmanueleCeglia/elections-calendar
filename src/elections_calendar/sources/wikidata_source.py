"""Wikidata: election items of EU member states with a date in the window.

The SPARQL search runs one query per country: small queries finish well
within the query service's time limit, and a timeout costs one country
instead of all 27. Besides the search, every election item already resolved
by the Wikipedia source contributes its Wikidata date, so Wikidata dates are
used even when the (heavily loaded) query service is unavailable.
"""

from __future__ import annotations

from datetime import timedelta

from ..dates import last_day
from ..models import Candidate, Period
from ..wikidata import ResolvedElection
from .base import Source, candidate_from_resolved


def is_placeholder(p: Period) -> bool:
    """Wikidata often stores a legal deadline or a guess as "30 June 2027"."""
    return p.is_exact and p.start == last_day(p.start.year, p.start.month)


def as_deadline(p: Period) -> Period:
    return Period(p.start.replace(month=1, day=1), p.start, "deadline", f"by {p.start:%-d %B %Y} (Wikidata)")


# Retries per country query (the default client allows more), and how many
# countries may fail in a row before the service is considered down.
RETRIES_PER_COUNTRY = 2
MAX_CONSECUTIVE_FAILURES = 3


class WikidataSource(Source):
    name = "wikidata"

    def fetch(self) -> list[Candidate]:
        qids, failed = self.search()
        if failed and not qids and not self.ctx.resolver.resolved:
            raise RuntimeError(f"SPARQL search failed for every country tried: {', '.join(failed)}")
        self.ctx.resolver.resolve(qids)
        out = []
        for election in self.ctx.resolver.resolved.values():
            cand = self.candidate(election)
            if cand:
                out.append(cand)
        return out

    def search(self) -> tuple[list[str], list[str]]:
        """Run the SPARQL search country by country.

        Returns the election items found and the codes of the countries whose
        query failed or was skipped.
        """
        window = self.ctx.window
        # Look back a few weeks so a first round just before the window still
        # brings its run-off along.
        start = window.start - timedelta(days=45)
        countries = list(self.config.countries.values())
        found: list[str] = []
        failed: list[str] = []
        consecutive = 0
        for i, country in enumerate(countries):
            try:
                qids = self.ctx.wikidata.dated_elections(
                    [country.wikidata, *country.wikidata_aliases], start, window.end,
                    retries=RETRIES_PER_COUNTRY,
                )
            except Exception as exc:
                failed.append(country.code)
                consecutive += 1
                self.log.warning("SPARQL search for %s failed: %s", country.name, exc)
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    skipped = [c.code for c in countries[i + 1:]]
                    failed += skipped
                    self.log.warning(
                        "query service looks unavailable (%d failures in a row); skipping %s and "
                        "using the items found via Wikipedia for them", consecutive, ", ".join(skipped) or "nothing",
                    )
                    break
                continue
            consecutive = 0
            found += qids
        found = list(dict.fromkeys(found))
        self.log.info(
            "SPARQL: %d election items from %d/%d countries%s", len(found), len(countries) - len(failed),
            len(countries), f" (failed or skipped: {', '.join(failed)})" if failed else "",
        )
        return found, failed

    def candidate(self, election: ResolvedElection) -> Candidate | None:
        window = self.ctx.window
        if self.ctx.resolver.parent_election(election):
            return None  # e.g. "Italian Senate election" inside the general election
        self.ctx.register_region(election)
        dates = [p for p in election.dates if p.end >= window.start - timedelta(days=45)]
        if not dates:
            return None
        first_round = dates[0]
        second_round = None
        if (
            len(dates) > 1
            and first_round.is_exact
            and dates[1].is_exact
            and 0 < (dates[1].start - first_round.start).days <= 35
        ):
            second_round = dates[1].start
        placeholder = is_placeholder(first_round)
        if placeholder:
            first_round = as_deadline(first_round)
        # Wikidata keeps stale placeholders (a "2027 X election" that was
        # held early...). Year-only dates, month-end placeholders and items
        # without a Wikipedia article need corroboration from another source.
        weak = first_round.precision == "year" or placeholder or not election.enwiki
        confidence = "low" if weak else "medium"
        return candidate_from_resolved(
            self.name,
            election,
            first_round,
            second_round=second_round,
            confidence=confidence,
            url=f"https://www.wikidata.org/wiki/{election.qid}",
            notes=f"Wikidata P585 = {first_round.text}",
        )
