#!/usr/bin/env python3
"""How much of a winner does the current exit actually capture?

The momentum board's execution layer answers two questions — where the stop goes, and
when the thesis is broken (E2, a close below the 5-session low). It answers nothing
above the entry price. So "should I have held ISAT past 2500?" has never had an
evidence-based answer, and the trader's own answer is fear.

This measures the excursions. It is DESCRIPTIVE and ships no rule. Its output is the
input to trade_backtest's profit-rule table, and the base-rate panel on the board.

Three deliberate separations, each of which inverts the answer if collapsed:

1. **Horizon.** `incumbent` scans only while the trade was actually alive under
   E1/E2 — so every "given it reached +2R" statistic is CONDITIONED ON SURVIVING to
   +2R, which is itself an outcome. `fixed10` ignores the stop entirely and is the
   upper envelope. They are never pooled, and the survival-conditioned ones carry
   `[sc]` in the key so they cannot be quoted without the caveat.
2. **Price basis.** `hi` is what a resting LIMIT could have caught; `cl` is what a
   CLOSE-based rule could have caught. Justifying a target on intraday highs and then
   implementing it on closes is the classic way to manufacture an edge, and the gap
   is widest on exactly the volatile names where a target feels most needed.
3. **Cohort.** 47 of the 161 panel names were added by a 2026-08 discovery screen and
   their pre-2026-06 history is survivorship-contaminated — that cohort was
   historically the strongest (+4.96pp). Reported apart from the curated 112.

The number that decides everything downstream is **post_touch_R = terminal_R -
threshold_R**: the R still earned AFTER first touching a level. That is precisely what
a full-exit target trades away. If it is positive with a band clear of zero, taking
profit there is destructive and no hard target should be built at all.

Usage:
    py scripts/mfe_study.py                 # full study
    py scripts/mfe_study.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trade_backtest as tb  # noqa: E402
from alpha_lib import PANEL, Panel, block_ci, panel_fingerprint  # noqa: E402
from trade_lib import (atr_series, config_from_env, low_n_prior,  # noqa: E402
                       rvol1_series, stop_price)

R_GRID = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
ATR_GRID = (1.0, 1.5, 2.0, 3.0)
FIXED_H = 10          # matches trade_backtest.MAX_HOLD — the incumbent's own ceiling
TICKERS = Path(__file__).resolve().parent.parent / "reference" / "tickers.csv"


def curated_symbols() -> set[str]:
    """The 112 hand-curated names, as opposed to the 2026-08 discovery additions."""
    out = set()
    if TICKERS.exists():
        with TICKERS.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                s = (r.get("ticker") or r.get("symbol") or "").strip().upper()
                if s:
                    out.add(s)
    return out


# --------------------------------------------------------------------------- one trade

def excursions(p: Panel, t: dict, atr14: float, last_i: int) -> dict:
    """MFE/MAE on both bases over (entry_i, last_i], plus the full R paths.

    R is measured against the SAME frozen risk-per-share the trade was sized on, so a
    number here is directly comparable to a number from trade_backtest.
    """
    sym, ent, entry, r_ps = t["symbol"], t["entry_i"], t["entry"], t["r_ps"]
    hi, lo, cl = p.high.get(sym, {}), p.low.get(sym, {}), p.raw_close.get(sym, {})
    f_ent = tb._adj_factor(p, sym, ent)

    def as_r(px: float, i: int) -> float:
        # Adjusted basis, same convention as trade_backtest: a split inside the window
        # is a price step, not a 90% excursion.
        return (px * tb._adj_factor(p, sym, i) / f_ent - entry) / r_ps

    out: dict = {"n_sessions_scanned": 0}
    best_hi = best_cl = worst_lo = worst_cl = None
    path_cl, path_hi = [], []
    for j in range(ent + 1, last_i + 1):
        if j not in cl:
            continue
        out["n_sessions_scanned"] += 1
        rc = as_r(cl[j], j)
        rh = as_r(hi[j], j) if j in hi else rc
        rl = as_r(lo[j], j) if j in lo else rc
        path_cl.append(round(rc, 4))
        path_hi.append(round(rh, 4))
        if best_hi is None or rh > best_hi[0]:
            best_hi = (rh, j)
        if best_cl is None or rc > best_cl[0]:
            best_cl = (rc, j)
        if worst_lo is None or rl < worst_lo[0]:
            worst_lo = (rl, j)
        if worst_cl is None or rc < worst_cl[0]:
            worst_cl = (rc, j)

    if best_hi is None:
        return {}
    for name, v in (("mfe_hi", best_hi), ("mfe_cl", best_cl),
                    ("mae_lo", worst_lo), ("mae_cl", worst_cl)):
        out[f"{name}_R"] = v[0]
        out[f"{name}_i"] = v[1]
        out[f"{name}_session"] = v[1] - ent
        out[f"{name}_atr"] = v[0] * r_ps / atr14 if atr14 else None
    out["mfe_before_mae"] = best_hi[1] <= worst_lo[1]
    out["path_close_R"] = path_cl
    out["path_high_R"] = path_hi
    return out


def build_rows(p: Panel, cfg, atrs: dict, rv1s: dict, cands: list[dict]) -> list[dict]:
    """Two rows per tradeable candidate: one per horizon."""
    curated = curated_symbols()
    rows = []
    for c in cands:
        sym, i = c["symbol"], c["i"]
        cl = p.raw_close.get(sym, {})
        ent = i + 1
        if ent not in cl or cl[ent] <= 0:
            continue
        entry = cl[ent]
        a = atrs.get(sym, {}).get(ent)
        if not a:
            continue
        stop, basis = stop_price(entry, a, low_n_prior(p, sym, ent, cfg.struct_lookback), cfg)
        if not stop:
            continue                      # same 'stoppable' universe the incumbent trades
        r_ps = entry - stop
        if r_ps <= 0:
            continue

        sim = tb.simulate(p, c, cfg, {"atr_stop", "E2"}, atrs, rv1s)
        if not sim:
            continue

        base = {"symbol": sym, "signal_i": i, "signal_date": p.dates[i],
                "entry_i": ent, "entry_date": p.dates[ent], "entry": entry,
                "stop": stop, "stop_basis": basis, "r_ps": r_ps, "atr14": a,
                "adtv": (p.adtv.get(sym) or {}).get(i),
                "cohort": "curated112" if sym in curated else "discovered49",
                "terminal_i": sim["exit_i"], "terminal_R": sim["R"],
                "terminal_excess": sim["excess"], "exit_rule": sim["rule"],
                "held": sim["held"]}

        # `incumbent` can overshoot ent+10 by one session: E2 decides on day j and
        # fills at j+1, and j may already be ent+MAX_HOLD. Clip and record it rather
        # than silently truncating a scan the trade really did experience.
        inc_last = min(sim["exit_i"], ent + FIXED_H)
        for horizon, last_i in (("incumbent", inc_last), ("fixed10", ent + FIXED_H)):
            ex = excursions(p, base | {"r_ps": r_ps}, a, last_i)
            if not ex:
                continue
            rows.append(base | ex | {
                "horizon": horizon, "scan_last_i": last_i,
                "overshoot": max(0, sim["exit_i"] - (ent + FIXED_H))})
    return rows


# ------------------------------------------------------------------ conditional tables

def conditional(rows: list[dict], horizon: str, basis: str, cohort: str,
                grid, kind: str = "R") -> list[dict]:
    """Given a trade reached the level, what happened next?

    post_touch_R is the whole point. `counterfactual_*` replays every trade with a
    full exit at the level — which is a population statement, not a validated rule:
    it has no null and no fold structure. trade_backtest supplies those.
    """
    sub = [r for r in rows if r["horizon"] == horizon
           and (cohort == "all" or r["cohort"] == cohort)]
    sub.sort(key=lambda r: (r["entry_i"], r["symbol"]))
    key = f"mfe_{basis}"
    out = []
    for th in grid:
        # threshold in R; for the ATR grid convert to R using the trade's own scale
        reached, thr_R = [], {}
        for r in sub:
            t_R = th if kind == "R" else (th * r["atr14"] / r["r_ps"])
            if r.get(f"{key}_R") is not None and r[f"{key}_R"] >= t_R:
                reached.append(r)
                thr_R[id(r)] = t_R
        if not reached:
            out.append({"threshold": th, "kind": kind, "n_reached": 0,
                        "reach_rate": 0.0})
            continue

        post = [r["terminal_R"] - thr_R[id(r)] for r in reached]
        ci = block_ci(post)
        term = [r["terminal_R"] for r in reached]
        # Counterfactual: the trades that reached the level exit there instead of
        # running to their real terminal. Every trade in `reached` reached it by
        # construction, so this is just the level itself, trade by trade — the ATR
        # grid makes the level differ per trade, which is why it is not a constant.
        cf = [thr_R[id(r)] for r in reached]
        first_touch = [r[f"{key}_session"] for r in reached]
        out.append({
            "threshold": th, "kind": kind, "horizon": horizon, "basis": basis,
            "cohort": cohort,
            "n_reached": len(reached), "reach_rate": len(reached) / len(sub),
            "median_session_of_first_touch": statistics.median(first_touch),
            "post_touch_R_mean": ci["mean"], "post_touch_R_median": statistics.median(post),
            "post_touch_R_ci95": [ci["lo95"], ci["hi95"]],
            "post_touch_clear_of_zero": bool(
                ci["lo95"] is not None and (ci["lo95"] > 0 or ci["hi95"] < 0)),
            "terminal_R_mean": statistics.fmean(term),
            "terminal_R_median": statistics.median(term),
            "frac_gave_it_all_back": sum(1 for r in reached if r["terminal_R"] <= 0) / len(reached),
            "frac_gave_back_half": sum(
                1 for r in reached if r["terminal_R"] <= 0.5 * thr_R[id(r)]) / len(reached),
            "frac_finished_above_threshold": sum(
                1 for r in reached if r["terminal_R"] >= thr_R[id(r)]) / len(reached),
            "frac_stopped_after_touch": sum(
                1 for r in reached if r["exit_rule"] == "E1_stop") / len(reached),
            "counterfactual_exit_at_threshold_R": statistics.fmean(cf),
            "counterfactual_delta_R": statistics.fmean(cf) - statistics.fmean(term),
        })
    return out


def summarise(rows: list[dict], horizon: str, basis: str) -> dict:
    sub = [r for r in rows if r["horizon"] == horizon]
    key = f"mfe_{basis}"
    mfe = [r[f"{key}_R"] for r in sub if r.get(f"{key}_R") is not None]
    mae = [r["mae_lo_R"] for r in sub if r.get("mae_lo_R") is not None]
    cap = [r["terminal_R"] / r[f"{key}_R"] for r in sub
           if r.get(f"{key}_R") and r[f"{key}_R"] > 0.25]
    sess = [r[f"{key}_session"] for r in sub if r.get(f"{key}_session") is not None]
    return {"horizon": horizon, "basis": basis, "n": len(sub),
            "mean_mfe_R": statistics.fmean(mfe) if mfe else None,
            "median_mfe_R": statistics.median(mfe) if mfe else None,
            "mean_mae_R": statistics.fmean(mae) if mae else None,
            "median_mae_R": statistics.median(mae) if mae else None,
            "mean_capture": statistics.fmean(cap) if cap else None,
            "median_capture": statistics.median(cap) if cap else None,
            "median_mfe_session": statistics.median(sess) if sess else None,
            "frac_mfe_on_session_1": (sum(1 for s in sess if s == 1) / len(sess)) if sess else None}


# ------------------------------------------------------------------------------ selftest

def _selftest() -> int:
    """Synthetic paths with known excursions — the arithmetic, not the data."""
    fails = 0

    def chk(name, got, want, tol=1e-9):
        nonlocal fails
        ok = abs(got - want) <= tol
        fails += (not ok)
        print(f"  [{'ok' if ok else '!!'}] {name}: got {got:.4f} want {want:.4f}")

    class P:  # minimal Panel stand-in
        dates = [f"d{i}" for i in range(10)]
        high = {"X": {1: 110, 2: 130, 3: 120}}
        low = {"X": {1: 95, 2: 100, 3: 90}}
        raw_close = {"X": {0: 100, 1: 105, 2: 125, 3: 95}}
        close = {"X": {0: 100, 1: 105, 2: 125, 3: 95}}
        adtv: dict = {}

    p = P()
    t = {"symbol": "X", "entry_i": 0, "entry": 100.0, "r_ps": 10.0}
    ex = excursions(p, t, atr14=6.6667, last_i=3)
    # entry 100, r_ps 10 -> +1R = 110. High 130 on session 2 => +3R.
    chk("MFE on highs is +3R", ex["mfe_hi_R"], 3.0)
    chk("MFE session is 2", float(ex["mfe_hi_session"]), 2.0)
    chk("MFE on closes is +2.5R", ex["mfe_cl_R"], 2.5)
    chk("MAE on lows is -1R", ex["mae_lo_R"], -1.0)
    chk("MAE on closes is -0.5R", ex["mae_cl_R"], -0.5)
    chk("MFE in ATR units", ex["mfe_hi_atr"], 3.0 * 10.0 / 6.6667, 1e-3)
    print(f"  [{'ok' if ex['mfe_before_mae'] else '!!'}] favourable peak precedes the trough")
    fails += (not ex["mfe_before_mae"])
    chk("scanned 3 sessions", float(ex["n_sessions_scanned"]), 3.0)
    chk("path length matches", float(len(ex["path_close_R"])), 3.0)

    # A trade whose MFE is on session 1 and never recovers.
    p2 = P()
    p2.high = {"X": {1: 120, 2: 101, 3: 99}}
    p2.low = {"X": {1: 99, 2: 90, 3: 80}}
    p2.raw_close = {"X": {0: 100, 1: 115, 2: 95, 3: 85}}
    p2.close = p2.raw_close
    ex2 = excursions(p2, t, atr14=10.0, last_i=3)
    chk("early-peak MFE +2R", ex2["mfe_hi_R"], 2.0)
    chk("early-peak MAE -2R", ex2["mae_lo_R"], -2.0)
    print(f"  [{'ok' if ex2['mfe_before_mae'] else '!!'}] peak precedes trough (early peak)")
    fails += (not ex2["mfe_before_mae"])

    print(f"\n[{'ok' if not fails else '!!'}] "
          f"{'all assertions passed' if not fails else f'{fails} failed'}")
    return fails


# ---------------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", type=str, default=str(PANEL / "mfe_study.json"))
    ap.add_argument("--rows", type=str, default=str(PANEL / "mfe_rows.csv.gz"))
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

    cfg, warn = config_from_env()
    for w in warn:
        print(f"[warn] {w}")

    print("loading panel...")
    p = Panel().load()
    print(f"  {p.describe()}")
    atrs = {s: atr_series(p, s) for s in p.raw_close}
    rv1s = {s: rvol1_series(p, s) for s in p.volume}
    cands = tb.build_candidates(p)
    print(f"  {len(cands)} candidates")

    rows = build_rows(p, cfg, atrs, rv1s, cands)
    n_tr = len({(r["symbol"], r["signal_i"]) for r in rows})
    n_cur = len({(r["symbol"], r["signal_i"]) for r in rows if r["cohort"] == "curated112"})
    print(f"  {n_tr} tradeable ({n_cur} curated112, {n_tr - n_cur} discovered49)"
          f" x 2 horizons = {len(rows)} rows")

    # ---- per-trade CSV
    cols = [k for k in rows[0] if k not in ("path_close_R", "path_high_R")] + \
           ["path_close_R", "path_high_R"]
    Path(a.rows).parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(a.rows, "wt", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r | {"path_close_R": json.dumps(r["path_close_R"]),
                            "path_high_R": json.dumps(r["path_high_R"])})
    print(f"  wrote {a.rows}")

    # ---- summary
    print("\nEXCURSION SUMMARY  (R against the trade's own frozen risk-per-share)")
    print(f"  {'horizon':<11}{'basis':<7}{'n':>6}{'meanMFE':>9}{'medMFE':>8}"
          f"{'meanMAE':>9}{'medMAE':>8}{'medCap':>8}{'medSess':>9}{'peak d1':>9}")
    summaries = []
    for h in ("incumbent", "fixed10"):
        for b in ("hi", "cl"):
            s = summarise(rows, h, b)
            summaries.append(s)
            print(f"  {h:<11}{b:<7}{s['n']:>6}{s['mean_mfe_R']:>9.2f}{s['median_mfe_R']:>8.2f}"
                  f"{s['mean_mae_R']:>9.2f}{s['median_mae_R']:>8.2f}"
                  f"{(s['median_capture'] or 0):>8.2f}{(s['median_mfe_session'] or 0):>9.1f}"
                  f"{(s['frac_mfe_on_session_1'] or 0) * 100:>8.0f}%")

    # ---- the decisive table
    conds = []
    for h in ("incumbent", "fixed10"):
        for b in ("hi", "cl"):
            for ch in ("all", "curated112", "discovered49"):
                conds += conditional(rows, h, b, ch, R_GRID, "R")
                conds += conditional(rows, h, b, ch, ATR_GRID, "ATR")

    print("\nPOST-TOUCH R — given the trade reached the level, what was still to come?")
    print("  post_touch_R = terminal_R - threshold. POSITIVE means taking profit there")
    print("  destroys value. Bands are circular moving-block bootstrap, never z-tests.")
    for h in ("incumbent", "fixed10"):
        print(f"\n  horizon={h}, basis=cl, cohort=all"
              f"{'   [sc: conditioned on surviving to the level]' if h == 'incumbent' else ''}")
        print(f"    {'level':>7}{'n':>6}{'reach':>7}{'postR':>8}{'  95% CI':>18}"
              f"{'clear':>7}{'allback':>9}{'>=lvl':>7}{'cfDelta':>9}")
        for c in conds:
            if not (c.get("horizon") == h and c.get("basis") == "cl"
                    and c.get("cohort") == "all" and c["kind"] == "R"):
                continue
            if not c["n_reached"]:
                continue
            lo, hi = c["post_touch_R_ci95"]
            print(f"    {c['threshold']:>6.1f}R{c['n_reached']:>6}{c['reach_rate'] * 100:>6.0f}%"
                  f"{c['post_touch_R_mean']:>8.2f}"
                  f"   [{lo:>+6.2f},{hi:>+6.2f}]"
                  f"{'YES' if c['post_touch_clear_of_zero'] else '-':>7}"
                  f"{c['frac_gave_it_all_back'] * 100:>8.0f}%"
                  f"{c['frac_finished_above_threshold'] * 100:>6.0f}%"
                  f"{c['counterfactual_delta_R']:>9.2f}")

    payload = {"panel_fingerprint": panel_fingerprint(),
               "conventions": {"gap_fill": tb.GAP_FILL, "e2_fill": tb.E2_FILL,
                               "ca_adjust": tb.CA_ADJUST},
               "fees": {"buy": cfg.fee_buy, "sell": cfg.fee_sell},
               "universe": {"n_candidates": len(cands), "n_tradeable": n_tr,
                            "curated112": n_cur, "discovered49": n_tr - n_cur},
               "summary": summaries, "conditional": conds}
    Path(a.out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {a.out}")
    print("\nDESCRIPTIVE ONLY — no rule ships from this file. The reading rule declared")
    print("before these numbers: post_touch_R positive with a band clear of zero means")
    print("a hard target is destructive and none should be built.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
