"""Command line: ``python -m elections_calendar run|evaluate``."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from .config import Config


def _date(value: str) -> date:
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="elections_calendar", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="fetch sources and create/update the calendar")
    run_p.add_argument("--today", type=_date, help="run date (default: today)")
    run_p.add_argument("--end", type=_date, help="window end (default: 31 Dec of next year)")
    run_p.add_argument("--sources", help="comma-separated subset: wikipedia,wikidata,cor_pdf,manual,llm")
    run_p.add_argument("--cache-dir", help="cache HTTP responses here (development)")
    run_p.add_argument("--dry-run", action="store_true", help="do not write output files")
    run_p.add_argument("--output-dir", help="write outputs here instead of the configured directory")

    ev_p = sub.add_parser("evaluate", help="compare the calendar with a ground-truth CSV")
    ev_p.add_argument("--ground-truth", type=Path,
                      default=Path("data/ground_truth/eu_elections_2026-10-12_to_2027-12-31.csv"))
    ev_p.add_argument("--calendar", type=Path, help="calendar CSV (default: configured output)")
    ev_p.add_argument("--start", type=_date, default=date(2026, 10, 12))
    ev_p.add_argument("--end", type=_date, default=date(2027, 12, 31))

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config()
    if getattr(args, "output_dir", None):
        config.settings["output"]["dir"] = args.output_dir

    if args.command == "run":
        from .pipeline import run

        sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
        result = run(config, today=args.today, window_end=args.end, only_sources=sources,
                     cache_dir=args.cache_dir, dry_run=args.dry_run)
        print(f"\nWindow {result.window.start} .. {result.window.end}")
        for name, status in result.source_report.items():
            print(f"  {name:10} {status}")
        print(f"Upcoming: {len(result.upcoming)}   past: {len(result.past)}   needs review: {len(result.review)}")
        counts: dict[str, int] = {}
        for ch in result.changes:
            counts[ch.change] = counts.get(ch.change, 0) + 1
        print("Changes: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"))
        for ch in result.changes:
            if ch.change != "added":
                print(f"  {ch.change:11} {ch.description}")
        return 0

    if args.command == "evaluate":
        from .evaluate import evaluate, print_report

        calendar = args.calendar or config.output_path("calendar_csv")
        print_report(evaluate(args.ground_truth, calendar, args.start, args.end))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
