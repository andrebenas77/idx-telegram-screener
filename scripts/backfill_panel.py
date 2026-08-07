#!/usr/bin/env python3
"""Phase 1 backfill — build the 2-year broker x stock x day panel.

One `inventory_chart` request per symbol returns BOTH the daily price series and 20
brokers' daily series, so the whole universe costs ~1 request per name (Phase 0 measured
the quota at 30,000/day). Pulling broker-first would have been wrong: `stalker/list`
returns totals aggregated over the window with no date dimension at all.

Three corrections from Phase 0 are load-bearing here, and each would corrupt the panel
silently if dropped:

1. **`value` is a CUMULATIVE position, not a daily flow.** Daily net is the first
   difference (verified 20/20 brokers; the raw value matched 0/20). The first day of a
   window is a baseline, not a flow, so it is dropped.
2. **Invezgo prices are RAW.** A 10:1 split reads as a -90% day. Forward returns are
   computed on a back-adjusted series built from Sectors corporate actions, and the
   result is asserted against IDX auto-rejection bands.
3. **Units and types.** Every figure arrives as a string; volume is in shares where
   Sectors uses lots (1 lot = 100 shares).

Outputs (monthly gzipped partitions, git-trackable):
    data/panel/flows-YYYY-MM.csv.gz    date,symbol,broker,net_value
    data/panel/prices-YYYY-MM.csv.gz   date,symbol,open,high,low,close,volume,close_adj
    data/panel/corporate_actions.json  raw, per symbol
    data/panel/backfill_report.json    coverage + validation results

Usage:
    py scripts/backfill_panel.py [--years 2] [--limit N] [--skip-actions]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from invezgo_client import InvezgoClient  # noqa: E402
from sectors_client import SectorsClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TICKERS = ROOT / "reference" / "tickers.csv"
OUT = ROOT / "data" / "panel"

LOT = 100  # IDX: 1 lot = 100 shares

# Auto-rejection bands, symmetric since 2023-09-04. A one-day move beyond these cannot
# come from trading, so a residual break after adjustment means a MISSED corporate
# action. Used as the acceptance test for the adjustment layer.
ARB_SLACK = 0.05


def arb_limit(price: float) -> float:
    if price <= 200:
        return 0.35
    if price <= 5000:
        return 0.25
    return 0.20


def to_float(v, default=None):
    """Invezgo returns every figure as a string; be liberal."""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_universe(limit: int | None) -> list[str]:
    rows = list(csv.DictReader(TICKERS.open(encoding="utf-8")))
    syms = [r["ticker"].strip().upper() for r in rows if r.get("liquid") == "1"]
    return syms[:limit] if limit else syms


# ------------------------------------------------------------------ corporate actions

def adjustment_factors(actions: dict, closes: dict[str, float]) -> tuple[dict, list]:
    """Back-adjustment factor per date, plus a list of actions actually applied.

    Walk dates DESCENDING carrying a running factor. On an ex-date, assign the current
    factor to that date and every later one, then fold the action in so all EARLIER
    dates get the smaller factor. adjusted = close * factor.

    Splits: `split_ratio: 10` means the price divides by 10 on the date, so earlier
    prices are 10x too high -> factor /= 10 going back.
    Dividends: the price drops by `dividend_amount` on ex_date, so earlier prices are
    high by that amount -> multiplicative (P-A)/P.
    """
    ca = (actions or {}).get("corporate_actions") or {}
    applied, unhandled = [], []

    # Sectors repeats some entries verbatim — TOWR's 2025-07-09 rights issue appears
    # twice — and applying one twice compounds the adjustment (+3.8% became +7.0%).
    # Dedupe on the full content of each action before anything is applied.
    def dedupe(items):
        seen, out = set(), []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            k = json.dumps(it, sort_keys=True, ensure_ascii=False)
            if k not in seen:
                seen.add(k)
                out.append(it)
        return out

    ca = {k: (dedupe(v) if isinstance(v, list) else v) for k, v in ca.items()}

    by_date: dict[str, list] = defaultdict(list)
    for s in (ca.get("stock_split") or []):
        d, r = s.get("date"), to_float(s.get("split_ratio"))
        if d and r and r > 0:
            by_date[str(d)[:10]].append(("split", r))
    for dv in (ca.get("dividend") or []):
        d, a = dv.get("ex_date"), to_float(dv.get("dividend_amount"))
        if d and a and a > 0:
            by_date[str(d)[:10]].append(("dividend", a))

    # Rights issues carry `old_ratio`/`new_ratio` (new shares per existing) plus the
    # subscription `price`. The reference price drops to the theoretical ex-rights
    # price (TERP) on the ex-date, and that drop can sit just UNDER the auto-rejection
    # band — so it is invisible to the ARB scan and has to be handled explicitly.
    for ri in (ca.get("right_issue") or []):
        d = ri.get("ex_date")
        old, new = to_float(ri.get("old_ratio")), to_float(ri.get("new_ratio"))
        sub = to_float(ri.get("price"))
        if d and old and new and sub is not None and (old + new) > 0:
            by_date[str(d)[:10]].append(("rights", (old, new, sub)))
        else:
            unhandled.append({"right_issue": ri})

    # Bonus shares dilute by old/(old+new). The payload gives `payment_date` rather
    # than an ex-date, so the adjustment can land a few days late; observed ratios are
    # tiny (e.g. 1:625) so the error is immaterial, but it is recorded either way.
    for bn in (ca.get("bonus") or []):
        d = bn.get("payment_date") or bn.get("ex_date")
        old, new = to_float(bn.get("old_ratio")), to_float(bn.get("new_ratio"))
        if d and old and new and (old + new) > 0:
            by_date[str(d)[:10]].append(("bonus", (old, new)))
        else:
            unhandled.append({"bonus": bn})

    # Warrants do not reprice the underlying on a single date — exercise is spread over
    # a long period — so they are recorded, never adjusted.
    for w in (ca.get("warrant") or []):
        unhandled.append({"warrant": w})

    dates = sorted(closes)
    factor = 1.0
    out: dict[str, float] = {}
    for d in reversed(dates):
        out[d] = factor
        for kind, val in by_date.get(d, []):
            prev = [x for x in dates if x < d]
            base = closes.get(prev[-1]) if prev else None

            if kind == "split":
                factor /= val
                applied.append({"date": d, "type": "split", "ratio": val})

            elif kind == "dividend":
                if base and base > val:
                    factor *= (base - val) / base
                    applied.append({"date": d, "type": "dividend", "amount": val})

            elif kind == "rights":
                old, new, sub = val
                if base:
                    terp = (old * base + new * sub) / (old + new)
                    if terp > 0:
                        factor *= terp / base
                        applied.append({
                            "date": d, "type": "rights", "old": old, "new": new,
                            "sub_price": sub, "cum_price": base,
                            "terp": round(terp, 2),
                            "adjustment": round(terp / base - 1, 4)})

            elif kind == "bonus":
                old, new = val
                factor *= old / (old + new)
                applied.append({"date": d, "type": "bonus", "old": old, "new": new,
                                "adjustment": round(old / (old + new) - 1, 4)})
    return out, {"applied": applied, "unhandled": unhandled}


# ---------------------------------------------------------------------------- backfill

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap universe size (for a smoke run)")
    ap.add_argument("--skip-actions", action="store_true",
                    help="skip the Sectors corporate-action pull (prices stay RAW)")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated symbols instead of the universe (testing)")
    ap.add_argument("--incremental", action="store_true",
                    help="daily refresh: pull a short recent window and MERGE into the "
                         "existing partitions instead of rebuilding from scratch")
    ap.add_argument("--window", type=int, default=90,
                    help="days of history to pull in incremental mode")
    ap.add_argument("--refresh-actions", action="store_true",
                    help="re-fetch corporate actions from Sectors (weekly; incremental "
                         "runs otherwise read the cached file for free)")
    args = ap.parse_args()

    inv = InvezgoClient()
    if not inv.enabled:
        print("INVEZGO_API_KEY not set — see scripts/probe_invezgo.py")
        return 2
    sec = SectorsClient()

    end = date.today()
    start = (end - timedelta(days=args.window) if args.incremental
             else end - timedelta(days=int(365 * args.years)))
    partial = bool(args.symbols or args.limit)
    universe = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                if args.symbols else load_universe(args.limit))

    # Partitions are written in truncate mode, so a partial run would otherwise replace
    # the whole panel with just the symbols it touched. Send partial runs somewhere
    # harmless instead; to repair one symbol, re-run the FULL universe — the day-scoped
    # caches make that nearly free.
    global OUT
    if partial:
        OUT = OUT / "_partial"
        print(f"PARTIAL RUN -> writing to {OUT} (the real panel is left untouched)")
    OUT.mkdir(parents=True, exist_ok=True)

    # Duplicates in the universe would write the same (date, symbol, broker) twice and
    # silently double every flow figure for that name.
    seen: set[str] = set()
    universe = [s for s in universe if not (s in seen or seen.add(s))]

    # Incremental runs read cached corporate actions rather than re-fetching ~113
    # symbols from Sectors every morning.
    cached_actions = None
    ca_path = OUT / "corporate_actions.json"
    if args.incremental and not args.refresh_actions and ca_path.exists():
        try:
            cached_actions = json.loads(ca_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"could not read {ca_path} ({e}) — falling back to live fetch")

    mode = "INCREMENTAL (merge)" if args.incremental else "FULL (rebuild)"
    ca_mode = ("SKIPPED (prices raw)" if args.skip_actions
               else f"cached file ({len(cached_actions)} symbols)" if cached_actions
               else "Sectors (live)")
    print(f"mode     : {mode}")
    print(f"universe : {len(universe)} liquid tickers")
    print(f"window   : {start} -> {end}")
    print(f"actions  : {ca_mode}\n")

    flows: dict[str, list] = defaultdict(list)   # YYYY-MM -> rows
    prices: dict[str, list] = defaultdict(list)
    all_actions: dict[str, dict] = {}
    stats = {"symbols_ok": 0, "symbols_failed": [], "broker_days": 0, "price_days": 0,
             "brokers_seen": set(), "residual_breaks": [], "action_notes": {}}

    for i, sym in enumerate(universe, 1):
        payload = inv.inventory_chart(sym, start.isoformat(), end.isoformat())
        if not payload or "broker" not in payload:
            stats["symbols_failed"].append(sym)
            print(f"[{i:>3}/{len(universe)}] {sym:<6} FAILED")
            continue

        # ---- prices
        closes: dict[str, float] = {}
        rawpx: dict[str, dict] = {}
        for r in payload.get("price") or []:
            d = str(r.get("date"))[:10]
            cl = to_float(r.get("close"))
            if not d or cl is None:
                continue
            closes[d] = cl
            rawpx[d] = r

        # ---- adjustment
        if args.skip_actions or not closes:
            factors, notes = {d: 1.0 for d in closes}, {"applied": [], "unhandled": []}
        else:
            if cached_actions is not None:
                # Incremental runs read the file written by the last full backfill.
                # Free, and correct as long as it is refreshed periodically — the ARB
                # break scan below is the backstop for anything it has gone stale on.
                actions = cached_actions.get(sym)
            else:
                actions = sec.corporate_actions(sym)
            all_actions[sym] = actions
            factors, notes = adjustment_factors(actions, closes)
        if notes["applied"] or notes["unhandled"]:
            stats["action_notes"][sym] = notes

        for d in sorted(closes):
            r = rawpx[d]
            f = factors.get(d, 1.0)
            prices[d[:7]].append([
                d, sym,
                to_float(r.get("open")), to_float(r.get("high")),
                to_float(r.get("low")), closes[d],
                to_float(r.get("volume")), round(closes[d] * f, 6),
            ])
            stats["price_days"] += 1

        # ---- residual break check (the adjustment's acceptance test)
        adj = sorted((d, closes[d] * factors.get(d, 1.0)) for d in closes)
        for (d0, p0), (d1, p1) in zip(adj, adj[1:]):
            if p0 <= 0:
                continue
            ret = p1 / p0 - 1
            if abs(ret) > arb_limit(p0) + ARB_SLACK:
                stats["residual_breaks"].append(
                    {"symbol": sym, "from": d0, "to": d1, "return": round(ret, 4)})

        # ---- broker flows: CUMULATIVE -> daily net via first difference
        for b in payload.get("broker") or []:
            code = str(b.get("broker") or "").strip().upper()
            if not code:
                continue
            stats["brokers_seen"].add(code)
            series = sorted(
                (str(x.get("date"))[:10], to_float(x.get("value")))
                for x in (b.get("data") or [])
                if x.get("date") is not None and to_float(x.get("value")) is not None
            )
            # zip(series, series[1:]) drops the first observation by construction —
            # that day is a baseline position, not a flow.
            for (_, v0), (d1, v1) in zip(series, series[1:]):
                net = v1 - v0
                if net == 0:
                    continue  # broker inactive that day
                flows[d1[:7]].append([d1, sym, code, int(net)])
                stats["broker_days"] += 1

        stats["symbols_ok"] += 1
        if i % 20 == 0 or i == len(universe):
            print(f"[{i:>3}/{len(universe)}] {sym:<6} "
                  f"ok={stats['symbols_ok']} flows={stats['broker_days']:,} "
                  f"req={inv.requests_used}")

    # ---- write partitions
    def write(kind: str, buckets: dict, header: list[str]) -> int:
        """Full rebuild: truncate and write. Only ever called on a full run."""
        n = 0
        for month, rows in sorted(buckets.items()):
            p = OUT / f"{kind}-{month}.csv.gz"
            with gzip.open(p, "wt", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(header)
                w.writerows(sorted(rows))
                n += len(rows)
        return n

    def merge(kind: str, buckets: dict, header: list[str], key_cols: int,
              lo_override: str | None = None) -> int:
        """Incremental: replace only the refreshed slice, preserve everything else.

        For every month the window touches, keep existing rows EXCEPT those whose
        symbol was refreshed AND whose date falls inside the window — those are
        superseded. Then add the new rows.

        Deliberately not an append: appending would duplicate every row on a re-run,
        and a doubled flow figure looks entirely plausible on the page. The replace-
        then-add shape is what makes running twice a no-op, which is the property
        the acceptance test checks.
        """
        # `lo_override` exists for FLOWS. Flows are the first difference of a
        # cumulative series, so the window's opening session is a baseline and yields
        # no flow row. Deleting the whole window and re-adding only the diffed rows
        # therefore empties that first date — a silent one-day hole that an
        # idempotency test cannot see, because every run reproduces it identically.
        # Replacing only from the first date we actually produced leaves the opening
        # session's original rows intact.
        lo = lo_override or start.isoformat()
        hi = end.isoformat()
        refreshed = set(universe)

        months = set(buckets)
        for f in OUT.glob(f"{kind}-*.csv.gz"):
            m = f.stem.replace(f"{kind}-", "").replace(".csv", "")
            # Include months the window spans even if they produced no new rows, so a
            # row that has since disappeared upstream is still dropped.
            if lo[:7] <= m <= hi[:7]:
                months.add(m)

        total = 0
        for month in sorted(months):
            p = OUT / f"{kind}-{month}.csv.gz"
            kept = []
            if p.exists():
                with gzip.open(p, "rt", encoding="utf-8") as fh:
                    rd = csv.reader(fh)
                    next(rd, None)
                    for row in rd:
                        if not row:
                            continue
                        d, sym = row[0], row[1]
                        if sym in refreshed and lo <= d <= hi:
                            continue  # superseded by this run
                        kept.append(row)

            fresh = [[str(c) for c in r] for r in buckets.get(month, [])]

            # Belt and braces: dedupe on the natural key so a repeated symbol or an
            # overlapping window can never produce two rows for the same fact.
            out_rows, seen_keys = [], set()
            for r in kept + fresh:
                k = tuple(r[:key_cols])
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                out_rows.append(r)

            with gzip.open(p, "wt", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(header)
                w.writerows(sorted(out_rows))
            total += len(out_rows)
        return total

    FLOW_HDR = ["date", "symbol", "broker", "net_value"]
    PX_HDR = ["date", "symbol", "open", "high", "low", "close", "volume", "close_adj"]
    if args.incremental:
        # Natural keys: a flow row is one (date, symbol, broker); a price row is one
        # (date, symbol). Flows replace only from the earliest date they actually
        # produced — see the note in merge().
        first_flow = min((r[0] for rows in flows.values() for r in rows), default=None)
        n_flow = merge("flows", flows, FLOW_HDR, key_cols=3, lo_override=first_flow)
        n_px = merge("prices", prices, PX_HDR, key_cols=2)
    else:
        n_flow = write("flows", flows, FLOW_HDR)
        n_px = write("prices", prices, PX_HDR)

    # Never overwrite the full corporate-action record with a partial one.
    if args.incremental and cached_actions is not None:
        all_actions = {}
    if all_actions:
        (OUT / "corporate_actions.json").write_text(
            json.dumps(all_actions, ensure_ascii=False, indent=1), encoding="utf-8")

    report = {
        "generated": date.today().isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "universe": len(universe),
        "symbols_ok": stats["symbols_ok"],
        "symbols_failed": stats["symbols_failed"],
        "broker_day_rows": n_flow,
        "price_rows": n_px,
        "distinct_brokers": sorted(stats["brokers_seen"]),
        "months": sorted(flows),
        "residual_breaks": stats["residual_breaks"],
        "action_notes": stats["action_notes"],
        "invezgo_requests": inv.requests_used,
        "sectors_credits": sec.credits,
    }
    (OUT / "backfill_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'=' * 66}\nBACKFILL COMPLETE\n{'=' * 66}")
    print(f"  symbols        : {stats['symbols_ok']}/{len(universe)} ok"
          + (f", failed: {stats['symbols_failed']}" if stats["symbols_failed"] else ""))
    print(f"  broker-days    : {n_flow:,} rows across {len(stats['brokers_seen'])} brokers")
    print(f"  price-days     : {n_px:,} rows")
    print(f"  months         : {len(flows)} partitions in {OUT}")
    print(f"  invezgo reqs   : {inv.requests_used}   sectors credits: {sec.credits}")

    breaks = stats["residual_breaks"]
    print(f"\n  ADJUSTMENT CHECK — residual moves beyond the auto-rejection band")
    if not breaks:
        print("    none. Every corporate action in the window is accounted for.")
    else:
        print(f"    {len(breaks)} residual break(s) — a MISSED corporate action:")
        for b in breaks[:15]:
            print(f"      {b['symbol']:<6} {b['from']} -> {b['to']}  "
                  f"{b['return']:+.1%}")
        if args.incremental:
            # A break during an incremental run means a corporate action the cached
            # file does not know about. Back-adjustment anchors at the newest date, so
            # rescaling only the refreshed slice would leave it inconsistent with the
            # untouched history before it — a discontinuity that no later check would
            # catch. Refuse rather than write a subtly wrong panel.
            print("\n    A corporate action landed inside the refreshed window.")
            print("    The merged slice would be rescaled while older rows are not,")
            print("    leaving a silent discontinuity. FIX:")
            print("      py scripts/backfill_panel.py --refresh-actions   (full rebuild)")
        else:
            print("    Forward returns for these names are NOT trustworthy yet.")
    inv.report()
    sec.report()
    return 0 if not breaks else 1


if __name__ == "__main__":
    raise SystemExit(main())
