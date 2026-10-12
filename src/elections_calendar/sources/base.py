"""Shared plumbing for sources."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from ..config import Config
from ..http import HttpClient
from ..models import Candidate
from ..wikidata import ElectionResolver, RegionIndex, ResolvedElection, Wikidata

log = logging.getLogger(__name__)


@dataclass
class Window:
    start: date
    end: date

    @property
    def years(self) -> list[int]:
        return list(range(self.start.year, self.end.year + 1))


@dataclass
class Context:
    config: Config
    http: HttpClient
    window: Window
    wikidata: Wikidata
    resolver: ElectionResolver
    regions: RegionIndex = field(default_factory=RegionIndex)

    @classmethod
    def build(cls, config: Config, http: HttpClient, window: Window) -> "Context":
        wd = Wikidata(http)
        regions = RegionIndex(wd, config)
        return cls(config, http, window, wd, ElectionResolver(wd, config, regions), regions)

    def register_region(self, election: ResolvedElection) -> None:
        if election.region_id:
            self.regions.add(
                election.country.code, election.region_id, election.region, election.region_names
            )


class Source(ABC):
    name: str = "source"

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.log = logging.getLogger(f"{__name__}.{self.name}")

    @abstractmethod
    def fetch(self) -> list[Candidate]:
        """Return candidates; may raise, the pipeline isolates failures."""


def candidate_from_resolved(
    source: str,
    election: ResolvedElection,
    period,
    *,
    second_round=None,
    confidence: str = "medium",
    url: str = "",
    notes: str = "",
) -> Candidate:
    return Candidate(
        source=source,
        country_code=election.country.code,
        level=election.level,
        election_type=election.election_type,
        period=period,
        region=election.region,
        region_id=election.region_id,
        aggregate=election.aggregate,
        indirect=election.indirect,
        title=election.title,
        second_round=second_round,
        url=url or (f"https://en.wikipedia.org/wiki/{election.enwiki.replace(' ', '_')}" if election.enwiki else ""),
        wikidata_id=election.qid,
        wikipedia_title=election.enwiki,
        confidence=confidence,
        notes=notes,
    )
