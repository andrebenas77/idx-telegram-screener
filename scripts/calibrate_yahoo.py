#!/usr/bin/env python3
"""Does the free Yahoo feed still reproduce the paid panel on the price leg?

`build_daily_report.py` computes its Leg 2 sections from Yahoo rather than Invezgo, on the
strength of one measurement: on 2026-08-29, across 147 names on session 2026-08-26, the two
sources agreed on the `is_momentum` verdict for 147 of 147, with a median absolute difference of
**0.000** on rvol5, rsi and dd60.

A measurement is not a guarantee. Yahoo could change its adjustment policy, start carrying
negotiated volume, shift a timestamp convention, or quietly drop a symbol. Any of those would
move the report's Leg 2 sections without moving anything visible, and the report would go on
looking exactly as trustworthy as it does today.

So this runs daily and fails loudly. It compares the two sources on every session BOTH cover and
exits non-zero below the agreement bar, which is what lets a systemd unit alert instead of
drifting.

The comparison is on the VERDICT, not just the inputs, because the verdict is what ships. Two
sources can differ by 0.001 on rvol5 and still disagree about a name sitting on the 1.50 gate;
that name is the one that matters.

Usage:
    py scripts/calibrate_yahoo.py
    py scripts/calibrate_yahoo.py --sessions 10 --bar 0.99
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_daily_report as R  # noqa: E402
import build_momentum_board as B  # noqa: E402
from alpha_lib import Panel  # noqa: E402
from momentum_setup import is_momentum  # noqa: E402
from overlay_test import features  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=5,
                    help="how many shared sessions to compare, most recent first")
    ap.add_argument("--bar", type=float, default=0.99,
                    help="minimum verdict agreement; below this the exit code is non-zero")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    log = (lambda *x: None) if a.quiet else (lambda *x: print(*x))

    rp = Panel()
    rp.load_prices()
    pool = sorted(rp.close)
    yp, _, failed = R.build_yahoo_panel(pool, log)
    if failed:
        log("  %d symbols unavailable from Yahoo: %s" % (len(failed), ", ".join(failed[:10])))

    shared = [d for d in rp.dates if d in yp.didx][-a.sessions:]
    if not shared:
        print("no shared sessions between the panel and Yahoo", file=sys.stderr)
        return 2

    print("=" * 78)
    print("YAHOO / PANEL CALIBRATION -- Leg 2 verdict agreement")
    print("=" * 78)
    print("  %-12s %7s %9s %9s %10s %10s"
          % ("session", "n", "agree", "disagree", "med|drvol|", "med|drsi|"))

    worst, total_n, total_ok = 1.0, 0, 0
    offenders = []
    for d in shared:
        ri, yi = rp.didx[d], yp.didx[d]
        n = ok = 0
        dv, ds = [], []
        for s in pool:
            if s not in yp.close:
                continue
            rf, yf = features(rp, s, ri), features(yp, s, yi)
            if not rf or not yf or rf.get("rvol5") is None or yf.get("rvol5") is None:
                continue
            n += 1
            rv = is_momentum(rf, B.RVOL_MIN, B.DD_MIN, B.RSI_MIN, B.RVOL_MAX)
            yv = is_momentum(yf, B.RVOL_MIN, B.DD_MIN, B.RSI_MIN, B.RVOL_MAX)
            if rv == yv:
                ok += 1
            else:
                offenders.append((d, s, rf["rvol5"], yf["rvol5"], rf["rsi"], yf["rsi"],
                                  rf["dd60"], yf["dd60"], rv, yv))
            dv.append(abs(rf["rvol5"] - yf["rvol5"]))
            ds.append(abs(rf["rsi"] - yf["rsi"]))
        if not n:
            continue
        rate = ok / n
        worst = min(worst, rate)
        total_n += n
        total_ok += ok
        print("  %-12s %7d %8.1f%% %9d %10.4f %10.3f"
              % (d, n, 100 * rate, n - ok,
                 statistics.median(dv) if dv else 0, statistics.median(ds) if ds else 0))

    overall = total_ok / total_n if total_n else 0.0
    print()
    print("  overall %.2f%% on %d comparisons | worst session %.2f%% | bar %.2f%%"
          % (100 * overall, total_n, 100 * worst, 100 * a.bar))
    if offenders:
        print("\n  disagreements (the ones sitting on a gate are the ones that matter):")
        for d, s, r1, y1, r2, y2, r3, y3, rv, yv in offenders[:15]:
            print("    %s %-6s panel rvol %.3f rsi %.1f dd %+.4f -> %s | "
                  "yahoo rvol %.3f rsi %.1f dd %+.4f -> %s"
                  % (d, s, r1, r2, r3, rv, y1, y2, y3, yv))

    if overall < a.bar:
        print("\n  FAIL: agreement %.2f%% is below the %.2f%% bar. The Leg 2 sections of "
              "build_daily_report.py are no longer trustworthy from Yahoo." % (100 * overall,
                                                                              100 * a.bar))
        return 1
    print("\n  PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
