#!/usr/bin/env python3
"""Fetch the IHSG daily series — the market-adjustment baseline for Broker Alpha.

Every forward return in the backtest is scored as `r_stock - r_IHSG`. Without this,
the leaderboard would rank brokers by beta: in a rising market whoever holds the most
volatile names looks like a genius, and in a falling one they look like a fool. The
market adjustment is what makes "does this broker pick well?" a separable question.

Sectors caps `/index-daily/` at a 90-day window, so a 2-year history is chained across
~9 calls (1 credit each). Invezgo has no daily index-history endpoint — its index
support is intraday/list only — so this stays on Sectors.

NOTHING RAN THIS FOR TWO MONTHS. The file was written once on 2026-08-07 and no job
called this script again, so on 2026-10-08 the production copy still ended 2026-08-06
against a panel current to 10-07. `Panel.excess_return` returns None when either leg has
no benchmark, so every excess return after early August was silently absent: check 0
read a window two months shorter than it claimed, and the broker ranks that tilt the
momentum board were scored on events to 08-06 only. Nothing errored. Hence:

    --incremental   what run_daily.sh now calls every morning: top the file up from its
                    own last date (1 credit), keeping every row already on disk.
    --lag           how many panel sessions the benchmark is behind, as one integer.
                    run_daily.sh warns in the morning message when it exceeds 2.

Output: data/panel/benchmark-ihsg.csv  (date,close)

Usage:
    py scripts/fetch_benchmark.py [--years 2] [--index ihsg]
    py scripts/fetch_benchmark.py --incremental
    py scripts/fetch_benchmark.py --lag
"""
from __future__ import annotations

import argparse
import csv
import gzip
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_prices import WIB, session_closed  # noqa: E402
from sectors_client import SectorsClient  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "panel"
WINDOW = 89  # API caps at 90 days; leave a day of slack
OVERLAP = 5  # days re-fetched before the last date on disk, so a revised close is picked up


def read_existing(path: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out[r["date"]] = float(r["close"])
            except (KeyError, TypeError, ValueError):
                pass
    return out


def panel_dates() -> list[str]:
    """Every session the panel holds a price for. The benchmark is only ever read at these."""
    seen: set[str] = set()
    for f in OUT.glob("prices-*.csv.gz"):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            rd = csv.reader(fh)
            next(rd, None)
            seen.update(row[0] for row in rd if row)
    return sorted(seen)


def lag_sessions(path: Path) -> int:
    """Panel sessions after the benchmark's last date. 0 means current."""
    have = read_existing(path)
    if not have:
        return len(panel_dates())
    last = max(have)
    return sum(1 for d in panel_dates() if d > last)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--index", default="ihsg")
    ap.add_argument("--incremental", action="store_true",
                    help="top up the file on disk from its own last date instead of "
                         "re-fetching the whole history (1 credit instead of ~9)")
    ap.add_argument("--lag", action="store_true",
                    help="print how many panel sessions the benchmark is behind, and stop")
    args = ap.parse_args()

    path = OUT / f"benchmark-{args.index}.csv"
    if args.lag:
        print(lag_sessions(path))
        return 0

    sec = SectorsClient()
    if not sec.enabled:
        print("SECTORS_API_KEY not set")
        return 2

    end = date.today()
    closes: dict[str, float] = read_existing(path) if args.incremental else {}
    if closes:
        start = date.fromisoformat(max(closes)) - timedelta(days=OVERLAP)
        print(f"index  : {args.index}\nmode   : incremental, {len(closes):,} rows on disk "
              f"to {max(closes)}\nwindow : {start} -> {end}")
    else:
        start = end - timedelta(days=int(365 * args.years))
        print(f"index  : {args.index}\nwindow : {start} -> {end}")

    # Today's level is still moving until pre-closing ends at 16:15 WIB. A manual run at
    # midday must not write it as a close.
    today = end.isoformat()
    open_today = not session_closed(datetime.now(WIB).timestamp())

    fetched = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=WINDOW), end)
        rows = sec.index_daily(args.index, cursor.isoformat(), chunk_end.isoformat())
        got = 0
        for r in rows or []:
            d = str(r.get("date"))[:10]
            # The index endpoint calls the level `price`, not `close`.
            v = r.get("price", r.get("close"))
            if d and v is not None and not (d == today and open_today):
                try:
                    closes[d] = float(v)
                    got += 1
                except (TypeError, ValueError):
                    pass
        print(f"  {cursor} -> {chunk_end}: {got} rows")
        fetched += got
        cursor = chunk_end + timedelta(days=1)

    if not fetched:
        print("\nNo data returned — nothing written.")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
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

    # Read against the panel's own calendar, because that is the only place the series is
    # used: a session the panel has and the benchmark lacks drops every event that enters
    # or exits on it, without a word.
    panel = panel_dates()
    holes = [d for d in panel if days[0] <= d <= days[-1] and d not in closes]
    behind = sum(1 for d in panel if d > days[-1])
    print(f"  panel  : {behind} session(s) behind, {len(holes)} panel session(s) missing inside the span"
          + (f" ({', '.join(holes[:8])}{' ...' if len(holes) > 8 else ''})" if holes else ""))

    # A 2-year window should hold ~480 trading days. Materially fewer means gaps, which
    # would silently drop events from the backtest rather than fail loudly.
    expected = args.years * 240
    if not args.incremental and len(closes) < expected * 0.9:
        print(f"\n  WARNING: expected ~{expected:.0f} trading days, got {len(closes)}. "
              f"Check for gaps before scoring.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
