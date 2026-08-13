#!/usr/bin/env python3
"""Build the gross broker partition — the buy/sell/freq dimension the panel lacks.

WHY
    `data/panel/flows-*.csv.gz` stores `date,symbol,broker,net_value` and nothing else.
    The accumulation thesis rests on `buy / (buy + sell)`, and A RATIO IS NOT RECOVERABLE
    FROM A DIFFERENCE. On BREN over 05->12 Aug, CC netted +33.7bn on 132.1bn of buying
    and 98.5bn of selling (57% — market-making churn) while DX netted +55.5bn on 56.4bn
    of buying and 0.9bn of selling (98% — a real accumulator). Ranked by net they sit
    side by side. No feature work recovers the difference; the data has to be pulled.

WHY SECTORS AND NOT INVEZGO
    Sectors `broker-summary` returns every broker's book GROUPED BY DAY across a 14-day
    window for ONE credit. Invezgo `summary-stock` returns the same fields but AGGREGATED
    over the window with no daily dimension, so a per-day series costs one request per
    stock-day. For 41 names over 40 sessions that is ~123 Sectors credits versus ~1,640
    Invezgo requests. Invezgo is used only for the reconciliation sample below.

THE RECONCILIATION IS THE POINT
    Two vendors, two units (Sectors reports LOTS, Invezgo reports SHARES), two board
    conventions. If `buy_value - sell_value` does not reproduce the existing panel's
    `net_value`, then everything downstream measures the disagreement rather than the
    market. This script REFUSES TO WRITE above a 0.5% mismatch rate rather than
    producing a partition that looks fine and is not.

Usage:
    py backfill_gross.py --end 2026-08-12 --days 40        # the hot list
    py backfill_gross.py --symbols BREN,PTRO --days 20
    py backfill_gross.py --end 2026-08-12 --dry-run        # cost estimate only
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

from sectors_client import SectorsClient  # noqa: E402
from invezgo_client import InvezgoClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "panel"
WIB = timezone(timedelta(hours=7))

LOT = 100                    # IDX: 1 lot = 100 shares
MISMATCH_LIMIT = 0.005       # 0.5% — above this the partition is not written
FIELDS = ["date", "symbol", "broker", "buy_value", "sell_value",
          "buy_lot", "sell_lot", "buy_freq", "sell_freq", "buy_avg", "sell_avg"]


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_hot_list() -> list[str]:
    p = PANEL / "universe_report.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("hot_list", [])
    except Exception:
        return []


def load_panel_net(symbols: set[str]) -> dict[tuple[str, str, str], float]:
    """Existing net_value from flows-*.csv.gz, keyed (date, symbol, broker)."""
    out: dict[tuple[str, str, str], float] = {}
    for path in sorted(PANEL.glob("flows-*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["symbol"] in symbols:
                    v = f(r["net_value"])
                    if v is not None:
                        out[(r["date"], r["symbol"], r["broker"])] = v
    return out


def pull_symbol(sc: SectorsClient, sym: str, start: str, end: str) -> list[dict]:
    """One symbol's per-broker-day gross across the span.

    broker_summary_range() walks the window the API actually grants, taking each next
    step from the ECHOED `start` rather than assuming 14 days — the cap clamps silently,
    so an assumed step size leaves holes that never surface.
    """
    rows: list[dict] = []
    for day in sc.broker_summary_range(sym, start, end):
        d = str(day.get("date"))[:10]
        for b in day.get("summary") or []:
            code = str(b.get("broker_code") or "").strip().upper()
            if not code:
                continue
            rows.append({
                "date": d, "symbol": sym, "broker": code,
                "buy_value": f(b.get("bval")) or 0.0,
                "sell_value": f(b.get("sval")) or 0.0,
                "buy_lot": f(b.get("blot")) or 0.0,
                "sell_lot": f(b.get("slot")) or 0.0,
                "buy_freq": f(b.get("bfreq")) or 0.0,
                "sell_freq": f(b.get("sfreq")) or 0.0,
                # navg_per_share is a DECOY — a verbatim copy of bavg when nval > 0 and
                # of savg when nval < 0. It is not a net average price. Not stored.
                "buy_avg": f(b.get("bavg_per_share")),
                "sell_avg": f(b.get("savg_per_share")),
            })
    return rows


def reconcile(rows: list[dict], panel_net: dict) -> dict:
    """Does buy - sell reproduce the panel's net_value?

    Checks the SIGNED value, not the magnitude: a lots/shares mixup would scale by 100
    and a board-segment mismatch (RG vs RG+NG) would shift only some rows, and both show
    up here before they can quietly poison a backtest.
    """
    checked = bad = 0
    worst = []
    for r in rows:
        key = (r["date"], r["symbol"], r["broker"])
        ref = panel_net.get(key)
        if ref is None or abs(ref) < 1e6:      # skip trivially small rows
            continue
        checked += 1
        got = r["buy_value"] - r["sell_value"]
        rel = abs(got - ref) / max(abs(ref), 1.0)
        if rel > 0.01:
            bad += 1
            if len(worst) < 5:
                worst.append(f"{r['symbol']}/{r['broker']}/{r['date']}: "
                             f"gross {got:,.0f} vs panel {ref:,.0f} ({rel * 100:.1f}%)")
    return {"checked": checked, "mismatched": bad,
            "rate": (bad / checked) if checked else None, "examples": worst}


def sample_invezgo(ic: InvezgoClient, rows: list[dict], n: int = 8) -> dict:
    """Cross-vendor spot check on a handful of stock-days.

    Sectors reports LOTS, Invezgo reports SHARES. Comparing VALUE sidesteps the unit
    question entirely, which is what makes this a real check rather than a restatement
    of an assumption.
    """
    by_day: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_day[(r["symbol"], r["date"])].append(r)
    picks = sorted(by_day)[::max(1, len(by_day) // n)][:n]

    out = []
    for sym, d in picks:
        payload = ic.broker_summary_stock(sym, start=d, end=d, market="RG")
        inv = payload if isinstance(payload, list) else (payload or {}).get("data") or []
        inv_by = {str(x.get("code") or "").upper(): f(x.get("buy_value")) or 0.0
                  for x in inv}
        if not inv_by:
            continue
        sec_total = sum(r["buy_value"] for r in by_day[(sym, d)])
        inv_total = sum(inv_by.values())
        if inv_total:
            out.append({"symbol": sym, "date": d, "sectors_buy": sec_total,
                        "invezgo_buy": inv_total,
                        "rel_gap": abs(sec_total - inv_total) / inv_total})
    gaps = [o["rel_gap"] for o in out]
    return {"samples": out, "max_gap": max(gaps) if gaps else None,
            "mean_gap": (sum(gaps) / len(gaps)) if gaps else None}


def write_partitions(rows: list[dict]) -> int:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[r["date"][:7]].append(r)
    n = 0
    for month, rs in buckets.items():
        path = PANEL / f"gross-{month}.csv.gz"
        existing: dict[tuple, dict] = {}
        if path.exists():                       # merge, never truncate
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for old in csv.DictReader(fh):
                    existing[(old["date"], old["symbol"], old["broker"])] = old
        for r in rs:
            existing[(r["date"], r["symbol"], r["broker"])] = r
        merged = [existing[k] for k in sorted(existing)]
        with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            w.writeheader()
            for r in merged:
                w.writerow({k: r.get(k, "") for k in FIELDS})
        n += len(merged)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-reconcile", action="store_true")
    ap.add_argument("--credit-ceiling", type=int, default=400,
                    help="refuse to start if the estimate exceeds this")
    args = ap.parse_args()

    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            if args.symbols else load_hot_list())
    if not syms:
        print("no symbols — run build_universe.py first, or pass --symbols",
              file=sys.stderr)
        return 1

    end = args.end
    start = (date.fromisoformat(end) - timedelta(days=int(args.days * 1.5))).isoformat()
    windows = max(1, round(args.days * 1.5 / 14 + 0.5))
    est = len(syms) * windows

    print(f"gross backfill: {len(syms)} symbols, {start} .. {end}")
    print(f"  estimate ~{est} Sectors credits ({windows} windows x {len(syms)} names)")
    if est > args.credit_ceiling:
        print(f"  ABOVE the {args.credit_ceiling}-credit ceiling — narrow --days or "
              f"--symbols, or raise --credit-ceiling deliberately", file=sys.stderr)
        return 1
    if args.dry_run:
        print("  --dry-run: nothing fetched")
        return 0

    sc = SectorsClient(date=end)
    if not sc.enabled:
        print("SECTORS_API_KEY not set", file=sys.stderr)
        return 1

    rows: list[dict] = []
    failed: list[str] = []
    for i, sym in enumerate(syms, 1):
        got = pull_symbol(sc, sym, start, end)
        if not got:
            failed.append(sym)
        rows.extend(got)
        days = len({r["date"] for r in got})
        print(f"  [{i:>3}/{len(syms)}] {sym:<6} {len(got):>6} broker-days "
              f"over {days:>3} sessions   credits={sc.credits}")

    if not rows:
        print("nothing fetched", file=sys.stderr)
        return 1

    rec = reconcile(rows, load_panel_net({r["symbol"] for r in rows})) \
        if not args.no_reconcile else {"checked": 0, "rate": None, "examples": []}

    print(f"\n  broker-days     : {len(rows):,}")
    print(f"  symbols ok      : {len(syms) - len(failed)}/{len(syms)}"
          + (f"   failed: {', '.join(failed)}" if failed else ""))
    print(f"  sessions        : {len({r['date'] for r in rows})}")
    print(f"  brokers         : {len({r['broker'] for r in rows})}")
    if rec["checked"]:
        print(f"  reconciliation  : {rec['mismatched']}/{rec['checked']} rows differ "
              f"from panel net by >1%  ({(rec['rate'] or 0) * 100:.2f}%)")
        for e in rec["examples"]:
            print(f"      {e}")
    else:
        print("  reconciliation  : skipped (no overlapping panel rows)")

    if rec["rate"] is not None and rec["rate"] > MISMATCH_LIMIT:
        print(f"\n  REFUSING TO WRITE — mismatch rate {rec['rate'] * 100:.2f}% exceeds "
              f"{MISMATCH_LIMIT * 100:.1f}%.\n  The two sources disagree about what "
              f"happened; a partition written now would look fine and be wrong.",
              file=sys.stderr)
        return 2

    ic = InvezgoClient(date=end)
    cross = sample_invezgo(ic, rows) if (ic.enabled and not args.no_reconcile) else {}
    if cross.get("samples"):
        print(f"  cross-vendor    : mean buy-value gap "
              f"{(cross['mean_gap'] or 0) * 100:.2f}%, max "
              f"{(cross['max_gap'] or 0) * 100:.2f}% over {len(cross['samples'])} "
              f"stock-days")

    n = write_partitions(rows)
    (PANEL / "gross_report.json").write_text(json.dumps({
        "generated_at": datetime.now(WIB).isoformat(),
        "window": {"start": start, "end": end},
        "symbols": len(syms), "failed": failed,
        "rows_total": n, "rows_new": len(rows),
        "reconciliation": rec, "cross_vendor": cross,
        "sectors_credits": sc.credits, "clamped_windows": sc.clamps,
    }, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    sc.report()
    print(f"  wrote {n:,} rows to {PANEL}/gross-*.csv.gz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
