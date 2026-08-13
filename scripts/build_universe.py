#!/usr/bin/env python3
"""Point-in-time top-N-by-value universe for the accumulation board.

WHY THIS EXISTS
    Every board in this repo has until now drawn its universe from
    `reference/tickers.csv` filtered to `liquid=1` — 113 hand-curated names. That file is
    a Telegram ALIAS DICTIONARY: it exists so chatter matching can turn "Barito
    Renewables" into BREN. It was never a liquidity screen, and it is maintained by hand.

    DSSA is not in it. DSSA ran on 2026-08-12 and no board could have scored it, at any
    threshold, because it was not in the panel at all. That is not a tuning failure, it
    is a universe failure, and no amount of feature work fixes it.

    So the universe here is DERIVED FROM TRADED VALUE and dated:

        hot_list(D) = symbols ranked top-N by daily value (close * volume)
                      on at least one of the trailing W sessions ending D

    W defaults to 40 sessions (~2 calendar months) because IDX themes are short-lived —
    DSSA only became active in the month before its move, and a 12-month lookback would
    have carried a long tail of names that stopped mattering while still missing it.

WHY THE UNIVERSE IS STORED AND NOT RECOMPUTED
    `universe-YYYY-MM.csv.gz` is a dated fact, written once and committed. Recomputing
    membership at board-build time would silently re-fit the universe on every run, and
    a backtest over a re-fitted universe measures the fitting, not the strategy.

COST
    Discovery is 4 Sectors credits for a whole window (most-traded is per-date over a
    range, not per-date-per-call). Ranking reuses `data/panel/prices-*.csv.gz`, which is
    already on disk. Only genuinely new names cost an Invezgo request each.

Usage:
    py build_universe.py --end 2026-08-12                 # rank + write + churn report
    py build_universe.py --end 2026-08-12 --no-discover   # panel only, zero credits
    py build_universe.py --end 2026-08-12 --dry-run       # report, write nothing
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import Panel  # noqa: E402
from invezgo_client import InvezgoClient  # noqa: E402
from sectors_client import SectorsClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "panel"
TICKERS = ROOT / "reference" / "tickers.csv"
WIB = timezone(timedelta(hours=7))

TOP_N = 20           # the board's universe: "not interested below top 20"
LOOKBACK = 40        # sessions in the rolling membership window (~2 months)
CHURN_ALARM = 0.30   # above this, membership is unstable and downstream is noise


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ sources

def from_panel(p: Panel) -> dict[str, dict[str, float]]:
    """{date: {symbol: (value, volume)}} from the on-disk panel. Free."""
    out: dict[str, dict[str, tuple]] = defaultdict(dict)
    for sym, series in p.turnover.items():
        vols = p.volume.get(sym, {})
        for i, val in series.items():
            if val and val > 0:
                out[p.dates[i]][sym] = (val, vols.get(i, 0.0))
    return out


def discover(sc: SectorsClient, start: str, end: str) -> tuple[set[str], dict]:
    """Names that were most-traded in the window but may be outside the panel.

    Both rankings are pulled because they disagree hard: on 2026-08-12 `adjusted=True`
    (value) gave CUAN/TPIA/PTRO while `adjusted=False` (raw share volume) gave
    BUMI/IATA/JGLE — almost disjoint universes. A value-ranked board still wants the
    volume list in the POOL, because a name can lead on volume today and on value
    tomorrow, and discovering it a day late is the whole failure mode being fixed.

    n_stock is capped at 10 by the API, so this is discovery only. The exact top-20
    ranking is computed from price data, not taken from here.
    """
    found: set[str] = set()
    truth: dict[str, list[str]] = {}
    for adjusted in (True, False):
        payload = sc.most_traded(start=start, end=end, n_stock=10, adjusted=adjusted)
        if not payload:
            continue
        if isinstance(payload, dict):
            for d, rows in payload.items():
                day = str(d)[:10]
                names = []
                for r in rows if isinstance(rows, list) else []:
                    sym = str(r.get("symbol") or "").upper().removesuffix(".JK")
                    if sym:
                        found.add(sym)
                        names.append(sym)
                if adjusted and names:
                    truth[day] = names        # the API's own top-10 by value
    return found, truth


def fetch_missing(ic: InvezgoClient, syms: list[str], start: str, end: str,
                  into: dict[str, dict[str, tuple]]) -> list[str]:
    """One inventory_chart request per new name buys its whole price+volume history."""
    ok = []
    for i, sym in enumerate(syms, 1):
        payload = ic.inventory_chart(sym, start=start, end=end, scope="val", limit=1)
        rows = (payload or {}).get("price") or []
        n = 0
        for r in rows:
            d = str(r.get("date"))[:10]
            cl, vol = f(r.get("close")), f(r.get("volume"))
            if d and cl and vol and cl > 0 and vol > 0:
                into[d][sym] = (cl * vol, vol)
                n += 1
        if n:
            ok.append(sym)
        print(f"  [{i:>3}/{len(syms)}] {sym:<6} {n:>4} sessions"
              f"{'' if n else '   (no data)'}")
    return ok


# ------------------------------------------------------------------ ranking

def rank_days(byday: dict[str, dict[str, tuple]]) -> dict[str, list[dict]]:
    """Per date, rank the pool by value and by volume."""
    out = {}
    for d, syms in byday.items():
        by_val = sorted(syms.items(), key=lambda kv: -kv[1][0])
        by_vol = sorted(syms.items(), key=lambda kv: -kv[1][1])
        vol_rank = {s: i + 1 for i, (s, _) in enumerate(by_vol)}
        out[d] = [{"symbol": s, "rank_value": i + 1, "rank_volume": vol_rank[s],
                   "value_idr": v[0], "volume_sh": v[1]}
                  for i, (s, v) in enumerate(by_val)]
    return out


def hot_list(ranked: dict[str, list[dict]], end: str, lookback: int,
             top_n: int) -> tuple[list[str], list[str]]:
    """Symbols that were top-N by value on >=1 of the trailing `lookback` sessions."""
    sessions = sorted(d for d in ranked if d <= end)[-lookback:]
    hot: set[str] = set()
    for d in sessions:
        hot.update(r["symbol"] for r in ranked[d][:top_n])
    return sorted(hot), sessions


def churn(ranked: dict[str, list[dict]], sessions: list[str], top_n: int) -> float:
    """Mean day-over-day turnover of the top-N set.

    A universe that replaces a third of itself daily is not a universe, it is a noise
    generator, and every downstream statistic inherits that. Measure it BEFORE paying
    for a backfill against it.
    """
    prev, rates = None, []
    for d in sessions:
        cur = {r["symbol"] for r in ranked[d][:top_n]}
        if prev:
            rates.append(len(cur - prev) / max(1, len(cur)))
        prev = cur
    return sum(rates) / len(rates) if rates else 0.0


def write_partitions(ranked: dict[str, list[dict]], top_n: int, out_dir: Path) -> int:
    """One gzipped CSV per month, matching the panel's partitioning convention."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for d, rows in ranked.items():
        for r in rows[:top_n * 3]:      # keep 3x depth: rank 21-60 is the promotion zone
            buckets[d[:7]].append({"date": d, **r})
    n = 0
    for month, rows in buckets.items():
        path = out_dir / f"universe-{month}.csv.gz"
        rows.sort(key=lambda r: (r["date"], r["rank_value"]))
        with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "symbol", "rank_value",
                                               "rank_volume", "value_idr", "volume_sh"])
            w.writeheader()
            for r in rows:
                w.writerow({k: (f"{v:.0f}" if isinstance(v, float) else v)
                            for k, v in r.items()})
        n += len(rows)
    return n


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--end", default=None, help="last session (default: today)")
    ap.add_argument("--lookback", type=int, default=LOOKBACK)
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--no-discover", action="store_true",
                    help="panel only; spend no Sectors credits")
    ap.add_argument("--max-new", type=int, default=60,
                    help="cap Invezgo requests for newly discovered names")
    ap.add_argument("--discover-only", action="store_true",
                    help="write the candidate pool and stop. Pass 1 of the two-pass "
                         "build: discovery costs 4 Sectors credits and no Invezgo "
                         "requests, then backfill_panel.py --from-universe pulls price "
                         "AND flows for the pool in one request per name. Fetching "
                         "prices here as well would pay twice for the same series.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    end = args.end or date.today().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=int(args.lookback * 1.8))).isoformat()

    print(f"universe: top-{args.top_n} by value, {args.lookback}-session lookback "
          f"ending {end}")

    p = Panel().load()
    byday = from_panel(p)
    print(f"  panel: {len(p.turnover)} symbols, {len(p.dates)} sessions "
          f"({p.dates[0]} .. {p.dates[-1]})")

    curated = set()
    if TICKERS.exists():
        curated = {r["ticker"].strip().upper()
                   for r in csv.DictReader(TICKERS.open(encoding="utf-8"))}

    truth: dict[str, list[str]] = {}
    pool = sorted(set(p.turnover))
    if not args.no_discover:
        sc = SectorsClient(date=end)
        if sc.enabled:
            found, truth = discover(sc, start, end)
            new = sorted(found - set(p.turnover))
            pool = sorted(set(p.turnover) | found)
            print(f"  discovered {len(found)} most-traded names, "
                  f"{len(new)} outside the panel: {', '.join(new[:20]) or '-'}")
            missing_from_curated = sorted(found - curated)
            if missing_from_curated:
                print(f"  NOT in reference/tickers.csv at all: "
                      f"{', '.join(missing_from_curated)}")
            if args.discover_only:
                PANEL.mkdir(parents=True, exist_ok=True)
                (PANEL / "universe_pool.json").write_text(json.dumps({
                    "generated_at": datetime.now(WIB).isoformat(),
                    "end": end, "window": {"start": start, "end": end},
                    "pool": pool, "pool_size": len(pool),
                    "discovered": sorted(found), "new_to_panel": new,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                sc.report()
                print(f"\n  --discover-only: wrote pool of {len(pool)} names to "
                      f"{PANEL}/universe_pool.json")
                print(f"  next: py backfill_panel.py --from-universe --incremental")
                return 0
            if new:
                ic = InvezgoClient(date=end)
                if ic.enabled:
                    fetch_missing(ic, new[:args.max_new], start, end, byday)
                    ic.report()
            sc.report()
        else:
            print("  SECTORS_API_KEY not set — discovery skipped")

    ranked = rank_days(byday)
    hot, sessions = hot_list(ranked, end, args.lookback, args.top_n)
    ch = churn(ranked, sessions, args.top_n)

    print(f"\n  sessions in window : {len(sessions)} "
          f"({sessions[0] if sessions else '-'} .. {sessions[-1] if sessions else '-'})")
    print(f"  hot list size      : {len(hot)} names")
    print(f"  top-{args.top_n} daily churn : {ch * 100:.1f}%"
          + ("   <-- UNSTABLE, downstream statistics are noise"
             if ch > CHURN_ALARM else "   (stable)"))

    # Pool completeness: if our own ranking cannot reproduce the API's top-10 by value,
    # the pool is missing names and the top-20 is wrong in a way no threshold can fix.
    if truth:
        checked = miss = 0
        examples = []
        for d, names in truth.items():
            if d not in ranked:
                continue
            checked += 1
            ours = {r["symbol"] for r in ranked[d][:10]}
            gap = set(names) - ours
            if gap:
                miss += 1
                if len(examples) < 3:
                    examples.append(f"{d}: {', '.join(sorted(gap))}")
        if checked:
            print(f"  pool completeness  : reproduced the API top-10 on "
                  f"{checked - miss}/{checked} days"
                  + (f"   gaps -> {' | '.join(examples)}" if examples else ""))

    if sessions:
        last = ranked[sessions[-1]][:args.top_n]
        print(f"\n  TOP {args.top_n} BY VALUE on {sessions[-1]}")
        for r in last:
            print(f"    {r['rank_value']:>3}. {r['symbol']:<6} "
                  f"Rp{r['value_idr'] / 1e9:>8.1f}bn   vol {r['volume_sh'] / 1e6:>8.1f}m"
                  f"   (vol rank {r['rank_volume']})")

    for name in ("BREN", "PTRO", "CUAN", "DSSA"):
        where = [d for d in sessions
                 if name in {r["symbol"] for r in ranked.get(d, [])[:args.top_n]}]
        print(f"  {name:<5} in top-{args.top_n} on {len(where)}/{len(sessions)} sessions"
              + (f"   latest {where[-1]}" if where else "   NEVER — still invisible"))

    if args.dry_run:
        print("\n  --dry-run: nothing written")
        return 0

    PANEL.mkdir(parents=True, exist_ok=True)
    n = write_partitions(ranked, args.top_n, PANEL)
    (PANEL / "universe_report.json").write_text(json.dumps({
        "generated_at": datetime.now(WIB).isoformat(),
        "end": end, "lookback": args.lookback, "top_n": args.top_n,
        "hot_list": hot, "hot_list_size": len(hot),
        "sessions": len(sessions), "churn": ch,
        "rows_written": n,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  wrote {n:,} rows to {PANEL}/universe-*.csv.gz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
