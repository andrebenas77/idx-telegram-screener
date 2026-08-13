#!/usr/bin/env python3
"""Why is the momentum board negative over the last quarter?

The board's rule was validated at +0.96pp (3d) / +1.40pp (5d), 4 of 4 sub-periods,
n=2,502. Re-measured on the rebuilt 159-name panel it earns +2.26pp (5d) over two years
but **-1.39pp since 2026-05-18**. The board is live and pushing signals daily, so the
question is not academic.

Four candidate explanations, and this script is built to separate them:

  A. COHORT ARTIFACT. The panel was rebuilt from 112 to 159 names on 2026-08-13, adding 47
     value-discovered names. Many are speculative second-liners (IATA, JGLE, KOTA, BNBR,
     ZATA). If the recent damage is concentrated there, it is a measurement change I
     introduced, not a change in the market or the rule.
  B. REGIME. The whole long-accumulation family stops working for a stretch. Then the rule
     is fine and the quarter is not.
  C. DECAY. The edge shrinks monotonically over time. That is the one that would mean
     retiring the rule.
  D. COMPOSITION. The rule is firing on a different KIND of event than it used to —
     bigger RVOL, weaker RSI, smaller names — so the population changed underneath it.

Read A first. It is the cheapest to check and the most likely to be my own fault.

FINDINGS — 2026-08-13. Answer: NONE OF THE FOUR. It is an unusually bad stretch, and it
sits at about the 5th percentile of the rule's own history. Do not retire the rule.

  A. COHORT — ruled out. Since 2026-05-18 the original 112 names carry 93 of the 97
     events at -1.25pp (5d); the 47 added names contribute 4. The panel change is not the
     cause. (Aside: those 47 were historically the BEST cohort, +4.96pp on n=414 — they
     are high-beta second-liners and they ran hard before this window.)

  B. REGIME — ruled out, and the obvious mechanism was tested and refuted. The rule does
     BEST in rising markets (+2.99pp, n=1,488) and worst in falling ones (+0.59pp,
     n=453), so a market bounce should help it, not hurt it. The "rebound" case
     specifically (index +3% over 20d while still 8% below its 60d high) is +1.76pp,
     n=109 — positive. And the mix of index states since 2026-05-18 is essentially
     unchanged from before: 18/34/48% falling/flat/rising against 16/33/52%.

  C. DECAY — not supported. The quarterly series is +0.88, +0.28, +3.38, +2.57, +2.11,
     +2.89, +1.71, then -1.44. That is one bad patch, not a slide, and 2025-Q1 (+0.28)
     was similarly weak before recovering to +3.38.

  D. COMPOSITION — barely moved. rvol5 1.98->1.96, rsi 74.8->73.3, dd60 -2.1%->-3.8%.
     Median event ADTV fell Rp24bn->Rp16bn and mean size rose 43%->50% of ADTV, but both
     liquidity halves are negative (-1.30 above median, -1.50 below), so it is not that.

  THE STATISTICS, and why the two disagree. Per-event 5d excess has sd 10.76pp. Treating
  events as independent gives SE = 1.09pp on n=97 and z = -3.47, which looks decisive.
  It is not: momentum events cluster heavily in time and share market exposure, so
  independence is badly violated. The correct test is the block bootstrap — of 2,722
  contiguous 97-event windows in history, 141 (5.2%) were this bad or worse, and the 5th
  percentile is -1.42pp against the observed -1.39pp.

  So: roughly a 1-in-20 stretch. Bad enough to watch, not bad enough to act on. Note also
  that the rule now fires ~3x less often than in 2025 (511/759/566/510 events per quarter,
  then 236, then 78), so accumulating enough new events to revisit this will take months —
  a verdict cannot be reached quickly, and that is itself worth knowing.

Usage:
    py momentum_diagnose.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel, summarise  # noqa: E402
from momentum_setup import is_momentum  # noqa: E402
from overlay_test import features  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TICKERS = ROOT / "reference" / "tickers.csv"

MIN_VALUE, MIN_ADTV_PCT = 500e6, 10.0
RVOL_MIN, RVOL_MAX, DD_MIN, RSI_MIN = 1.5, 3.0, -0.10, 55.0
EXHAUST_RVOL = 3.0
HORIZONS = (3, 5, 10)


def pp(v):
    return "  n/a " if v is None else f"{v * 100:+6.2f}"


def build_all(p: Panel):
    """Every momentum event and every exhaustion event, with their features."""
    mom, exh = [], []
    for (sym, broker), series in p.flows.items():
        by_i = dict(series)
        for i, net in series:
            if i < 20 or i + max(HORIZONS) + 1 >= len(p.dates):
                continue
            if net is None or net < MIN_VALUE:
                continue
            a = (p.adtv.get(sym) or {}).get(i)
            if not a or net < (MIN_ADTV_PCT / 100.0) * a:
                continue
            if sum(1 for j in range(i - 2, i + 1) if by_i.get(j, 0) > 0) < 2:
                continue
            f = features(p, sym, i)
            if not f or f.get("rvol5") is None:
                continue
            rec = (sym, i, f, net, a)
            if is_momentum(f, RVOL_MIN, DD_MIN, RSI_MIN, RVOL_MAX):
                mom.append(rec)
            elif f["rvol5"] >= EXHAUST_RVOL:
                exh.append(rec)
    return mom, exh


def measure(p: Panel, evs, k):
    xs = [p.excess_return(s, i, k, entry_lag=1) for s, i, *_ in evs]
    xs = [x for x in xs if x is not None]
    return summarise(xs) if xs else None


def line(label, p, evs, width=30):
    bits = []
    for k in HORIZONS:
        r = measure(p, evs, k)
        bits.append(pp(r["mean_excess"]) if r else "  n/a ")
    n = len(evs)
    return f"  {label:<{width}} n={n:>5}   " + "   ".join(bits)


def main() -> int:
    p = Panel().load()
    print(f"MOMENTUM BOARD DIAGNOSIS — {len(p.turnover)} symbols, {len(p.dates)} sessions "
          f"({p.dates[0]} .. {p.dates[-1]})")
    print(f"  rule: net>=max(Rp500m, 10% ADTV), buying 2 of last 3, "
          f"rvol5 in [1.5,3.0), dd60>=-10%, rsi>=55")
    print(f"  excess vs IHSG, entry_lag=1.  Columns: 3d / 5d / 10d mean excess (pp)\n")

    mom, exh = build_all(p)
    print(line("ALL momentum events", p, mom))
    print(line("ALL exhaustion events (rvol>=3)", p, exh))

    # ---------------------------------------------------------------- A. cohort
    pool = PANEL / "universe_pool.json"
    new = set()
    if pool.exists():
        new = {s.upper() for s in
               json.loads(pool.read_text(encoding="utf-8")).get("new_to_panel", [])}
    cut = p.didx.get("2026-05-18", 0)

    print(f"\nA. COHORT — was it the 47 names I added to the panel on 2026-08-13?")
    print(f"   ({len(new)} discovered names; the rest are the original curated list)")
    for label, lo, hi in (("full history", 0, len(p.dates)),
                          ("before 2026-05-18", 0, cut),
                          ("since 2026-05-18", cut, len(p.dates))):
        old_e = [e for e in mom if lo <= e[1] < hi and e[0] not in new]
        new_e = [e for e in mom if lo <= e[1] < hi and e[0] in new]
        print(f"   {label}")
        print(line("original 112 names", p, old_e, width=28))
        print(line("newly added 47 names", p, new_e, width=28))

    # ---------------------------------------------------------------- B/C. time
    print(f"\nB/C. REGIME vs DECAY — by calendar quarter")
    print(f"   {'quarter':<10} {'n':>5}   {'3d':>6} {'5d':>6} {'10d':>6}   "
          f"{'IHSG':>7}  original-112 only (5d)")
    byq = defaultdict(list)
    for e in mom:
        byq[p.dates[e[1]][:7][:5] + ("Q1" if p.dates[e[1]][5:7] <= "03" else
                                     "Q2" if p.dates[e[1]][5:7] <= "06" else
                                     "Q3" if p.dates[e[1]][5:7] <= "09" else "Q4")
             ].append(e)
    for q in sorted(byq):
        evs = byq[q]
        idxs = [e[1] for e in evs]
        b0, b1 = p.bench.get(min(idxs)), p.bench.get(max(idxs))
        ihsg = (b1 / b0 - 1) if (b0 and b1) else None
        old_only = [e for e in evs if e[0] not in new]
        r5o = measure(p, old_only, 5)
        bits = []
        for k in HORIZONS:
            r = measure(p, evs, k)
            bits.append(pp(r["mean_excess"]) if r else "  n/a ")
        print(f"   {q:<10} {len(evs):>5}   " + " ".join(bits)
              + f"   {pp(ihsg)}%  " + (pp(r5o["mean_excess"]) if r5o else "  n/a ")
              + f" (n={len(old_only)})")

    # ---------------------------------------------------------------- D. composition
    print(f"\nD. COMPOSITION — is it firing on a different kind of event?")
    def stats(evs, key):
        vals = [e[2].get(key) for e in evs if e[2].get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    for label, lo, hi in (("before 2026-05-18", 0, cut),
                          ("since 2026-05-18", cut, len(p.dates))):
        evs = [e for e in mom if lo <= e[1] < hi]
        adtvs = [e[4] for e in evs]
        pcts = [100.0 * e[3] / e[4] for e in evs if e[4]]
        print(f"   {label:<20} n={len(evs):>5}  "
              f"rvol5 {stats(evs, 'rvol5'):.2f}  rsi {stats(evs, 'rsi'):.1f}  "
              f"dd60 {stats(evs, 'dd60') * 100:+.1f}%  "
              f"median ADTV Rp{sorted(adtvs)[len(adtvs) // 2] / 1e9:,.0f}bn  "
              f"mean net {sum(pcts) / len(pcts):.0f}% ADTV")

    # liquidity tier — the board itself splits Tier A (top 50 by ADTV) from Tier B
    print(f"\n   by liquidity tier, since 2026-05-18")
    recent = [e for e in mom if e[1] >= cut]
    if recent:
        med = sorted(e[4] for e in recent)[len(recent) // 2]
        print(line(f"above median ADTV (>Rp{med / 1e9:,.0f}bn)", p,
                   [e for e in recent if e[4] >= med], width=28))
        print(line(f"below median ADTV", p,
                   [e for e in recent if e[4] < med], width=28))

    print(f"\n   READING: if 'newly added 47' carries the damage, this is my panel change,")
    print(f"   not the market. If both cohorts fall together, it is regime or decay — and")
    print(f"   decay shows as a monotonic slide across quarters, regime as one bad patch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
