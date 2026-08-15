#!/usr/bin/env python3
"""One shot at the close, or layered bids below it?

The incumbent buys the whole line at close[i+1] — effectively a market-on-close order
placed with the entire session's information. A ladder bids under the reference and
gets a better average price on the names that dip, at the cost of not being filled on
the names that run.

**The timing point that decides whether this test is valid at all.** The ladder is
placed PRE-OPEN on i+1, when only close[i] and ATR[i] are known. Referencing it to
close[i+1] would be look-ahead of the most ordinary and most fatal kind, so the
reference is close[i] — the same price trade_plan.py already publishes entry_lo/hi from.

**The trap this file is built to detect.** A ladder that fills 100% of the losers and
40% of the winners produces a BETTER mean R and a WORSE book: it buys all of what falls
and little of what rises. Mean-R across trades cannot see that, because it weights a
quarter-filled winner the same as a fully-filled loser.

    So the primary statistic is SIZE-WEIGHTED excess and total deployed-capital return.
    Mean R is a diagnostic and is labelled as such. fill_rate_on_winners is printed
    beside fill_rate_on_losers, and if the former is materially lower the policy is
    adversely selected and dies on that alone, whatever its average fill price says.

A ladder that abandons unfilled tranches also CHANGES THE TRADE SET, so it faces the
same-acceptance-rate null — the control that killed this repo's screener vetoes.

Usage:
    py scripts/entry_fill_test.py
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trade_backtest as tb  # noqa: E402
from alpha_lib import PANEL, Panel, block_ci, panel_fingerprint  # noqa: E402
from trade_lib import (atr_series, config_from_env, round_tick,  # noqa: E402
                       rvol1_series)

RULES = {"atr_stop", "E2"}


@dataclass(frozen=True)
class Ladder:
    tranches: tuple[tuple[float, float], ...]   # ((weight, atr_offset <= 0), ...)
    unfilled: str                               # "abandon" | "chase_close"
    label: str


POLICIES = [
    Ladder(((1.0, 0.0),), "chase_close", "P0 one-shot at close"),
    Ladder(((0.40, -0.10), (0.35, -0.25), (0.25, -0.40)), "abandon", "P1 ladder3 abandon"),
    Ladder(((0.40, -0.10), (0.35, -0.25), (0.25, -0.40)), "chase_close", "P2 ladder3 chase"),
    Ladder(((0.50, -0.05), (0.50, -0.15)), "abandon", "P3 ladder2 shallow"),
    Ladder(((0.50, -0.25), (0.50, -0.50)), "abandon", "P4 ladder2 deep"),
]


def fill(p: Panel, sym: str, ent: int, ref: float, atr: float, pol: Ladder):
    """Blended fill and the weight actually filled. None when nothing filled.

    A tranche fills iff the session LOW reached it, at min(limit, open) — a gap-down
    opens below the bid and fills BETTER, which is only knowable because Panel now
    carries opens.
    """
    lo = p.low.get(sym, {}).get(ent)
    op = p.open.get(sym, {}).get(ent)
    cl = p.raw_close.get(sym, {}).get(ent)
    if lo is None or cl is None:
        return None, 0.0
    got: list[tuple[float, float]] = []
    unfilled = 0.0
    for w, off in pol.tranches:
        px = round_tick(ref + off * atr, "down") if off else cl
        if off == 0.0:
            got.append((w, cl))                     # the market-on-close tranche
        elif lo <= px:
            got.append((w, min(px, op) if op else px))
        else:
            unfilled += w
    if unfilled > 0 and pol.unfilled == "chase_close":
        got.append((unfilled, cl))
        unfilled = 0.0
    if not got:
        return None, 0.0
    wt = sum(w for w, _ in got)
    return sum(w * px for w, px in got) / wt, wt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--draws", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=str, default=str(PANEL / "entry_fill_test.json"))
    a = ap.parse_args()

    cfg, _ = config_from_env()
    print("loading panel...")
    p = Panel().load()
    print(f"  {p.describe()}")
    atrs = {s: atr_series(p, s) for s in p.raw_close}
    rv1s = {s: rvol1_series(p, s) for s in p.volume}
    cands = tb.build_candidates(p)
    print(f"  {len(cands)} candidates | ladder referenced to close[i] and ATR[i], "
          f"both known pre-open on i+1")

    rows = []
    per_policy_trades = {}
    for pol in POLICIES:
        recs = []
        for c in cands:
            sym, i = c["symbol"], c["i"]
            ent = i + 1
            ref = p.raw_close.get(sym, {}).get(i)
            atr = atrs.get(sym, {}).get(i)
            if not ref or not atr:
                continue
            px, wt = fill(p, sym, ent, ref, atr, pol)
            if px is None or wt <= 0:
                continue
            t = tb.simulate(p, c, cfg, RULES, atrs, rv1s, entry_px=float(px))
            if not t or t["excess"] is None:
                continue
            recs.append(t | {"w": wt, "fill_px": px})
        if not recs:
            continue
        recs.sort(key=lambda t: (t["entry_i"], t["symbol"]))
        per_policy_trades[pol.label] = recs

        wsum = sum(t["w"] for t in recs)
        swx = sum(t["w"] * t["excess"] for t in recs) / wsum
        win = [t for t in recs if t["excess"] > 0]
        los = [t for t in recs if t["excess"] <= 0]
        ci = block_ci([t["w"] * t["excess"] for t in recs])
        rows.append({
            "label": pol.label, "n": len(recs),
            "fill_rate": wsum / len(recs),
            "fill_rate_winners": statistics.fmean([t["w"] for t in win]) if win else 0,
            "fill_rate_losers": statistics.fmean([t["w"] for t in los]) if los else 0,
            "size_weighted_excess": swx,
            "capital_units": wsum,
            "excess_per_capital": sum(t["w"] * t["excess"] for t in recs) / wsum,
            "book_excess_total": sum(t["w"] * t["excess"] for t in recs),
            "mean_R_DIAGNOSTIC": statistics.fmean([t["R"] for t in recs]),
            "mean_excess_DIAGNOSTIC": statistics.fmean([t["excess"] for t in recs]),
            "ci": [ci["lo95"], ci["hi95"]],
        })

    base = rows[0]
    print(f"\nENTRY-FILL POLICIES — primary statistic is SIZE-WEIGHTED excess")
    print(f"  {'policy':<22}{'n':>6}{'fill':>7}{'fillW':>7}{'fillL':>7}"
          f"{'sw excess':>11}{'capital':>9}{'book tot':>10}{'meanR*':>8}")
    print("  " + "-" * 90)
    for r in rows:
        print(f"  {r['label']:<22}{r['n']:>6}{r['fill_rate'] * 100:>6.0f}%"
              f"{r['fill_rate_winners'] * 100:>6.0f}%{r['fill_rate_losers'] * 100:>6.0f}%"
              f"{r['size_weighted_excess'] * 100:>10.2f}%{r['capital_units']:>9.0f}"
              f"{r['book_excess_total']:>10.2f}{r['mean_R_DIAGNOSTIC']:>8.3f}")
    print("  * meanR is a DIAGNOSTIC. A ladder that skips winners improves it while")
    print("    shrinking the book — that is the failure this table exists to expose.")

    print(f"\n  ADVERSE SELECTION CHECK  (fill rate on winners vs losers)")
    for r in rows[1:]:
        gap = r["fill_rate_winners"] - r["fill_rate_losers"]
        verdict = "ADVERSE — dies here" if gap < -0.05 else \
                  ("neutral" if abs(gap) <= 0.05 else "favourable")
        print(f"    {r['label']:<22}{gap * 100:>+7.1f}pp   {verdict}")

    # ---- same-acceptance-rate null for the policies that abandon trades
    print(f"\n  SAME-ACCEPTANCE-RATE NULL ({a.draws} draws) — a policy that abandons")
    print("  trades changes the TRADE SET, so it must beat random filters taking the")
    print("  same fraction. This is the control that killed the screener vetoes.")
    full = per_policy_trades[base["label"]]
    rng = random.Random(a.seed)
    nulls = {}
    for r in rows[1:]:
        recs = per_policy_trades[r["label"]]
        acc = r["fill_rate"]
        if acc > 0.995:
            continue
        real = r["size_weighted_excess"]
        draws = []
        for _ in range(a.draws):
            picked = [t["excess"] for t in full if rng.random() < acc]
            if picked:
                draws.append(statistics.fmean(picked))
        draws.sort()
        pct = sum(1 for d in draws if d <= real) / len(draws)
        nulls[r["label"]] = {"acceptance": acc, "percentile": pct,
                             "p05": draws[int(.05 * len(draws))],
                             "p95": draws[int(.95 * len(draws))]}
        print(f"    {r['label']:<22} acc {acc * 100:>3.0f}% | real {real * 100:+.2f}% | "
              f"null p05 {draws[int(.05 * len(draws))] * 100:+.2f}% "
              f"p95 {draws[int(.95 * len(draws))] * 100:+.2f}% | "
              f"{pct * 100:.0f}th pct")

    best = max(rows, key=lambda r: r["book_excess_total"])
    print("\n" + "=" * 90)
    print(f"VERDICT on trade-level evidence: {best['label']}")
    print(f"  book total excess {best['book_excess_total']:.2f} vs one-shot "
          f"{base['book_excess_total']:.2f} "
          f"({best['book_excess_total'] - base['book_excess_total']:+.2f})")
    print("  NOTE: this does not ship on this number. A smaller position frees a slot")
    print("  and capital for the next name, which a trade-level test cannot see —")
    print("  portfolio_sim.py is where an entry policy is actually judged.")

    Path(a.out).write_text(json.dumps(
        {"panel_fingerprint": panel_fingerprint(), "rows": rows, "nulls": nulls},
        indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
