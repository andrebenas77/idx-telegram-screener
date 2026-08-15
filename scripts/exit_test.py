#!/usr/bin/env python3
"""Does taking profit beat holding to the structure exit?

The incumbent is `ATR stop + E2` and it is the thing to beat. mfe_study.py has already
measured that trades reaching +2R go on to earn a further +1.39R on average with a band
clear of zero, so the prior going in is that ANY profit-taking is destructive. That is
a prediction, and this file is the test of it.

**Kill criterion, declared before the numbers.** A profit rule ships only if it clears
ALL of: n >= 200; beats the incumbent on mean R AND mean excess; wins >= 3 of 4 time
folds; sits at or above the 95th percentile of the timing-matched null; and its paired
delta band excludes zero. If the primary fails, the fallback is NOT "ship the best of
the fourteen" — it is to price the primary as a deliberate comfort tax, capped at
40bp of mean excess, which is roughly half the stop layer's entire like-for-like edge.

**One pre-registered primary: SCALE half at +2R.** Thirteen others run as exploratory
and report their rank among fourteen. Fourteen configurations with one winner is
exactly the shape that produced this repo's rejected intraday result, where the best
of 14 gates reached the 60th percentile of random filters — i.e. what chance produces.

**The null that binds here is TIMING, not acceptance.** A scale-out does not change
which trades are taken, so a same-acceptance-rate filter null is not the right control.
The right one asks: would selling half at a RANDOM session have done as well? It
permutes the observed scale sessions across the trades that fired, preserving both the
firing set and the distribution of holding periods. Matching only the count would
compare against a different EXPOSURE rather than a different TIMING.

A structural property that makes this exact and cheap: a `scale` leg never breaks the
simulation loop, so the remainder's exit path is IDENTICAL to the incumbent's. The
scale changes the P&L, not the trade. So every configuration is paired to the incumbent
trade-for-trade, and the paired band is far tighter — and far more honest — than
comparing two independently bootstrapped means.

Usage:
    py scripts/exit_test.py
    py scripts/exit_test.py --draws 400
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trade_backtest as tb  # noqa: E402
from alpha_lib import PANEL, Panel, block_ci, panel_fingerprint  # noqa: E402
from trade_lib import (LegPlan, atr_series, config_from_env,  # noqa: E402
                       rvol1_series, target_price)

RULES = {"atr_stop", "E2"}
SHIP_N = 200
KILL_BP = -40.0          # the user's declared comfort-tax ceiling, in bp of excess

PRIMARY = LegPlan(kind="scale", trigger="R", level=2.0, fraction=0.5,
                  fill="limit_intraday", label="scale50@2R")

EXPLORATORY = [
    LegPlan("scale", "R", 1.0, 0.5, "limit_intraday", "scale50@1R"),
    LegPlan("scale", "R", 1.5, 0.5, "limit_intraday", "scale50@1.5R"),
    LegPlan("scale", "R", 2.5, 0.5, "limit_intraday", "scale50@2.5R"),
    LegPlan("scale", "R", 3.0, 0.5, "limit_intraday", "scale50@3R"),
    LegPlan("scale", "R", 2.0, 1 / 3, "limit_intraday", "scale33@2R"),
    LegPlan("scale", "R", 2.0, 2 / 3, "limit_intraday", "scale67@2R"),
    LegPlan("scale", "ATR", 1.5, 0.5, "limit_intraday", "scale50@1.5ATR"),
    LegPlan("scale", "ATR", 2.0, 0.5, "limit_intraday", "scale50@2ATR"),
    LegPlan("scale", "ATR", 3.0, 0.5, "limit_intraday", "scale50@3ATR"),
    LegPlan("full", "R", 2.0, 1.0, "limit_intraday", "FULL@2R"),
    LegPlan("full", "R", 3.0, 1.0, "limit_intraday", "FULL@3R"),
    LegPlan("full", "ATR", 2.0, 1.0, "limit_intraday", "FULL@2ATR"),
    LegPlan("full", "ATR", 3.0, 1.0, "limit_intraday", "FULL@3ATR"),
]


def keyed(trades: list[dict]) -> dict:
    return {(t["symbol"], t["i"]): t for t in trades if t}


def paired(base: dict, test: dict) -> dict:
    """Trade-for-trade deltas, in time order so the block bootstrap means something."""
    ks = sorted(set(base) & set(test), key=lambda k: (k[1], k[0]))
    dR, dX = [], []
    for k in ks:
        dR.append(test[k]["R"] - base[k]["R"])
        if test[k]["excess"] is not None and base[k]["excess"] is not None:
            dX.append(test[k]["excess"] - base[k]["excess"])
    ciR, ciX = block_ci(dR), block_ci(dX)
    return {"n_paired": len(ks), "dR": ciR["mean"], "dR_ci": [ciR["lo95"], ciR["hi95"]],
            "dX_bp": ciX["mean"] * 10000,
            "dX_bp_ci": [(ciX["lo95"] or 0) * 10000, (ciX["hi95"] or 0) * 10000],
            "clear_of_zero": bool(ciX["lo95"] is not None
                                  and (ciX["lo95"] > 0 or ciX["hi95"] < 0))}


def random_exit_null(p: Panel, base: dict, real: dict, plan: LegPlan,
                     cfg, atrs: dict, draws: int, seed: int) -> dict:
    """Sell the same fraction on a RANDOM session instead of at the target.

    Permutes the observed scale sessions across the firing trades — preserving the
    firing set AND the holding-period distribution — then clips each to the receiving
    trade's own life. Sells at that session's CLOSE.
    """
    fired = [(k, real[k]) for k in real if real[k].get("scale_i")]
    if not fired:
        return {"n_fired": 0}
    offsets = [t["scale_i"] - t["entry_i"] for _, t in fired]
    rng = random.Random(seed)
    real_mean = statistics.fmean(real[k]["R"] for k, _ in fired)

    means = []
    for _ in range(draws):
        perm = offsets[:]
        rng.shuffle(perm)
        vals = []
        for (k, t), off in zip(fired, perm):
            b = base[k]
            ent, life = b["entry_i"], b["exit_i"] - b["entry_i"]
            if life < 1:
                vals.append(b["R"])
                continue
            j = ent + max(1, min(off, life))
            cl = p.raw_close.get(k[0], {})
            if j not in cl:
                vals.append(b["R"])
                continue
            eff = cl[j] * tb._adj_factor(p, k[0], j) / tb._adj_factor(p, k[0], ent) \
                if tb.CA_ADJUST else cl[j]
            fx = tb._adj_factor(p, k[0], b["exit_i"]) / tb._adj_factor(p, k[0], ent) \
                if tb.CA_ADJUST else 1.0
            entry, r_ps = b["entry"], (b["entry"] - b["stop"])
            cost = cfg.fee_buy + cfg.fee_sell
            w = plan.fraction
            vals.append((w * (eff - entry - entry * cost)
                         + (1 - w) * (b["exit"] * fx - entry - entry * cost)) / r_ps)
        means.append(statistics.fmean(vals))
    means.sort()
    worse = sum(1 for m in means if m <= real_mean)
    return {"n_fired": len(fired), "real_meanR": real_mean,
            "null_mean": statistics.fmean(means),
            "p05": means[int(0.05 * draws)], "p95": means[int(0.95 * draws)],
            "percentile": worse / len(means)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--draws", type=int, default=400)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=str, default=str(PANEL / "exit_test.json"))
    a = ap.parse_args()

    cfg, warn = config_from_env()
    for w in warn:
        print(f"[warn] {w}")
    print("loading panel...")
    p = Panel().load()
    print(f"  {p.describe()}")
    atrs = {s: atr_series(p, s) for s in p.raw_close}
    rv1s = {s: rvol1_series(p, s) for s in p.volume}
    cands = tb.build_candidates(p)

    c0 = tb.check_zero(p, cands, a.folds)
    print(f"  CHECK 0 lift {c0['pooled']['lift'] * 100:+.2f}pp "
          f"{'PASS' if c0['ok'] else 'FAIL'}")
    if not c0["ok"]:
        print("  [!!] hostile window — nothing read from it can separate rule from period")
        return 4

    days = sorted({c["i"] for c in cands})
    edges = [days[len(days) * k // a.folds] for k in range(a.folds)] + [days[-1] + 1]
    folds = [[c for c in cands if edges[k] <= c["i"] < edges[k + 1]] for k in range(a.folds)]

    base = keyed(tb.run_set(p, cands, cfg, RULES, atrs, rv1s))
    base_folds = [keyed(tb.run_set(p, sub, cfg, RULES, atrs, rv1s)) for sub in folds]
    bs = tb.summarise_trades(list(base.values()))
    print(f"\nINCUMBENT  ATR stop + E2  |  n {bs['n']} | meanR {bs['meanR']:+.3f} | "
          f"excess {bs['mean_excess'] * 100:+.2f}% | worst {bs['worstR']:.2f}R")
    print(f"fees {cfg.fee_buy:.4f}/{cfg.fee_sell:.4f} "
          f"({(cfg.fee_buy + cfg.fee_sell) * 100:.2f}% round trip) | "
          f"gap_fill={tb.GAP_FILL} ca_adjust={tb.CA_ADJUST}")

    rows = []
    for plan in [PRIMARY] + EXPLORATORY:
        tr = keyed(tb.run_set(p, cands, cfg, RULES, atrs, rv1s, legs=plan))
        s = tb.summarise_trades(list(tr.values()))
        pr = paired(base, tr)
        wins = 0
        for k, sub in enumerate(folds):
            sf = tb.summarise_trades(list(keyed(
                tb.run_set(p, sub, cfg, RULES, atrs, rv1s, legs=plan)).values()))
            bf = tb.summarise_trades(list(base_folds[k].values()))
            if sf.get("n") and sf["mean_excess"] > bf.get("mean_excess", 0):
                wins += 1
        n_fired = sum(1 for t in tr.values() if t.get("scale_i") or "T_" in t.get("rule", ""))
        n_both = sum(t.get("both_touched", 0) for t in tr.values())
        rows.append({"label": plan.label, "kind": plan.kind, "n": s["n"],
                     "meanR": s["meanR"], "mean_excess": s["mean_excess"],
                     "worstR": s["worstR"], "hit": s["hit"], "held": s["held"],
                     "folds_won": wins, "n_fired": n_fired, "n_both_touched": n_both,
                     "primary": plan.label == PRIMARY.label, **pr})

    print(f"\nPROFIT-RULE TABLE — paired against the incumbent, same trades")
    print(f"  {'rule':<16}{'n':>5}{'fired':>7}{'meanR':>8}{'excess':>8}{'worstR':>8}"
          f"{'dR':>8}{'d bp':>8}{'  95% CI bp':>18}{'fold':>6}{'clr':>5}")
    print("  " + "-" * 105)
    for r in sorted(rows, key=lambda x: -x["mean_excess"]):
        lo, hi = r["dX_bp_ci"]
        star = " *" if r["primary"] else "  "
        print(f"  {r['label']:<14}{star}{r['n']:>5}{r['n_fired']:>7}{r['meanR']:>8.3f}"
              f"{r['mean_excess'] * 100:>7.2f}%{r['worstR']:>8.2f}{r['dR']:>8.3f}"
              f"{r['dX_bp']:>8.1f}   [{lo:>+6.1f},{hi:>+6.1f}]{r['folds_won']:>4}/4"
              f"{'YES' if r['clear_of_zero'] else '-':>5}")
    print("  * = the one pre-registered primary. Every other row is exploratory and")
    print("    reports rank among 14; none of them can ship on its own.")

    # ---- the binding null, on the primary only
    prim = next(r for r in rows if r["primary"])
    real = keyed(tb.run_set(p, cands, cfg, RULES, atrs, rv1s, legs=PRIMARY))
    null = random_exit_null(p, base, real, PRIMARY, cfg, atrs, a.draws, a.seed)
    print(f"\nTIMING-MATCHED NULL — sell the same half on a RANDOM session ({a.draws} draws)")
    if null.get("n_fired"):
        print(f"  {null['n_fired']} trades fired the target | real meanR {null['real_meanR']:+.4f}"
              f" | null mean {null['null_mean']:+.4f} | p05 {null['p05']:+.4f} "
              f"p95 {null['p95']:+.4f}")
        print(f"  the real timing sits at the {null['percentile'] * 100:.0f}th percentile "
              f"of random timings")

    rank = sorted(rows, key=lambda x: -x["mean_excess"]).index(prim) + 1
    ship = (prim["n"] >= SHIP_N and prim["meanR"] > bs["meanR"]
            and prim["mean_excess"] > bs["mean_excess"]
            and prim["folds_won"] >= a.folds - 1
            and null.get("percentile", 0) >= 0.95 and prim["clear_of_zero"])
    print("\n" + "=" * 105)
    print(f"PRIMARY  {PRIMARY.label}  rank {rank} of {len(rows)}")
    print(f"  n {prim['n']} >= {SHIP_N}            {'PASS' if prim['n'] >= SHIP_N else 'FAIL'}")
    print(f"  beats incumbent meanR       "
          f"{'PASS' if prim['meanR'] > bs['meanR'] else 'FAIL'}"
          f"  ({prim['meanR']:+.3f} vs {bs['meanR']:+.3f})")
    print(f"  beats incumbent excess      "
          f"{'PASS' if prim['mean_excess'] > bs['mean_excess'] else 'FAIL'}"
          f"  ({prim['mean_excess'] * 100:+.2f}% vs {bs['mean_excess'] * 100:+.2f}%)")
    print(f"  folds won {prim['folds_won']}/4              "
          f"{'PASS' if prim['folds_won'] >= a.folds - 1 else 'FAIL'}")
    print(f"  >= 95th pct vs null         "
          f"{'PASS' if null.get('percentile', 0) >= 0.95 else 'FAIL'}"
          f"  ({null.get('percentile', 0) * 100:.0f}th)")
    print(f"  paired band excludes zero   {'PASS' if prim['clear_of_zero'] else 'FAIL'}")
    print(f"\nVERDICT: {'SHIPS ON ITS OWN MERIT' if ship else 'DOES NOT BEAT THE INCUMBENT'}")

    if not ship:
        cost_bp = -prim["dX_bp"]
        lo_bp = -prim["dX_bp_ci"][1]
        hi_bp = -prim["dX_bp_ci"][0]
        within = prim["dX_bp_ci"][0] >= KILL_BP
        print(f"\nCOMFORT TAX, PRICED")
        print(f"  cost {cost_bp:+.1f} bp of mean excess per trade "
              f"(95% CI {lo_bp:+.1f} to {hi_bp:+.1f} bp)")
        print(f"  declared ceiling {-KILL_BP:.0f} bp — worst case {-prim['dX_bp_ci'][0]:.1f} bp "
              f"-> {'WITHIN' if within else 'BREACHES'}")
        print(f"  {'SHIP as a deliberate, priced choice.' if within else 'DO NOT SHIP.'}")

    payload = {"panel_fingerprint": panel_fingerprint(),
               "conventions": {"gap_fill": tb.GAP_FILL, "e2_fill": tb.E2_FILL,
                               "ca_adjust": tb.CA_ADJUST},
               "fees": {"buy": cfg.fee_buy, "sell": cfg.fee_sell},
               "check_zero": c0, "incumbent": bs, "rows": rows,
               "primary": PRIMARY.label, "primary_rank": rank,
               "null": null, "ships": ship, "kill_bp": KILL_BP}
    Path(a.out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
