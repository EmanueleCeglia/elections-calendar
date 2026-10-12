"""European Committee of the Regions - "EU-27 Election Calendar" (PDF).

The PDF opens with an "EU-wide Chronological Overview" table
(Date | Country | Level | Territory | Election | Status | Source) that mirrors
the country chapters, so only that table is parsed. It is particularly good
at regional elections and states whether a date is officially confirmed.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date

from ..classify import election_type, exclusion_reason, is_aggregate_territory, is_indirect
from ..dates import split_rounds
from ..models import Candidate
from .base import Source


@dataclass
class CorRow:
    date_text: str
    country: str
    level: str
    territory: str
    election: str
    status: str
    source: str
    year_context: str = ""


def _join(*parts: str) -> str:
    """Join cell fragments split across PDF lines ("Mecklenburg-" + "Vorpommern")."""
    out = ""
    for p in (p.strip() for p in parts if p and p.strip()):
        out = f"{out}{p}" if out.endswith("-") else f"{out} {p}"
    return re.sub(r"\s+", " ", out).strip()


def parse_overview(pdf_bytes: bytes) -> tuple[list[CorRow], date | None]:
    import pdfplumber  # imported lazily: only this source needs it

    rows: list[CorRow] = []
    last_update = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        first_text = pdf.pages[0].extract_text() or ""
        m = re.search(r"Last Update:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", first_text)
        if m:
            last_update = date(int(m[3]), int(m[2]), int(m[1]))
        year_context = ""
        for page in pdf.pages:
            for table in page.extract_tables():
                head = " ".join(c or "" for r in table[:4] for c in r)
                if not all(k in head for k in ("Country", "Level", "Territory")):
                    continue
                header = next(r for r in table if "Country" in [(c or "").strip() for c in r])
                col = {name: [(c or "").strip() for c in header].index(name)
                       for name in ("Country", "Level", "Territory", "Status", "Source")}
                election_col = next(
                    i for r in table[:4] for i, c in enumerate(r) if c and c.strip().startswith("Election")
                )
                for raw in table:
                    cells = [re.sub(r"\s+", " ", (c or "")).strip() for c in raw]
                    if any(c.startswith(("Date / expected", "Country", "period")) for c in cells[:2]) or \
                            cells[election_col] in ("office", "Election / assembly or"):
                        continue
                    others = [c for i, c in enumerate(cells) if i != 0 and c]
                    if re.fullmatch(r"\d{4}", cells[0]) and not others:
                        year_context = cells[0]
                        continue
                    if cells[col["Country"]]:
                        rows.append(CorRow(cells[0], cells[col["Country"]], cells[col["Level"]],
                                           cells[col["Territory"]], cells[election_col],
                                           cells[col["Status"]], cells[col["Source"]], year_context))
                    elif rows and any(cells):
                        r = rows[-1]
                        r.date_text = _join(r.date_text, cells[0])
                        r.territory = _join(r.territory, cells[col["Territory"]])
                        r.election = _join(r.election, cells[election_col])
                        r.status = _join(r.status, cells[col["Status"]])
                        r.source = _join(r.source, cells[col["Source"]])
    for r in rows:
        if r.year_context and not re.search(r"\d{4}", r.date_text):
            r.date_text = _join(r.date_text, r.year_context)
    return rows, last_update


def split_territories(text: str, is_region=lambda name: False) -> list[str]:
    """"Aragon, Asturias and La Rioja" -> three names, keeping region names
    that contain "and" ("Castile and León") when ``is_region`` knows them."""
    text = re.sub(r"\([^)]*\)", "", text).strip(" .")
    parts = [p.strip() for p in re.split(r",\s*", text) if p.strip()]
    if not parts or " and " not in parts[-1] or is_region(parts[-1]):
        return parts
    last = parts[-1]
    pieces = last.split(" and ")
    for i in range(1, len(pieces)):
        left, right = " and ".join(pieces[:i]), " and ".join(pieces[i:])
        if is_region(left) or is_region(right):
            return parts[:-1] + [left, right]
    return parts[:-1] + pieces


class CorPdfSource(Source):
    name = "cor_pdf"

    def fetch(self) -> list[Candidate]:
        settings = self.config.source_settings("cor_pdf")
        errors = []
        for url in settings.get("urls", []):
            try:
                pdf = self.ctx.http.request_bytes(url, min_interval=1)
                rows, last_update = parse_overview(pdf)
            except Exception as exc:  # try the next configured URL
                errors.append(f"{url}: {exc}")
                continue
            if not rows:
                errors.append(f"{url}: no overview table found")
                continue
            self._check_age(last_update, settings)
            self.log.info("CoR calendar (last update %s): %d rows", last_update, len(rows))
            return [c for row in rows for c in self.to_candidates(row, url, last_update)]
        raise RuntimeError("; ".join(errors) or "no CoR PDF URL configured")

    def _check_age(self, last_update: date | None, settings: dict) -> None:
        max_age = int(settings.get("warn_if_older_than_days", 90))
        if last_update and (self.ctx.window.start - last_update).days > max_age:
            self.log.warning(
                "CoR calendar last updated %s (> %d days ago); check sources.cor_pdf.urls "
                "for a newer edition", last_update, max_age,
            )

    def to_candidates(self, row: CorRow, url: str, last_update: date | None) -> list[Candidate]:
        level = row.level.lower()
        if level not in ("national", "regional"):
            return []
        country = self.config.country_by_name(row.country) or self.config.find_country_in_text(row.country)
        if not country:
            return []
        if exclusion_reason(row.election, self.config):
            return []
        year_hint = int(m[0]) if (m := re.search(r"\d{4}", row.date_text)) else None
        period, second = split_rounds(row.date_text, year_hint)
        if period is None:
            return []
        confirmed = row.status.lower().startswith("confirmed")
        common = dict(
            source=self.name,
            country_code=country.code,
            level=level,
            period=period,
            second_round=second,
            url=url,
            confidence="high" if confirmed else "medium",
            notes=f"CoR ({last_update}): {row.status}; source: {row.source}",
        )
        if level == "national":
            etype = election_type(row.election, country, "national")
            return [Candidate(
                election_type=etype,
                indirect=is_indirect(row.election, country, etype),
                title=f"{country.name}: {row.election}",
                **common,
            )]
        # Regional: one candidate per named region, or one aggregate row.
        if is_aggregate_territory(row.territory) or not row.territory:
            return [Candidate(
                election_type="regional", region=f"All {row.territory}".strip(), aggregate=True,
                title=f"{country.name}: {row.election} ({row.territory})", **common,
            )]
        out = []
        known = lambda name: self.ctx.regions.lookup(country.code, name) is not None  # noqa: E731
        for name in split_territories(row.territory, known):
            if self.config.is_excluded_territory(name):
                continue
            match = self.ctx.regions.lookup(country.code, name)
            region_id, region = match if match else ("", name)
            out.append(Candidate(
                election_type="regional", region=region, region_id=region_id,
                title=f"{country.name}, {region}: {row.election}", **common,
            ))
        return out
