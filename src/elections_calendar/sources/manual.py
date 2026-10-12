"""Hand-curated additions/corrections (``data/manual_events.csv``).

Use it for elections the automatic sources miss or get wrong. Rows here have
the highest priority. Columns: country_code, level, region, election_type,
indirect, title, date_text, date_start, date_end, second_round, notes, source_url
(date_text is used when date_start is empty).
"""

from __future__ import annotations

import csv
from datetime import date

from ..dates import parse_period
from ..models import Candidate, Period
from .base import Source

COLUMNS = [
    "country_code", "level", "region", "election_type", "indirect", "title", "date_text",
    "date_start", "date_end", "second_round", "notes", "source_url",
]


class ManualSource(Source):
    name = "manual"

    def fetch(self) -> list[Candidate]:
        path = self.config.root / self.config.source_settings("manual").get("path", "data/manual_events.csv")
        if not path.exists():
            return []
        out = []
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                cand = self._row(row)
                if cand:
                    out.append(cand)
        return out

    def _row(self, row: dict) -> Candidate | None:
        country = self.config.country((row.get("country_code") or "").strip())
        if not country:
            return None
        if row.get("date_start"):
            start = date.fromisoformat(row["date_start"].strip())
            end = date.fromisoformat(row["date_end"].strip()) if row.get("date_end") else start
            period = Period(start, end, "day" if (end - start).days <= 2 else "range", row.get("date_text") or "")
        else:
            period = parse_period(row.get("date_text", ""))
        if period is None:
            self.log.warning("manual row without a usable date: %s", row)
            return None
        level = (row.get("level") or "national").strip()
        return Candidate(
            source=self.name,
            country_code=country.code,
            level=level,
            election_type=(row.get("election_type") or ("regional" if level == "regional" else "parliamentary")).strip(),
            period=period,
            region=(row.get("region") or "").strip(),
            indirect=(row.get("indirect") or "").strip().lower() in ("1", "yes", "true", "y"),
            title=(row.get("title") or "").strip(),
            second_round=date.fromisoformat(row["second_round"].strip()) if row.get("second_round") else None,
            url=(row.get("source_url") or "").strip(),
            confidence="high",
            notes=(row.get("notes") or "").strip(),
        )
