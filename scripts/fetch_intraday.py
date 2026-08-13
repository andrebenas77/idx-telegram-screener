#!/usr/bin/env python3
"""Backfill 5-minute bars for the panel universe. The only caller of multi-time.

One request per symbol covers the endpoint's whole ~114-session history, which is what
makes a 112-name backfill cost 0.4% of a 30,000/MONTH quota instead of exhausting it.
The quota is shared silently with every other Invezgo call on this machine, including
MCP tools, so this script prints its running usage and refuses to start if headroom is
thin.

Verification is not optional here. `--verify` rebuilds each daily O/H/L/C/volume from
the 5-minute bars and compares against `data/panel/prices-*.csv.gz`. If the intraday
feed disagrees with the daily panel, every intraday backtest result is measuring the
disagreement rather than the strategy.

Usage:
    py scripts/fetch_intraday.py --probe-index      # find the index code (2-3 requests)
    py scripts/fetch_intraday.py --universe --from 2026-02-11 --to 2026-08-11
    py scripts/fetch_intraday.py --verify
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import Panel  # noqa: E402
from intraday_lib import (BROWSER_UA, INTRADAY, Bar, bars_path, parse_payload,  # noqa: E402
                          pin_ipv4, read_bars, write_bars)
from invezgo_client import InvezgoClient  # noqa: E402

# Candidates for the composite index on this endpoint. The code is not documented and
# G1 (the regime gate) is inoperable without it, so we probe rather than guess.
INDEX_CANDIDATES = ["COMPOSITE", "IHSG", "JKSE", "IDX", "^JKSE", "COMPOSITEINDEX"]
QUOTA_FLOOR = 2000          # refuse to start a bulk job below this much headroom


def client() -> InvezgoClient:
    pin_ipv4()
    return InvezgoClient(user_agent=BROWSER_UA, use_cache=False, verbose=False)


def quota(c: InvezgoClient) -> dict | None:
    u = c.api_usage()
    return u if isinstance(u, dict) else None


def fetch_one(c: InvezgoClient, sym: str, start: str, end: str) -> list[Bar]:
    rows = c.multi_time_chart(sym, start, end, 5)
    if not isinstance(rows, list):
        return []
    return parse_payload(rows)


def cmd_probe_index(a) -> int:
    c = client()
    q = quota(c)
    if q:
        print(f"quota: {q.get('usage')}/{q.get('limit')} used, {q.get('remaining')} left")
    print("probing for the composite index code (G1 needs it)...")
    for code in INDEX_CANDIDATES:
        bars = fetch_one(c, code, a.start, a.end)
        if bars:
            days = sorted({b.date for b in bars})
            print(f"  [ok] {code:<16} {len(bars):>6} bars over {len(days)} sessions "
                  f"({days[0]}..{days[-1]})")
            print(f"       sample: {bars[0].hhmm} c={bars[0].c} v={bars[0].v} | "
                  f"{bars[-1].hhmm} c={bars[-1].c} v={bars[-1].v}")
            has_vol = any(b.v for b in bars)
            print(f"       volume present: {has_vol} -> regime gate uses "
                  f"{'VWAP' if has_vol else 'TWAP'}")
            write_bars(code, bars)
            print(f"       wrote {bars_path(code)}")
            return 0
        print(f"  [--] {code:<16} no data")
    print("[!!] no index code responded. G1 will have to fall back to Yahoo ^JKSE "
          "(free, but volume-less, so TWAP either way).")
    return 1


def cmd_universe(a) -> int:
    c = client()
    q = quota(c)
    if q:
        rem = q.get("remaining", 0)
        print(f"quota: {q.get('usage')}/{q.get('limit')} used, {rem} remaining "
              f"(resets {q.get('expire', '?')[:10]})")
        if rem < QUOTA_FLOOR:
            print(f"[!!] only {rem} requests left, below the {QUOTA_FLOOR} floor. "
                  f"Refusing to start a bulk backfill.")
            return 1

    p = Panel()
    p.load_prices()
    syms = sorted(p.close)
    if a.symbols:
        syms = [s.upper() for s in a.symbols]
    print(f"backfilling {len(syms)} symbols, {a.start}..{a.end}, timeframe 5m")
    print(f"  -> {INTRADAY}")

    ok = skipped = failed = 0
    t0 = time.time()
    for n, sym in enumerate(syms, 1):
        if bars_path(sym).exists() and not a.force:
            skipped += 1
            continue
        bars = fetch_one(c, sym, a.start, a.end)
        if not bars:
            failed += 1
            print(f"  [{n:>3}/{len(syms)}] {sym:<6} no data")
            continue
        write_bars(sym, bars)
        ok += 1
        days = len({b.date for b in bars})
        if n % 10 == 0 or n == len(syms):
            print(f"  [{n:>3}/{len(syms)}] {sym:<6} {len(bars):>6} bars / {days:>3} sessions "
                  f"| used {c.requests_used} req | {time.time() - t0:.0f}s")
    print(f"\n[ok] {ok} written, {skipped} already present, {failed} empty "
          f"| {c.requests_used} requests used")
    q = quota(c)
    if q:
        print(f"quota now: {q.get('usage')}/{q.get('limit')}, {q.get('remaining')} remaining")
    return 0 if ok or skipped else 1


def cmd_verify(a) -> int:
    """Rebuild daily bars from 5-minute bars and reconcile against the daily panel.

    A tolerance is allowed on volume only. O/H/L/C must match exactly: these are
    printed levels, and if they disagree the two feeds are describing different
    sessions, which invalidates any study that joins them.

    Reconciliation reads bars WITH the 08:55 pre-opening auction, because the daily
    panel's high/low include it — the auction print is the session extreme often
    enough that excluding it puts 5% of sessions one tick apart. The analysis path
    still excludes it; see read_bars().
    """
    print("loading daily panel...")
    p = Panel()
    p.load_prices()

    files = sorted(INTRADAY.glob("m5-*.csv.gz"))
    if not files:
        print(f"[!!] no cached bars in {INTRADAY} — run --universe first")
        return 1
    print(f"reconciling {len(files)} symbols against prices-*.csv.gz\n")

    tot_days = mism_ohlc = mism_vol = missing = 0
    bad: list[str] = []
    for f in files:
        sym = f.stem.replace("m5-", "").replace(".csv", "")
        days = read_bars(sym, include_auction=True)
        hi, lo, cl, vo = (p.high.get(sym) or {}), (p.low.get(sym) or {}), \
            (p.raw_close.get(sym) or {}), (p.volume.get(sym) or {})
        for d, bars in days.items():
            i = p.didx.get(d)
            if i is None or i not in cl:
                missing += 1
                continue
            tot_days += 1
            h5, l5, c5 = max(b.h for b in bars), min(b.l for b in bars), bars[-1].c
            v5 = sum(b.v for b in bars)
            if not (h5 == hi.get(i) and l5 == lo.get(i) and c5 == cl.get(i)):
                mism_ohlc += 1
                if len(bad) < 8:
                    bad.append(f"    {sym} {d}: 5m H/L/C {h5:g}/{l5:g}/{c5:g} vs "
                               f"panel {hi.get(i)}/{lo.get(i)}/{cl.get(i)}")
            pv = vo.get(i) or 0
            if pv and abs(v5 - pv) / pv > 0.02:
                mism_vol += 1
    print(f"  sessions compared : {tot_days:,}")
    print(f"  O/H/L/C mismatch  : {mism_ohlc:,} ({mism_ohlc / tot_days:.2%})" if tot_days else "")
    print(f"  volume >2% apart  : {mism_vol:,} ({mism_vol / tot_days:.2%})" if tot_days else "")
    print(f"  not in daily panel: {missing:,} (expected — panel ends before the feed does)")
    for b in bad:
        print(b)
    if tot_days and mism_ohlc / tot_days > 0.02:
        print("\n[!!] more than 2% of sessions disagree on printed levels. "
              "Do not run the intraday backtest until this is understood.")
        return 1
    print("\n[ok] the 5-minute feed reconciles with the daily panel")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe-index", action="store_true")
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--from", dest="start", default="2026-02-01")
    ap.add_argument("--to", dest="end", default="2026-08-11")
    ap.add_argument("--force", action="store_true", help="refetch symbols already cached")
    a = ap.parse_args()
    if a.probe_index:
        return cmd_probe_index(a)
    if a.universe:
        return cmd_universe(a)
    if a.verify:
        return cmd_verify(a)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
