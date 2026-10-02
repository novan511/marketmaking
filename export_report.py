#!/usr/bin/env python3
"""Generate export event/series dari data collector.

Usage:
  python3 export_report.py
  python3 export_report.py --since 2026-10-02T00:00:00+00:00 --limit 25
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from src.export import events_csv, events_json, load_events, report_md


def main():
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Export event & report")
    ap.add_argument("--data", default=str(root / "data"))
    ap.add_argument("--outdir", default=None, help="default: <data>/exports")
    ap.add_argument("--since", default=None, help="filter ts_open >= ISO timestamp (UTC)")
    ap.add_argument("--limit", type=int, default=15, help="jumlah event detail di report.md")
    args = ap.parse_args()

    data = Path(args.data)
    outdir = Path(args.outdir) if args.outdir else data / "exports"
    outdir.mkdir(parents=True, exist_ok=True)
    events = load_events(data / "events.jsonl")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    (outdir / f"events_{ts}.csv").write_text(events_csv(events))
    (outdir / f"events_{ts}.json").write_text(events_json(events))
    md = report_md(events, since=args.since, limit=args.limit)
    (outdir / f"report_{ts}.md").write_text(md)
    print(f"Events   : {len(events)}")
    print(f"CSV      : {outdir / f'events_{ts}.csv'}")
    print(f"JSON     : {outdir / f'events_{ts}.json'}")
    print(f"Report   : {outdir / f'report_{ts}.md'}")
    print(f"Series   : {data / 'series.csv'} (salin manual bila perlu)")


if __name__ == "__main__":
    main()
