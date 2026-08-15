#!/usr/bin/env python3
"""What size, on a concentrated book holding at most a handful of names?

Trade-level results cannot answer this. They assume every signal is taken at full size,
and the board produces a mean of 4.5 candidates on 73% of sessions — 109 of 476 days
offer more than five. So slot competition binds on roughly one signal day in three, and
a per-trade mean silently describes a book that could never have existed.

This walks the book day by day: mark equity, run exits, then admit new names greedily in
rank order against the real caps (slots, heat, sector, beta-gross, liquidity, notional).
Every refusal is logged with its reason, because "which constraint actually bit" is the
answer to the sizing question and a mean return cannot express it.

**One exact simplification, and the reason it is exact.** A candidate's exit path depends
only on its price path and its stop, and the stop depends only on entry price and ATR —
never on size. So R and the return are size-independent; only the rupiah scales. Trades
are therefore simulated ONCE and the walk decides only which to take and how big. This is
not an approximation.

**The n=1 problem is this file's real weakness.** ~1,000 trades but ONE equity path per
configuration, and max drawdown from a single path is very noisy — while being the number
most wanted. So MDD and CAGR are always reported as p5-p50-p95 from a contiguous-block
resample of the admitted trade sequence, and no configuration is ever chosen on a point
MDD. If the spread across the whole sweep sits inside a single path's own band, the sweep
carries no information and every cell is a tie: a legitimate finding, stated in those
words rather than dressed up as an optimum.

**Point-in-time honesty.** 47 of the 161 panel names were added by a 2026-08 discovery
screen; their pre-2026-06 history is survivorship-contaminated and that cohort was
historically the strongest (+4.96pp). The primary run is the curated 112 — which is also
the only cohort carrying sectors, so it is the only one where max_per_sector is real.

Usage:
    py scripts/portfolio_sim.py                  # anchors + sweep
    py scripts/portfolio_sim.py --quick          # anchors only
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trade_backtest as tb  # noqa: E402
from alpha_lib import PANEL, Panel, panel_fingerprint  # noqa: E402
from trade_lib import (SHARES_PER_LOT, atr_series, beta,  # noqa: E402
                       config_from_env, rvol1_series, size_position)

RULES = {"atr_stop", "E2"}
TICKERS = Path(__file__).resolve().parent.parent / "reference" / "tickers.csv"
MDD_TOLERANCE = -0.15      # declared drawdown ceiling; set it before reading the sweep


def load_sectors() -> dict[str, str]:
    out = {}
    if TICKERS.exists():
        with TICKERS.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                s = (r.get("ticker") or r.get("symbol") or "").strip().upper()
                sec = (r.get("sector") or "").strip()
                if s:
                    out[s] = sec or None
    return out


# --------------------------------------------------------------------------- the walk

def walk(p: Panel, trades: dict, cfg, sectors: dict, betas: dict,
         *, start_i: int, end_i: int, universe: set[str] | None,
         rank: str = "adtv_pct", seed: int = 7) -> dict:
    """Day-by-day portfolio replay. Returns the equity path and the refusal ledger."""
    by_day: dict[int, list] = {}
    for (sym, i), t in trades.items():
        if universe and sym not in universe:
            continue
        by_day.setdefault(t["entry_i"], []).append((sym, i, t))

    rng = random.Random(seed)
    equity = cfg.equity_idr
    cash = equity
    open_pos: list[dict] = []
    path, refusals = [], {}
    peak, mdd = equity, 0.0
    admitted, n_signals, fees_paid, gross_hist, nopen_hist = [], 0, 0.0, [], []
    at_slot = at_heat = 0

    for i in range(start_i, end_i + 1):
        # ---- 1. exits first: capital freed today is available to today's admissions
        still = []
        for pos in open_pos:
            if pos["exit_i"] <= i:
                px = pos["exit_px_eff"]
                proceeds = px * pos["lots"] * SHARES_PER_LOT
                fee = proceeds * cfg.fee_sell
                cash += proceeds - fee
                fees_paid += fee
                admitted.append(pos | {"realised": proceeds - fee - pos["cost_idr"]})
            else:
                still.append(pos)
        open_pos = still

        # ---- 2. mark on RAW closes, the same series every structure decision uses
        mark = cash + sum(pos["lots"] * SHARES_PER_LOT
                          * p.raw_close.get(pos["symbol"], {}).get(i, pos["entry_px"])
                          for pos in open_pos)
        peak = max(peak, mark)
        mdd = min(mdd, mark / peak - 1)
        gross = sum(pos["lots"] * SHARES_PER_LOT
                    * p.raw_close.get(pos["symbol"], {}).get(i, pos["entry_px"])
                    for pos in open_pos) / mark if mark > 0 else 0.0
        gross_hist.append(gross)
        nopen_hist.append(len(open_pos))
        path.append({"i": i, "date": p.dates[i], "equity": mark, "cash": cash,
                     "gross": gross, "n_open": len(open_pos)})

        # ---- 3. admit, greedily and ACCUMULATING. Testing each candidate against the
        # book as it stands this morning clears them all independently and proposes six
        # positions at full risk against a 4.5% heat cap.
        todays = by_day.get(i, [])
        if not todays:
            continue
        n_signals += len(todays)
        if rank == "random":
            rng.shuffle(todays)
        else:
            todays.sort(key=lambda x: -(x[2].get("adtv_pct") or 0))

        # equity is MARKED, so the book compounds; replace() rather than mutate, since
        # RiskConfig is a mutable dataclass shared by reference across sweep cells
        cfg_today = dataclasses.replace(cfg, equity_idr=mark)
        slot_hit = heat_hit = False
        for sym, sig_i, t in todays:
            if any(o["symbol"] == sym for o in open_pos):
                refusals["already_open"] = refusals.get("already_open", 0) + 1
                continue
            entry, stop = t["entry"], t["stop"]
            if not stop or entry <= stop:
                refusals["no_stop"] = refusals.get("no_stop", 0) + 1
                continue
            sz = size_position(entry, stop, (p.adtv.get(sym) or {}).get(sig_i), cfg_today)
            if sz["lots"] < 1:
                k = (sz.get("binding") or "zero_lots")
                refusals[k] = refusals.get(k, 0) + 1
                continue
            cand = {"symbol": sym, "entry_px": entry, "stop_px": stop,
                    "lots": sz["lots"], "sector": sectors.get(sym),
                    "beta": betas.get((sym, t["entry_i"]))}
            ok, why = tb_admit(cand, open_pos, cfg_today)
            if not ok:
                refusals[why] = refusals.get(why, 0) + 1
                # admit() returns a REASON WITH NUMBERS ("slots full (5)",
                # "heat 4.55% > 4.50%"), so match the prefix — an equality test here
                # silently reports 0% cap-binding on a book that is capped daily.
                slot_hit |= why.startswith("slots")
                heat_hit |= why.startswith("heat")
                continue
            cost = entry * sz["lots"] * SHARES_PER_LOT
            fee = cost * cfg.fee_buy
            if cost + fee > cash:
                refusals["no_cash"] = refusals.get("no_cash", 0) + 1
                continue
            cash -= cost + fee
            fees_paid += fee
            fe = tb._adj_factor(p, sym, t["entry_i"])
            fx = tb._adj_factor(p, sym, t["exit_i"])
            open_pos.append({"symbol": sym, "entry_i": t["entry_i"], "entry_px": entry,
                             "stop_px": stop, "lots": sz["lots"],
                             "exit_i": t["exit_i"],
                             "exit_px_eff": t["exit"] * fx / fe if tb.CA_ADJUST else t["exit"],
                             "cost_idr": cost + fee, "R": t["R"],
                             "sector": sectors.get(sym), "r_idr": (entry - stop) * sz["lots"] * SHARES_PER_LOT})
        at_slot += slot_hit
        at_heat += heat_hit

    final = path[-1]["equity"] if path else cfg.equity_idr
    yrs = max(1e-9, len(path) / 246.0)
    rets = [path[k]["equity"] / path[k - 1]["equity"] - 1 for k in range(1, len(path))
            if path[k - 1]["equity"] > 0]
    return {"path": path, "final": final,
            "total_return": final / cfg.equity_idr - 1,
            "cagr": (final / cfg.equity_idr) ** (1 / yrs) - 1,
            "mdd": mdd, "calmar": ((final / cfg.equity_idr) ** (1 / yrs) - 1) / abs(mdd) if mdd else None,
            "vol_ann": statistics.pstdev(rets) * math.sqrt(246) if len(rets) > 2 else None,
            "worst_day": min(rets) if rets else None,
            "n_signals": n_signals, "n_admitted": len(admitted),
            "refusals": refusals, "fees_idr": fees_paid,
            "avg_gross": statistics.fmean(gross_hist) if gross_hist else 0,
            "avg_open": statistics.fmean(nopen_hist) if nopen_hist else 0,
            "pct_days_slot_cap": at_slot / max(1, len(path)),
            "pct_days_heat_cap": at_heat / max(1, len(path)),
            "admitted": admitted}


def tb_admit(cand, positions, cfg):
    from trade_lib import admit
    return admit(cand, positions, cfg)


def path_band(res: dict, cfg, draws: int = 600, seed: int = 7) -> dict:
    """Contiguous-block resample of DAILY equity returns, path replayed.

    Daily returns, not the trade sequence. Up to `max_open` positions are live at once,
    so the book's day-to-day move is the SUM of several overlapping trades; replaying
    realised trade P&Ls one after another treats them as sequential and manufactures
    drawdowns the structure cannot produce. Resampling the daily series keeps whatever
    concurrency the configuration actually ran.

    Blocks, not iid: consecutive days share market exposure and volatility clusters, so
    an iid resample reports a band far too narrow — the same error a z-test makes.
    Block length is a full trading month, comfortably longer than the ~7-session mean
    hold, so a block contains whole trades rather than slicing them.
    """
    path = res["path"]
    rets = [path[k]["equity"] / path[k - 1]["equity"] - 1
            for k in range(1, len(path)) if path[k - 1]["equity"] > 0]
    n = len(rets)
    if n < 60:
        return {}
    block = max(20, math.ceil(n ** (1 / 3)))
    nb = math.ceil(n / block)
    rng = random.Random(seed)
    cagrs, mdds = [], []
    yrs = max(1e-9, n / 246.0)
    for _ in range(draws):
        eq, pk, dd = 1.0, 1.0, 0.0
        for _ in range(nb):
            s = rng.randrange(n)
            for t in range(block):
                eq *= (1 + rets[(s + t) % n])
                pk = max(pk, eq)
                dd = min(dd, eq / pk - 1)
        cagrs.append(max(eq, 1e-6) ** (1 / yrs) - 1)
        mdds.append(dd)
    cagrs.sort()
    mdds.sort()
    def q(v, f):
        return v[min(len(v) - 1, int(f * len(v)))]
    return {"cagr_p5": q(cagrs, .05), "cagr_p50": q(cagrs, .50), "cagr_p95": q(cagrs, .95),
            "mdd_p5": q(mdds, .05), "mdd_p50": q(mdds, .50), "mdd_p95": q(mdds, .95)}


def line(name: str, r: dict, band: dict) -> str:
    ref = r["refusals"]
    top = ", ".join(f"{k} {v}" for k, v in sorted(ref.items(), key=lambda x: -x[1])[:3])
    return (f"  {name:<26}{r['cagr'] * 100:>7.1f}%{r['mdd'] * 100:>8.1f}%"
            f"{(band.get('mdd_p5') or 0) * 100:>9.1f}%"
            f"{r['avg_gross'] * 100:>7.0f}%{r['avg_open']:>6.1f}"
            f"{r['n_admitted']:>6}/{r['n_signals']:<5}"
            f"{r['pct_days_slot_cap'] * 100:>5.0f}%  {top}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", type=str, default=str(PANEL / "portfolio_sim.json"))
    a = ap.parse_args()

    cfg0, warn = config_from_env()
    print("loading panel...")
    p = Panel().load()
    print(f"  {p.describe()}")
    atrs = {s: atr_series(p, s) for s in p.raw_close}
    rv1s = {s: rvol1_series(p, s) for s in p.volume}
    cands = tb.build_candidates(p)
    sectors = load_sectors()
    curated = {s for s in sectors}

    print("simulating every candidate once (exit path is size-independent)...")
    trades = {}
    for c in cands:
        t = tb.simulate(p, c, cfg0, RULES, atrs, rv1s)
        if t:
            t["adtv_pct"] = c.get("adtv_pct")
            trades[(c["symbol"], c["i"])] = t
    print(f"  {len(trades)} tradeable of {len(cands)} candidates")

    betas = {}
    for (sym, _i), t in trades.items():
        k = (sym, t["entry_i"])
        if k not in betas:
            betas[k] = beta(p, sym, t["entry_i"])
    n_def = sum(1 for v in betas.values() if v is None)
    print(f"  betas precomputed | {n_def} of {len(betas)} default to 1.0")

    ents = [t["entry_i"] for t in trades.values()]
    start_i, end_i = min(ents), max(t["exit_i"] for t in trades.values())
    print(f"  walk {p.dates[start_i]} -> {p.dates[end_i]}  "
          f"(candidates stop at {p.dates[max(ents)]}: build_events drops events whose "
          f"forward horizons are unavailable)")

    def mk(**kw):
        base = dict(equity_idr=2_000_000_000.0, sizing_mode="risk", risk_pct=0.015,
                    max_open=5, heat_cap_pct=0.045, max_pos_pct=0.30)
        return dataclasses.replace(cfg0, **(base | kw))

    results = {}
    print(f"\n{'PRIMARY COHORT: curated 112'} | risk mode | MDD tolerance "
          f"{MDD_TOLERANCE * 100:.0f}%")
    print(f"  {'configuration':<26}{'CAGR':>8}{'MDD':>8}{'MDDp5':>10}"
          f"{'gross':>7}{'open':>6}{'taken':>12}{'slot':>6}  top refusals")
    print("  " + "-" * 118)

    anchors = [
        ("USER-STATED 2bn/5", mk()),
        ("ENV-LIVE 3bn/notional/6", dataclasses.replace(
            cfg0, equity_idr=3_000_000_000.0, sizing_mode="notional", max_open=6)),
        ("UNCONSTRAINED", mk(max_open=999, heat_cap_pct=99.0, max_pos_pct=0.30)),
    ]
    for name, cfg in anchors:
        r = walk(p, trades, cfg, sectors, betas, start_i=start_i, end_i=end_i,
                 universe=curated)
        b = path_band(r, cfg)
        results[name] = {k: v for k, v in r.items() if k not in ("path", "admitted")} | {"band": b}
        print(line(name, r, b))

    # IHSG buy-and-hold over the same window — without it the curve is uninterpretable
    bh = (p.bench[end_i] / p.bench[start_i] - 1) if (start_i in p.bench and end_i in p.bench) else None
    yrs = (end_i - start_i) / 246.0
    if bh is not None:
        print(f"  {'IHSG buy-and-hold':<26}{((1 + bh) ** (1 / yrs) - 1) * 100:>7.1f}%"
              f"{'':>8}{'':>10}{'100%':>7}")
        results["IHSG"] = {"cagr": (1 + bh) ** (1 / yrs) - 1, "total_return": bh}

    if not a.quick:
        for axis, vals in (("max_open", (3, 4, 5, 6, 8)),
                           ("risk_pct", (0.005, 0.0075, 0.010, 0.015, 0.020)),
                           ("heat_cap_pct", (0.030, 0.045, 0.060, 0.090)),
                           ("max_pos_pct", (0.15, 0.20, 0.30, 0.40))):
            print(f"\n  --- sweep {axis} (one at a time around USER-STATED) ---")
            for v in vals:
                cfg = mk(**{axis: v})
                r = walk(p, trades, cfg, sectors, betas, start_i=start_i, end_i=end_i,
                         universe=curated)
                b = path_band(r, cfg)
                nm = f"{axis}={v}"
                results[nm] = {k: x for k, x in r.items()
                               if k not in ("path", "admitted")} | {"band": b}
                print(line(nm, r, b))

        print("\n  --- all-161 cohort (SURVIVORSHIP-CONTAMINATED, secondary) ---")
        r = walk(p, trades, mk(), sectors, betas, start_i=start_i, end_i=end_i,
                 universe=None)
        b = path_band(r, mk())
        results["ALL161"] = {k: v for k, v in r.items()
                             if k not in ("path", "admitted")} | {"band": b}
        print(line("all 161 names", r, b))
        print("    47 of these were added by a 2026-08 discovery screen; their")
        print("    pre-2026-06 history is selected on later liquidity. Not achievable.")

    # ---- is the sweep informative at all?
    swept = [v for k, v in results.items() if "=" in k]
    if swept:
        spread = max(x["cagr"] for x in swept) - min(x["cagr"] for x in swept)
        band_w = statistics.fmean([x["band"]["cagr_p95"] - x["band"]["cagr_p5"]
                                   for x in swept if x.get("band")])
        print(f"\nIS THE SWEEP INFORMATIVE?")
        print(f"  CAGR spread across the grid   {spread * 100:.2f}pp")
        print(f"  mean single-path 90% band     {band_w * 100:.2f}pp")
        print(f"  -> {'the grid spread sits INSIDE one path own band: every cell is a tie, and the choice is preference, not optimisation.' if spread < band_w else 'the grid separates configurations by more than path noise.'}")

    payload = {"panel_fingerprint": panel_fingerprint(),
               "mdd_tolerance": MDD_TOLERANCE,
               "conventions": {"gap_fill": tb.GAP_FILL, "ca_adjust": tb.CA_ADJUST},
               "results": results}
    Path(a.out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
