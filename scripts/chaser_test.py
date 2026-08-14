#!/usr/bin/env python3
"""THE GATE — does chaser presence sort momentum outcomes?

    py chaser_test.py [--window 20] [--cohort lateness|same-day|foreign]

THE QUESTION
    Momentum events have chasers present almost by definition: rvol5 in [1.5,3) and
    rsi >= 55 select names already moving, which is where chasers show up. So:

        Among names that ALREADY qualify on momentum, do those the chasers have NOT yet
        found outperform those they already hold?

    H1 (declared in reference/chaser.md 5): low chaser_presence outperforms — you are
        early and they are your exit liquidity.
    H2 (declared, opposite): high chaser_presence outperforms — their arrival confirms
        institutional conviction.

    Both were declared before this ran. They are two ends of one tercile spread, not two
    independent attempts at the data.

BASELINE IS THE MOMENTUM EVENT SET, NOT THE UNIVERSE
    This is an OVERLAY. The bar is not "beats owning a top-20 name" but "improves a rule
    that already earns +2.26pp at k=5". An overlay that merely re-sorts an existing edge
    without adding to it has done nothing.

EVERY SCORE IS POINT-IN-TIME
    The chaser cohort at event i is derived from behaviour observed strictly BEFORE i, on
    a trailing window, via broker_profile.schedule(). Using a full-sample cohort would be
    lookahead and would invent an edge from nothing.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import broker_profile as bpf  # noqa: E402
import momentum_diagnose as md  # noqa: E402
from alpha_lib import PANEL, Panel, summarise  # noqa: E402

WIB = timezone(timedelta(hours=7))
HORIZONS = (3, 5, 10)
FOLDS = 4
SEED = 20260814
COHORT_FRAC = 0.25      # top quartile by score = "the chasers"
PRESENCE_WINDOW = 20    # sessions of chaser flow that count as "in town"


def measure(p: Panel, events, k: int):
    xs = [p.excess_return(s, i, k, entry_lag=1) for s, i, *_ in events]
    xs = [x for x in xs if x is not None]
    return summarise(xs) if xs else None


def pp(m, key="mean_excess"):
    if not m or m.get(key) is None:
        return "   n/a"
    return f"{m[key] * 100:+6.2f}"


def cohort_at(grid, i, frac=COHORT_FRAC, foreign=None, mode="lateness"):
    """The chaser set as known at day i. Empty before the grid starts."""
    if mode == "foreign":
        return {b for b, f in (foreign or {}).items() if f}
    s = bpf.scores_for(grid, i)
    if not s:
        return set()
    ranked = sorted(s.items(), key=lambda kv: -kv[1]["score"])
    n = max(1, int(len(ranked) * frac))
    return {b for b, _ in ranked[:n]}


def composition(p: Panel, sym: str, i: int, scores: dict, w: int = PRESENCE_WINDOW,
                by_sym=None):
    """Score-weighted COMPOSITION of the flow — chaser.md 4, as declared.

        comp = SUM_b [ net_b(s,i,W) * score_b ] / SUM_b |net_b(s,i,W)|

    Positive = the net buying is coming from persistent chasers. Negative = from
    persistent absorbers.

    NORMALISED BY TOTAL GROSS FLOW, and that is the whole point. The first
    implementation here used raw `chaser_net / ADTV` instead — a MAGNITUDE measure — and
    the identity-shuffle null caught it immediately: a randomly chosen 25% cohort still
    produced a -1.28pp tercile spread, because "cohort net flow / ADTV" is largely a
    proxy for TOTAL net flow, which is the quantity four previous theses already showed
    is not predictive. Dividing by the gross makes the measure scale-free and about WHO
    is on each side rather than HOW MUCH moved, which is what the hypothesis actually
    claims. The null is the control that surfaced the deviation; this restores the
    declared formula rather than relaxing anything.
    """
    if not scores:
        return None
    num = den = 0.0
    for b, series in (by_sym or {}).get(sym, {}).items():
        sc = scores.get(b)
        if sc is None:
            continue
        net = 0.0
        for j, v in series:
            if i - w < j <= i:
                net += v
        if net:
            num += net * sc
            den += abs(net)
    return (num / den) if den else None


def terciles(vals):
    s = sorted(vals)
    if len(s) < 6:
        return None, None
    return s[len(s) // 3], s[2 * len(s) // 3]


def by_fold(p: Panel, events, k, folds=FOLDS):
    """Split by calendar period. Folds with no events are returned as None and must be
    reported as UNAVAILABLE, not counted as failures: the point-in-time score grid needs a
    250-session burn-in, so on a 476-session panel the first two folds are structurally
    empty. Counting them as negative would fail a rule for having been careful about
    lookahead."""
    if not p.dates:
        return []
    size = len(p.dates) / folds
    return [measure(p, [e for e in events if int(f * size) <= e[1] < int((f + 1) * size)], k)
            for f in range(folds)]


def index_flows(p: Panel):
    by_sym = defaultdict(dict)
    for (sym, b), series in p.flows.items():
        by_sym[sym][b] = series
    return by_sym


def run(p: Panel, grid, foreign, mode, window, mom, by_sym):
    """Returns (all, low, mid, high) event lists split by flow COMPOSITION."""
    rows = []
    for sym, i, feat, net, adtv in mom:
        if i + max(HORIZONS) + 1 >= len(p.dates):
            continue
        sc = bpf.scores_for(grid, i)
        if not sc:
            continue
        flat = {b: v["score"] for b, v in sc.items()}
        if mode == "foreign":
            flat = {b: (1.0 if foreign.get(b) else -1.0) for b in flat}
        pr = composition(p, sym, i, flat, window, by_sym)
        if pr is None:
            continue
        rows.append((sym, i, pr, 0))
    if not rows:
        return [], [], [], []
    lo_cut, hi_cut = terciles([r[2] for r in rows])
    if lo_cut is None:
        return [], [], [], []
    low = [r for r in rows if r[2] <= lo_cut]
    high = [r for r in rows if r[2] >= hi_cut]
    mid = [r for r in rows if lo_cut < r[2] < hi_cut]
    return rows, low, mid, high


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", type=int, default=PRESENCE_WINDOW)
    ap.add_argument("--cohort", default="lateness",
                    choices=("lateness", "same-day", "foreign"))
    args = ap.parse_args()

    p = Panel().load()
    o = bpf.build_observations(p)
    field = {"lateness": "xr_trail", "same-day": "xr_same"}.get(args.cohort, "xr_trail")
    grid = bpf.schedule(p, o, field=field)
    foreign = bpf.load_is_foreign()

    print(f"CHASER OVERLAY GATE — cohort={args.cohort}, presence window={args.window}d")
    print(f"  panel {len(p.turnover)} symbols x {len(p.dates)} sessions")
    print(f"  cohort = top {COHORT_FRAC:.0%} by score, POINT-IN-TIME "
          f"({len(grid)} monthly re-estimates)")

    mom, _ = md.build_all(p)          # computed ONCE; run() was rebuilding it per call
    by_sym = index_flows(p)
    rows, low, mid, high = run(p, grid, foreign, args.cohort, args.window, mom, by_sym)
    if not rows:
        print("  no events", file=sys.stderr)
        return 1

    base = [(s, i, 0.0, 0) for s, i, *_ in rows]
    print(f"\nBASELINE — all momentum events with a point-in-time cohort  n={len(base)}")
    for k in HORIZONS:
        print(f"  k={k:<3} {pp(measure(p, base, k))}pp")
    bm = {k: (measure(p, base, k) or {}).get("mean_excess") or 0.0 for k in HORIZONS}

    print(f"\nTERCILES BY CHASER PRESENCE")
    print(f"  {'':<26}{'n':>6}{'k=3':>9}{'k=5':>9}{'k=10':>9}")
    out = {}
    for name, evs in (("LOW  (not in town)", low), ("MID", mid),
                      ("HIGH (already here)", high)):
        bits = []
        for k in HORIZONS:
            m = measure(p, evs, k)
            bits.append(pp(m))
            out.setdefault(name, {})[k] = (m or {}).get("mean_excess")
        print(f"  {name:<26}{len(evs):>6}" + "".join(f"{b:>9}" for b in bits))

    spread5 = None
    if out.get("LOW  (not in town)", {}).get(5) is not None \
            and out.get("HIGH (already here)", {}).get(5) is not None:
        spread5 = (out["LOW  (not in town)"][5] - out["HIGH (already here)"][5]) * 100
    print(f"\n  LOW - HIGH spread at k=5: "
          + ("n/a" if spread5 is None else f"{spread5:+.2f}pp")
          + ("   (H1: low outperforms)" if (spread5 or 0) > 0
             else "   (H2: high outperforms)"))

    # ---- sub-periods on the favoured side
    fav_name = "LOW  (not in town)" if (spread5 or 0) > 0 else "HIGH (already here)"
    opp_name = "HIGH (already here)" if (spread5 or 0) > 0 else "LOW  (not in town)"
    fav = low if fav_name.startswith("LOW") else high
    opp = high if fav_name.startswith("LOW") else low
    ff, of = by_fold(p, fav, 5), by_fold(p, opp, 5)
    bits, pos, avail = [], 0, 0
    for a, b in zip(ff, of):
        if not a or not b or a.get("mean_excess") is None or b.get("mean_excess") is None:
            bits.append("   n/a")
        else:
            avail += 1
            d = (a["mean_excess"] - b["mean_excess"]) * 100
            bits.append(f"{d:+6.2f}")
            pos += d > 0
    print(f"  spread by sub-period (k=5): {' '.join(bits)}    {pos}/{avail} of the "
          f"AVAILABLE folds positive ({FOLDS - avail} empty: the point-in-time grid "
          f"needs a {bpf.WINDOW}-session burn-in)")

    # ---- nulls
    rng = random.Random(SEED)
    print(f"\nNULLS (k=5)")
    # Null A: shuffle scores across broker identities. Preserves the score DISTRIBUTION
    # and destroys the identity<->score link. This is the control that was vacuous in the
    # accumulation gate; here identity IS the feature, so it finally bites.
    shuf = {}
    for a, s in grid.items():
        codes = list(s)
        vals = [s[c]["score"] for c in codes]
        rng.shuffle(vals)
        shuf[a] = {c: {"score": v} for c, v in zip(codes, vals)}
    _, nl, _, nh = run(p, shuf, foreign, args.cohort, args.window, mom, by_sym)
    ml, mh = measure(p, nl, 5), measure(p, nh, 5)
    ns = ((ml["mean_excess"] - mh["mean_excess"]) * 100
          if ml and mh and ml.get("mean_excess") is not None
          and mh.get("mean_excess") is not None else None)
    flag = ("   <-- exceeds +-0.3pp: the identity link is not what drives it"
            if ns is not None and abs(ns) > 0.30 else "")
    print(f"  identity shuffle   LOW-HIGH spread "
          + ("n/a" if ns is None else f"{ns:+.2f}pp") + flag)

    # ---- verdict
    fav5 = out[fav_name][5]
    checks = [
        ("1. |spread| >= 1.5pp at k=5", spread5 is not None and abs(spread5) >= 1.5,
         "n/a" if spread5 is None else f"{spread5:+.2f}pp"),
        ("2. favoured tercile >= baseline + 0.5pp",
         fav5 is not None and (fav5 * 100) >= bm[5] * 100 + 0.5,
         f"{(fav5 or 0) * 100:+.2f} vs {bm[5] * 100 + 0.5:+.2f}"),
        ("3. spread positive in >=3/4 of AVAILABLE sub-periods",
         avail >= 2 and pos / avail >= 0.75, f"{pos}/{avail} available"),
        ("4. identity-shuffle null within +-0.3pp",
         ns is not None and abs(ns) <= 0.30, "n/a" if ns is None else f"{ns:+.2f}pp"),
        ("5. n >= 300 per tercile", min(len(low), len(high)) >= 300,
         f"low={len(low)} high={len(high)}"),
    ]
    print(f"\n{'=' * 70}\nVERDICT against reference/chaser.md 8\n{'=' * 70}")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<44} {detail}")
    passed = all(ok for _, ok, _ in checks)
    print(f"\n  {'GATE PASSED' if passed else 'GATE FAILED'} — "
          + ("the overlay may ship" if passed
             else "no signal; do not ship, and do not run a sixth variant"))

    (PANEL / "chaser_test.json").write_text(json.dumps({
        "generated_at": datetime.now(WIB).isoformat(),
        "cohort": args.cohort, "window": args.window,
        "n_events": len(rows), "baseline": bm,
        "terciles": {k: {str(kk): vv for kk, vv in v.items()} for k, v in out.items()},
        "spread5_pp": spread5, "folds_positive": pos, "null_spread_pp": ns,
        "passed": passed,
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks],
    }, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    return 0 if passed else 3


if __name__ == "__main__":
    sys.exit(main())
