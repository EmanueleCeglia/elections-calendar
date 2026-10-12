"""Load ``config/settings.yaml`` and ``config/countries.yaml``."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .textutil import normalize

def find_root() -> Path:
    """Directory holding ``config/``: $ELECTIONS_CALENDAR_ROOT, else the
    working directory, else the source checkout this package lives in."""
    env = os.environ.get("ELECTIONS_CALENDAR_ROOT")
    if env:
        return Path(env)
    cwd = Path.cwd()
    if (cwd / "config" / "settings.yaml").exists():
        return cwd
    return Path(__file__).resolve().parents[2]


@dataclass
class Country:
    code: str
    name: str
    wikidata: str
    names: list[str]
    wikidata_aliases: list[str] = field(default_factory=list)
    indirect: set[str] = field(default_factory=set)
    upper_house: list[str] = field(default_factory=list)
    concurrent_upper: bool = False
    not_elected: list[str] = field(default_factory=list)


class Config:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else find_root()
        self.settings: dict[str, Any] = yaml.safe_load(
            (self.root / "config" / "settings.yaml").read_text(encoding="utf-8")
        )
        raw = yaml.safe_load((self.root / "config" / "countries.yaml").read_text(encoding="utf-8"))
        self.countries: dict[str, Country] = {
            code: Country(
                code=code,
                name=c["name"],
                wikidata=c["wikidata"],
                names=list(c.get("names", [])) + [c["name"]],
                wikidata_aliases=list(c.get("wikidata_aliases", [])),
                indirect=set(c.get("indirect", [])),
                upper_house=list(c.get("upper_house", [])),
                concurrent_upper=bool(c.get("concurrent_upper", False)),
                not_elected=list(c.get("not_elected", [])),
            )
            for code, c in raw.items()
        }
        self._by_qid = {q: c for c in self.countries.values() for q in [c.wikidata, *c.wikidata_aliases]}
        self._by_name: dict[str, Country] = {}
        for c in self.countries.values():
            for n in c.names:
                self._by_name[normalize(n)] = c
        # Longest names first so "Czech Republic" wins over "Czech".
        self._name_patterns = sorted(self._by_name.items(), key=lambda kv: -len(kv[0]))
        self.excluded_territories = {normalize(t) for t in self.settings.get("excluded_territories", [])}

    # ------------------------------------------------------------- lookups
    def country(self, code: str) -> Country | None:
        return self.countries.get(code.upper()) if code else None

    def country_by_qid(self, qid: str) -> Country | None:
        return self._by_qid.get(qid)

    def country_by_name(self, name: str) -> Country | None:
        if not name:
            return None
        n = normalize(name)
        n = re.sub(r"^(elections|politics) (in|of) ", "", n)
        return self._by_name.get(n)

    def find_country_in_text(self, text: str) -> Country | None:
        n = f" {normalize(text)} "
        for name, country in self._name_patterns:
            if f" {name} " in n:
                return country
        return None

    def is_excluded_territory(self, name: str) -> bool:
        return bool(name) and normalize(name) in self.excluded_territories

    def mentions_excluded_territory(self, text: str) -> bool:
        n = f" {normalize(text)} "
        return any(f" {t} " in n for t in self.excluded_territories)

    # ------------------------------------------------------------ settings
    def source_settings(self, name: str) -> dict[str, Any]:
        return self.settings.get("sources", {}).get(name, {}) or {}

    def source_priority(self, source: str) -> int:
        base = source.split(":", 1)[0]
        return int(self.settings.get("source_priority", {}).get(base, 0))

    def scope(self, key: str, default: Any = None) -> Any:
        return self.settings.get("scope", {}).get(key, default)

    def output_path(self, key: str) -> Path:
        out = self.settings["output"]
        return (self.root / out["dir"]).resolve() / out[key]
