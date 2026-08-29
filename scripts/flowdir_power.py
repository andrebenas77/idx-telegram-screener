#!/usr/bin/env python3
"""When may the flow-direction record be read? Framework: reference/flow-direction.md section 8.

This exists to stop a number being quoted before it means anything, including by the person who
wrote it. `flow_record.csv` accumulates one row per candidate per session with NO outcome
attached; the temptation, every week, is to join forward returns and look. This script is the
thing that says no, and says exactly why and until when.

TWO SEPARATE QUESTIONS, AND CONFLATING THEM IS THE ERROR

  * How many ROWS are there -- which grows with how many names qualify per session, and is
    almost irrelevant.
  * How many independent 30-day BLOCKS the record spans -- which grows only with the CALENDAR,
    and is what decides whether an interval is inferential.

A year of 40 names a day is still 8 blocks. `lift_lib.MIN_BLOCKS_INFERENTIAL` is 15. Below it
every interval is DESCRIPTIVE and must be labelled so; the joint-lift thesis was killed
structurally on 8.

The `block=15` arm is reported beside the primary as a pre-registered SENSITIVITY, never alone.
`lift_lib` requires the block to exceed the 95th percentile of holding period, and for a fixed
k=5 horizon 15 days is 3x -- but BLOCK_DAYS = 30 was set for first-passage barriers with 15-25
day holds and remains the primary. Declaring the sensitivity in advance is what stops it being
renegotiated later, when the primary is short and the temptation is highest.

Usage:
    py scripts/flowdir_power.py
    py scripts/flowdir_power.py --read-out        # refused until the record is readable
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lift_lib as L  # noqa: E402
from alpha_lib import PANEL, Panel  # noqa: E402

RECORD = PANEL / "flow_record.csv"
PRIMARY_K = 5
SESSIONS_PER_MONTH = 20.5           # IDX trading sessions, long-run average

# The attenuated pass bar. blockdom.md set +1.0pp on a true variable; an instrument correlated r
# with truth recovers about r times a linear effect. The gating instrument measures rho +0.856
# against true signed flow on the tapes, but its single-session reliability caps what it can
# carry at sqrt(0.270) = 0.519, and the 5-session sorter at sqrt(0.649) = 0.805. The bar is set
# from the RELIABILITY ceiling, which is the pessimistic and defensible one.
ATTENUATED_BAR_PP = 0.80


def z(p: float) -> float:
    """Inverse standard normal, Acklam-style rational approximation. Stdlib only."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def load_record():
    if not RECORD.exists():
        return []
    with RECORD.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def excess_sd(p: Panel, k: int = PRIMARY_K, sample: int = 4000) -> float:
    """Empirical sd of the k-day excess return over the panel. The scale every MDE rests on.

    Sampled ACROSS the universe, with a per-symbol quota. The first version of this walked
    `sorted(p.close)` and broke out of BOTH loops once `sample` was reached, so it consumed only
    the first 64 of 159 symbols -- alphabetically A..GEMS. That is a prefix of the alphabet, not
    a sample of the market: it over-weighted whatever happens to sort early, and adding one
    symbol with an early code (COIN, a "C") silently moved the number that every MDE and every
    readable-date estimate rests on.

    Now every symbol contributes, and the stride adapts so a short history is not over-sampled
    relative to a long one.
    """
    if not p.bench:
        raise SystemExit(
            "excess_sd: the benchmark is not loaded, so Panel.excess_return returns None for "
            "every event and this would silently report a hardcoded constant as a measurement. "
            "Call p.load_benchmark() before p.load_prices() results are used here.")
    syms = sorted(p.close)
    per = max(1, sample // max(len(syms), 1))
    xs = []
    for s in syms:
        idxs = sorted(p.close[s])
        if not idxs:
            continue
        stride = max(1, len(idxs) // per)
        for i in idxs[::stride]:
            v = p.excess_return(s, i, k)
            if v is not None:
                xs.append(v)
    if len(xs) < 100:
        raise SystemExit("excess_sd: only %d usable observations -- refusing to report a "
                         "dispersion estimate from that." % len(xs))
    return statistics.pstdev(xs)


def mde(sd: float, n_eff: float, power: float = 0.80, alpha: float = 0.10) -> float:
    """Smallest effect detectable at `power`, two-sided at `alpha`. Bands here are 10/90, so
    alpha is 0.10 rather than 0.05: with ~15 blocks a 5% tail is not resolvable and quoting one
    is the same error class as a z-test on clustered events."""
    if n_eff <= 0:
        return float("inf")
    return (z(1 - alpha / 2) + z(power)) * sd / math.sqrt(n_eff)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--read-out", action="store_true",
                    help="attempt the forward-return read-out; refused unless inferential")
    a = ap.parse_args()

    rows = load_record()
    if not rows:
        print("no record yet at %s -- run build_flow_board.py first" % RECORD)
        return 1

    p = Panel()
    p.load_prices()
    # excess_return needs the IHSG series; without it every call returns None and every
    # downstream number becomes a fallback constant wearing the costume of a measurement.
    p.load_benchmark()
    dates = sorted({r["date"] for r in rows})
    idxs = sorted({p.didx[r["date"]] for r in rows if r["date"] in p.didx})
    cells = Counter(r["cell"] for r in rows)

    b30 = L.blocks_with_treatment(idxs, 30)
    b15 = L.blocks_with_treatment(idxs, 15)
    span = (idxs[-1] - idxs[0] + 1) if idxs else 0

    print("=" * 78)
    print("FLOW-DIRECTION RECORD -- census")
    print("=" * 78)
    print("  rows            %d" % len(rows))
    print("  sessions        %d  (%s .. %s)" % (len(dates), dates[0], dates[-1]))
    print("  calendar span   %d panel sessions" % span)
    print("  cells           " + "  ".join("%s %d" % (k, v) for k, v in sorted(cells.items())))
    print("  per-session     %.1f rows" % (len(rows) / max(len(dates), 1)))
    print()
    print("  blocks, BLOCK_DAYS=30 (PRIMARY)      %2d  of %d needed  -> %s"
          % (b30, L.MIN_BLOCKS_INFERENTIAL,
             "INFERENTIAL" if L.is_inferential(b30) else "DESCRIPTIVE ONLY"))
    print("  blocks, block=15 (SENSITIVITY only)  %2d  of %d needed  -> %s"
          % (b15, L.MIN_BLOCKS_INFERENTIAL,
             "inferential" if L.is_inferential(b15) else "descriptive only"))

    need30 = max(0, L.MIN_BLOCKS_INFERENTIAL * 30 - span)
    need15 = max(0, L.MIN_BLOCKS_INFERENTIAL * 15 - span)
    print()
    print("  sessions still needed: %d at block=30 (~%.1f months), %d at block=15 (~%.1f months)"
          % (need30, need30 / SESSIONS_PER_MONTH, need15, need15 / SESSIONS_PER_MONTH))

    sd = excess_sd(p)
    print()
    print("=" * 78)
    print("POWER -- what the record could detect, against an attenuated bar of %.2fpp"
          % ATTENUATED_BAR_PP)
    print("=" * 78)
    print("  sd of the %d-day excess return   %.2fpp" % (PRIMARY_K, 100 * sd))
    n_naive = len(rows)
    n_dates = len(dates)
    print("  MDE at 80%% power, treating rows as independent   %+.2fpp  (n=%d)"
          % (100 * mde(sd, n_naive), n_naive))
    print("  MDE at 80%% power, one observation per DATE       %+.2fpp  (n=%d)"
          % (100 * mde(sd, n_dates), n_dates))
    print("  The second is the honest one. Same-day co-movement is the dominant dependence on")
    print("  IDX, so rows inside a session are close to one observation, not many.")
    target = mde(sd, n_dates)
    if target > 2 * ATTENUATED_BAR_PP / 100:
        need = (((z(0.95) + z(0.80)) * sd) / (2 * ATTENUATED_BAR_PP / 100)) ** 2
        print("  sessions needed for MDE to reach 2x the bar: ~%d (~%.1f months)"
              % (math.ceil(need), math.ceil(need) / SESSIONS_PER_MONTH))

    readable = L.is_inferential(b30) and target <= 2 * ATTENUATED_BAR_PP / 100
    print()
    print("=" * 78)
    print("VERDICT: the record is %s" % ("READABLE" if readable else "NOT READABLE"))
    print("=" * 78)
    if not readable:
        why = []
        if not L.is_inferential(b30):
            why.append("only %d of %d blocks at BLOCK_DAYS=30" % (b30, L.MIN_BLOCKS_INFERENTIAL))
        if target > 2 * ATTENUATED_BAR_PP / 100:
            why.append("MDE %.2fpp is above 2x the %.2fpp bar"
                       % (100 * target, ATTENUATED_BAR_PP))
        print("  " + "; ".join(why) + ".")
        print("  No lift, hit rate or quintile spread may be quoted from this record yet.")
        print("  A number computed now would be indistinguishable from the period.")

    if a.read_out:
        if not readable:
            print("\n--read-out REFUSED. See above. This refusal is the feature.")
            return 3
        print("\n--read-out: computing the pre-registered comparison...")
        by_cell = defaultdict(lambda: defaultdict(list))
        for r in rows:
            i = p.didx.get(r["date"])
            if i is None:
                continue
            v = p.excess_return(r["symbol"], i, PRIMARY_K)
            if v is not None:
                by_cell[r["cell"]][i].append(v)
        for cell in ("CONFIRM", "DIVERGENCE", "DIVERGENCE-WEAK"):
            per = by_cell.get(cell)
            if not per:
                print("  %-16s no observations" % cell)
                continue
            band = L.date_block_bootstrap(
                per, lambda xs: statistics.fmean(xs) if xs else None)
            nb = L.blocks_with_treatment(sorted(per), 30)
            print("  %-16s mean %+.2fpp  10/90 [%+.2f, %+.2f]  blocks %d  %s"
                  % (cell, 100 * (band["point"] or 0), 100 * (band["lo"] or 0),
                     100 * (band["hi"] or 0), nb,
                     "inferential" if L.is_inferential(nb) else "DESCRIPTIVE"))
        print("\n  check_zero on this window, with its universe convention, must be run and")
        print("  quoted beside these numbers before any of them is believed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
