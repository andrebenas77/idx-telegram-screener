#!/usr/bin/env python3
"""Backfill hourly (tf=60) bars for the panel — the instrument for thesis #12.

Pre-registered in `reference/dispersion.md`. One request per symbol buys 469 sessions
(~2 years), which is the whole check-0-PASSING window; the m5 store covers only 116
sessions and sits entirely inside the window where check 0 FAILS.

Writes `data/intraday/h60-{SYM}.csv.gz`. A NEW prefix — the m5 store is not touched.

Cost control, because this repo has already lost 29,000 requests in one afternoon to a
retry loop with no ceiling:
  - /usage/api (free) is read first and the job REFUSES below QUOTA_FLOOR
  - MAX_REQUESTS is a hard stop, checked every iteration
  - the client's own retry ladder is 2 and returns None rather than looping
  - use_cache=False: the day-scoped cache rewrites the WHOLE file per request and would
    go quadratic across 159 payloads of ~0.5MB

    py scripts/fetch_hourly.py --dry-run     # show what would be fetched, 0 requests
    py scripts/fetch_hourly.py
"""

from __future__ import annotations

import argparse
import csv
import gzip
import socket
import sys
from pathlib import Path

import urllib3.util.connection as u3c

sys.path.insert(0, str(Path(__file__).resolve().parent))

# IPv6 to api.invezgo.com is blackholed on this box (43.0s vs 0.16s). Must be set BEFORE
# any connection pool is created. See reference/invezgo.md.
u3c.allowed_gai_family = lambda: socket.AF_INET  # noqa: E731

from alpha_lib import PANEL, Panel  # noqa: E402
from intraday_lib import parse_payload  # noqa: E402
from invezgo_client import InvezgoClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INTRADAY = ROOT / "data" / "intraday"
TIMEFRAME = 60
FROM, TO = "2024-09-01", "2026-08-26"     # tf=60 retention is 2 years; outside it is a free 422
QUOTA_FLOOR = 2000
MAX_REQUESTS = 200                        # hard ceiling: 159 symbols + slack
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def path_for(sym: str) -> Path:
    return INTRADAY / f"h60-{sym}.csv.gz"


def write_bars(sym: str, bars) -> int:
    INTRADAY.mkdir(parents=True, exist_ok=True)
    with gzip.open(path_for(sym), "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "hhmm", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.date, b.hhmm, b.o, b.h, b.l, b.c, b.v])
    return len(bars)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="refetch symbols already on disk")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    p = Panel()
    p.load()
    syms = sorted(p.raw_close)
    todo = [s for s in syms if args.force or not path_for(s).exists()]
    on_disk = len(syms) - len(todo)          # computed BEFORE --limit, or it misreports
    if args.limit:
        todo = todo[:args.limit]

    print(f"panel symbols: {len(syms)}   already on disk: {on_disk}   to fetch: {len(todo)}")
    print(f"range {FROM} .. {TO}  timeframe={TIMEFRAME}  -> data/intraday/h60-*.csv.gz")
    if args.dry_run:
        print("dry run — no requests made")
        return 0
    if len(todo) > MAX_REQUESTS:
        print(f"[!!] {len(todo)} symbols exceeds MAX_REQUESTS={MAX_REQUESTS}. Refusing.")
        return 3

    c = InvezgoClient(user_agent=BROWSER_UA, use_cache=False, verbose=False)
    u = c.api_usage() or {}
    rem = u.get("remaining")
    print(f"quota: {u.get('usage')}/{u.get('limit')} used, {rem} remaining, role {u.get('role')}")
    if rem is None or rem < QUOTA_FLOOR:
        print(f"[!!] below the {QUOTA_FLOOR} floor. Refusing to start.")
        return 3

    ok = empty = failed = 0
    total_bars = 0
    for n, sym in enumerate(todo, 1):
        if c.requests_used >= MAX_REQUESTS:
            print(f"[!!] hit MAX_REQUESTS={MAX_REQUESTS}, stopping at {sym}")
            break
        rows = c.multi_time_chart(sym, FROM, TO, timeframe=TIMEFRAME)
        if not isinstance(rows, list) or not rows:
            print(f"  [{n:>3}/{len(todo)}] {sym:<8} no data")
            empty += 1
            continue
        # parse_payload dedupes on (date, hhmm) — every bar comes back EXACTLY TWICE,
        # byte-identical, and a doubled volume changes every dispersion estimate.
        bars = parse_payload(rows)
        if not bars:
            print(f"  [{n:>3}/{len(todo)}] {sym:<8} unparseable ({len(rows)} raw rows)")
            failed += 1
            continue
        nb = write_bars(sym, bars)
        total_bars += nb
        ok += 1
        if n % 20 == 0 or n == len(todo):
            sessions = len({b.date for b in bars})
            print(f"  [{n:>3}/{len(todo)}] {sym:<8} {nb:>5} bars / {sessions:>3} sessions "
                  f"(raw {len(rows)}, dedupe {1 - nb / max(1, len(rows)):.0%})  "
                  f"spent {c.requests_used}")

    u2 = c.api_usage() or {}
    print(f"\nok {ok}   empty {empty}   failed {failed}   bars written {total_bars:,}")
    print(f"requests used this run: {c.requests_used}")
    print(f"quota now: {u2.get('usage')}/{u2.get('limit')}, {u2.get('remaining')} remaining")
    if c.errors:
        print(f"errors ({len(c.errors)}): {c.errors[:5]}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
