#!/usr/bin/env python3
"""Fetch the IHSG daily series — the market-adjustment baseline for Broker Alpha.

Every forward return in the backtest is scored as `r_stock - r_IHSG`. Without this,
the leaderboard would rank brokers by beta: in a rising market whoever holds the most
volatile names looks like a genius, and in a falling one they look like a fool. The
market adjustment is what makes "does this broker pick well?" a separable question.

Sectors caps `/index-daily/` at a 90-day window, so a 2-year history is chained across
~9 calls (1 credit each). Invezgo has no daily index-history endpoint — its index
support is intraday/list only — so this stays on Sectors.

Output: data/panel/benchmark-ihsg.csv  (date,close)

Usage:
    py scripts/fetch_benchmark.py [--years 2] [--index ihsg]
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sectors_client import SectorsClient  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "panel"
WINDOW = 89  # API caps at 90 days; leave a day of slack


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--index", default="ihsg")
    args = ap.parse_args()

    sec = SectorsClient()
    if not sec.enabled:
        print("SECTORS_API_KEY not set")
        return 2

    end = date.today()
    start = end - timedelta(days=int(365 * args.years))
    print(f"index  : {args.index}\nwindow : {start} -> {end}")

    closes: dict[str, float] = {}
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=WINDOW), end)
        rows = sec.index_daily(args.index, cursor.isoformat(), chunk_end.isoformat())
        got = 0
        for r in rows or []:
            d = str(r.get("date"))[:10]
            # The index endpoint calls the level `price`, not `close`.
            v = r.get("price", r.get("close"))
            if d and v is not None:
                try:
                    closes[d] = float(v)
                    got += 1
                except (TypeError, ValueError):
                    pass
        print(f"  {cursor} -> {chunk_end}: {got} rows")
        cursor = chunk_end + timedelta(days=1)

    if not closes:
        print("\nNo data returned — nothing written.")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"benchmark-{args.index}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "close"])
        for d in sorted(closes):
            w.writerow([d, closes[d]])

    days = sorted(closes)
    print(f"\nwrote {len(closes):,} rows -> {path}")
    print(f"  span   : {days[0]} -> {days[-1]}")
    print(f"  level  : {closes[days[0]]:,.1f} -> {closes[days[-1]]:,.1f} "
          f"({closes[days[-1]] / closes[days[0]] - 1:+.1%} over the window)")
    sec.report()

    # A 2-year window should hold ~480 trading days. Materially fewer means gaps, which
    # would silently drop events from the backtest rather than fail loudly.
    expected = args.years * 240
    if len(closes) < expected * 0.9:
        print(f"\n  WARNING: expected ~{expected:.0f} trading days, got {len(closes)}. "
              f"Check for gaps before scoring.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
