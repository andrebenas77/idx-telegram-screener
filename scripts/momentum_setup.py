#!/usr/bin/env python3
"""Validate the MOMENTUM setup that replaced the failed 'quiet accumulation' thesis.

The gradient check showed accumulation pays best in names that are already moving:
heavy relative volume, close to their 60-day high, strong RSI. That reading came from
looking at the WHOLE sample, so the obvious failure mode is that I fitted thresholds to
noise and am now about to build a board on them.

Two defences here:

1. **Period stability.** The setup is a fixed rule, not a fitted model, so the right
   test is whether its edge shows up in EVERY sub-period rather than being carried by
   one lucky stretch. A rule that only works in one half is not a rule.
2. **Threshold sweep.** If the edge exists only at one exact cut, it is noise. A real
   effect degrades gracefully as the thresholds move.

Reported on mean excess, market-adjusted vs IHSG, with the one-session entry lag.

Usage:
    py scripts/momentum_setup.py [--periods 4] [--sweep]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel, summarise  # noqa: E402
from broker_alpha import build_events  # noqa: E402
from overlay_test import features  # noqa: E402


def is_momentum(f: dict, rvol_min: float, dd_min: float, rsi_min: float,
                rvol_max: float = float("inf")) -> bool:
    """Accumulation into strength: heavy volume, near the highs, strong RSI.

    RVOL is a BAND, not a floor. The sweep showed the edge rising through RVOL ~2.5 and
    then inverting hard above 3.0 (-1.9% to -4.2% lift, 3d hit rate 30%, 5d 16%) —
    blow-off volume marks exhaustion, not continuation. Leaving the top open would let
    the worst events in through the same door as the best.
    """
    if f.get("rvol5") is None:
        return False
    return (rvol_min <= f["rvol5"] < rvol_max
            and f["dd60"] >= dd_min and f["rsi"] >= rsi_min)


def stats(evs, k):
    return summarise([e[f"x{k}"] for e in evs]) if evs else None


def line(label, evs, base3=None, base5=None):
    if not evs:
        return f"  {label:<30}{'—':>9}"
    s3, s5 = stats(evs, 3), stats(evs, 5)
    out = (f"  {label:<30}{s3['n']:>9,}{s3['hit_rate']:>9.1%}{s5['hit_rate']:>9.1%}"
           f"{s3['mean_excess']:>+11.2%}{s5['mean_excess']:>+11.2%}")
    if base3 is not None:
        out += f"{s3['mean_excess'] - base3:>+10.2%}{s5['mean_excess'] - base5:>+10.2%}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-adtv-pct", type=float, default=10.0)
    ap.add_argument("--min-value", type=float, default=500e6)
    ap.add_argument("--rvol-min", type=float, default=1.5)
    ap.add_argument("--rvol-max", type=float, default=3.0)
    ap.add_argument("--dd-min", type=float, default=-0.10)
    ap.add_argument("--rsi-min", type=float, default=55.0)
    ap.add_argument("--periods", type=int, default=4)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    print("loading panel…")
    p = Panel().load()
    print(f"  {p.describe()}")

    events = build_events(p, args.min_value, args.min_adtv_pct)
    kept = []
    for e in events:
        f = features(p, e["symbol"], e["i"])
        if f is None:
            continue
        e["f"] = f
        e["mom"] = is_momentum(f, args.rvol_min, args.dd_min, args.rsi_min,
                               args.rvol_max)
        kept.append(e)

    base3 = stats(kept, 3)["mean_excess"]
    base5 = stats(kept, 5)["mean_excess"]
    mom = [e for e in kept if e["mom"]]
    rest = [e for e in kept if not e["mom"]]

    print(f"\nMOMENTUM SETUP: RVOL5 >= {args.rvol_min}, within "
          f"{abs(args.dd_min):.0%} of the 60d high, RSI >= {args.rsi_min}")
    hdr = (f"  {'group':<30}{'n':>9}{'hit3':>9}{'hit5':>9}{'mean3':>11}{'mean5':>11}"
           f"{'lift3':>10}{'lift5':>10}")
    print(hdr)
    print(line("ALL accumulation", kept))
    print(line("  momentum", mom, base3, base5))
    print(line("  the rest", rest, base3, base5))

    if not mom:
        print("\nNo events match — loosen the thresholds.")
        return 1
    lift3 = stats(mom, 3)["mean_excess"] - base3
    lift5 = stats(mom, 5)["mean_excess"] - base5
    share = len(mom) / len(kept)
    print(f"\n  lift {lift3:+.2%} (3d) / {lift5:+.2%} (5d) on {share:.0%} of events")

    # ---------------------------------------------------------- period stability
    print(f"\nPERIOD STABILITY — the same rule across {args.periods} equal stretches")
    print("  (a rule carried by one lucky stretch is not a rule)")
    print(f"  {'period':<30}{'n':>9}{'hit3':>9}{'hit5':>9}{'mean3':>11}{'mean5':>11}"
          f"{'lift3':>10}{'lift5':>10}")
    n_dates = len(p.dates)
    edge = n_dates // args.periods
    per_period = []
    for q in range(args.periods):
        lo, hi = q * edge, (q + 1) * edge if q < args.periods - 1 else n_dates
        seg = [e for e in kept if lo <= e["i"] < hi]
        if not seg:
            continue
        b3, b5 = stats(seg, 3)["mean_excess"], stats(seg, 5)["mean_excess"]
        segm = [e for e in seg if e["mom"]]
        label = f"{p.dates[lo][:7]}..{p.dates[hi - 1][:7]}"
        print(line(f"  {label}", segm, b3, b5))
        if segm:
            per_period.append({
                "period": label, "n": len(segm),
                "lift3": stats(segm, 3)["mean_excess"] - b3,
                "lift5": stats(segm, 5)["mean_excess"] - b5})

    pos3 = sum(1 for r in per_period if r["lift3"] > 0)
    pos5 = sum(1 for r in per_period if r["lift5"] > 0)
    print(f"\n  positive lift in {pos3}/{len(per_period)} periods (3d), "
          f"{pos5}/{len(per_period)} (5d)")
    if per_period and pos3 == len(per_period) and pos5 == len(per_period):
        print("  VERDICT: stable in every period — safe to build on.")
    elif per_period and (pos3 >= len(per_period) * 0.75
                         or pos5 >= len(per_period) * 0.75):
        print("  VERDICT: mostly stable. Usable, but expect it to misfire in some "
              "regimes.")
    else:
        print("  VERDICT: NOT stable — the whole-sample edge is carried by a minority "
              "of periods.\n           Do not build a board on this.")

    # ------------------------------------------------------------------- sweep
    if args.sweep:
        print("\nTHRESHOLD SWEEP — a real effect should degrade gracefully")
        print(hdr)
        for lo, hi in ((1.2, 3.0), (1.5, 3.0), (2.0, 3.0), (1.5, 2.5),
                       (3.0, float("inf"))):
            for rs in (50.0, 65.0):
                sel = [e for e in kept
                       if is_momentum(e["f"], lo, args.dd_min, rs, hi)]
                cap = "inf" if hi == float("inf") else f"{hi}"
                print(line(f"  RVOL {lo}-{cap} RSI>={rs}", sel, base3, base5))

    out = {"params": vars(args), "n_events": len(kept), "n_momentum": len(mom),
           "lift3": lift3, "lift5": lift5, "per_period": per_period,
           "momentum_3d": stats(mom, 3), "momentum_5d": stats(mom, 5),
           "baseline_3d": stats(kept, 3), "baseline_5d": stats(kept, 5)}
    path = PANEL / "momentum_setup.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float),
                    encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
