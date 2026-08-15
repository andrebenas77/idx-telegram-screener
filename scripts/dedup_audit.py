#!/usr/bin/env python3
"""Does the momentum board survive being counted properly?

    py dedup_audit.py [--selftest]

WHAT THIS IS AUDITING
    `broker_alpha.build_events()` emits one event per (broker, symbol, day) — correct for
    a broker-ranking question, wrong for a stock-level one. `momentum_setup.py` consumed
    it without deduping, so the momentum board's published validation
    (+0.96pp at 3d, +1.40pp at 5d, "4 of 4 sub-periods", n=2,502) counted each stock-day
    once per qualifying broker. On the current panel ~45% of rows are duplicate outcomes.

    This is the ONE rule still being traded, so the question is not academic: does the
    edge survive, and by how much do the confidence claims shrink?

TWO EFFECTS, AND THEY PULL DIFFERENTLY
    Weighting bias  — the mean tilts toward stock-days with broad broker participation,
                      which can move it in EITHER direction.
    Overstated CI   — every interval and every sub-period count was computed on roughly
                      twice the true independent n.

    So a deduped mean that comes out LOWER is not automatically "the edge was fake"; it
    could mean breadth genuinely helps and the old number was a breadth-weighted average.
    Section 4 measures breadth directly so the two can be told apart.

Reports everything side by side rather than replacing the old numbers, because the point
is the comparison.
"""
from __future__ import annotations

import argparse
import bisect
import json
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel, summarise  # noqa: E402
from broker_alpha import build_events, dedupe_stock_days  # noqa: E402
from momentum_setup import is_momentum  # noqa: E402
from overlay_test import features  # noqa: E402

WIB = timezone(timedelta(hours=7))
HORIZONS = (3, 5, 10)
FOLDS = 4
SEED = 20260814

MIN_VALUE, MIN_ADTV_PCT = 500e6, 10.0
RVOL_MIN, RVOL_MAX, DD_MIN, RSI_MIN = 1.5, 3.0, -0.10, 55.0
EXHAUST_RVOL = 3.0


def xs(p: Panel, evs, k):
    out = []
    for e in evs:
        r = p.excess_return(e["symbol"], e["i"], k, entry_lag=1)
        if r is not None:
            out.append(r)
    return out


def stat(p: Panel, evs, k):
    v = xs(p, evs, k)
    return summarise(v) if v else None


def fmt(s):
    if not s or not s.get("n"):
        return f"{'n=0':>34}"
    return (f"n={s['n']:>5}  hit {s['hit_rate'] * 100:>5.1f}%  "
            f"wilson {s['wilson'] * 100:>5.1f}%  mean {s['mean_excess'] * 100:+6.2f}pp")


def classify(p: Panel, evs):
    """Split accumulation events into momentum / exhaustion, attaching features once."""
    mom, exh, base = [], [], []
    for e in evs:
        f = features(p, e["symbol"], e["i"])
        if not f or f.get("rvol5") is None:
            continue
        e = dict(e)
        e["f"] = f
        base.append(e)
        if is_momentum(f, RVOL_MIN, DD_MIN, RSI_MIN, RVOL_MAX):
            mom.append(e)
        elif f["rvol5"] >= EXHAUST_RVOL:
            exh.append(e)
    return base, mom, exh


def folds(p: Panel, evs, k, n=FOLDS):
    size = len(p.dates) / n
    return [stat(p, [e for e in evs if int(j * size) <= e["i"] < int((j + 1) * size)], k)
            for j in range(n)]


def block_bootstrap(p: Panel, evs, k, cut_i, label):
    """How unusual is the recent stretch, against contiguous blocks of the same size?

    Contiguous blocks, not a z-test: momentum events cluster in time and share market
    exposure, so independence fails badly and a naive standard error is far too small.
    """
    ordered = sorted(evs, key=lambda e: e["i"])
    recent = [e for e in ordered if e["i"] >= cut_i]
    n = len(xs(p, recent, k))
    if n < 20:
        return None
    mr = statistics.fmean(xs(p, recent, k))
    vals = []
    for j in range(len(ordered) - len(recent)):
        blk = xs(p, ordered[j:j + len(recent)], k)
        if len(blk) >= n * 0.8:
            vals.append(statistics.fmean(blk))
    if not vals:
        return None
    worse = sum(1 for v in vals if v <= mr)
    return {"label": label, "n": n, "mean": mr, "blocks": len(vals),
            "worse": worse, "pct": 100.0 * worse / len(vals)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    p = Panel().load()
    raw = build_events(p, MIN_VALUE, MIN_ADTV_PCT)
    ded = dedupe_stock_days(raw)

    uniq = len({(e["symbol"], e["i"]) for e in raw})
    print(f"DEDUP AUDIT — momentum board, {len(p.turnover)} symbols x {len(p.dates)} "
          f"sessions")
    print(f"  accumulation events : {len(raw):,} broker-days -> {len(ded):,} stock-days "
          f"({100 * (1 - len(ded) / max(1, len(raw))):.0f}% were duplicates)")
    assert len(ded) == uniq, f"dedupe produced {len(ded)} rows for {uniq} unique keys"
    print(f"  unique (symbol, i)  : {uniq:,}  [assertion passed]")

    b_raw, m_raw, e_raw = classify(p, raw)
    b_ded, m_ded, e_ded = classify(p, ded)

    print(f"\n{'=' * 78}\nSIDE BY SIDE — per broker-day (as published) vs per stock-day"
          f"\n{'=' * 78}")
    for k in HORIZONS:
        print(f"\n  k={k}")
        for label, rows_raw, rows_ded in (
                ("all accumulation", b_raw, b_ded),
                ("MOMENTUM setup", m_raw, m_ded),
                ("exhaustion (rvol>=3)", e_raw, e_ded)):
            sr, sd = stat(p, rows_raw, k), stat(p, rows_ded, k)
            print(f"    {label:<22} broker-day  {fmt(sr)}")
            print(f"    {'':<22} STOCK-DAY   {fmt(sd)}")
        # the published number is a LIFT over the accumulation baseline, not a raw mean
        for tag, rows, base in (("broker-day", m_raw, b_raw), ("STOCK-DAY", m_ded, b_ded)):
            sm, sb = stat(p, rows, k), stat(p, base, k)
            if sm and sb:
                print(f"    {'momentum LIFT':<22} {tag:<11} "
                      f"{(sm['mean_excess'] - sb['mean_excess']) * 100:+6.2f}pp")

    print(f"\n{'=' * 78}\nSUB-PERIODS — momentum lift over the accumulation baseline, k=5"
          f"\n{'=' * 78}")
    for tag, rows, base in (("broker-day", m_raw, b_raw), ("STOCK-DAY", m_ded, b_ded)):
        fm, fb = folds(p, rows, 5), folds(p, base, 5)
        bits, pos, avail = [], 0, 0
        for a, b in zip(fm, fb):
            if not a or not b:
                bits.append("   n/a")
                continue
            avail += 1
            d = (a["mean_excess"] - b["mean_excess"]) * 100
            bits.append(f"{d:+6.2f}")
            pos += d > 0
        print(f"  {tag:<11} {' '.join(bits)}    {pos}/{avail} positive")

    print(f"\n{'=' * 78}\nRECENT STRETCH — block bootstrap from 2026-05-18\n{'=' * 78}")
    cut = p.didx.get("2026-05-18", 0)
    for tag, rows in (("broker-day", m_raw), ("STOCK-DAY", m_ded)):
        bb = block_bootstrap(p, rows, 5, cut, tag)
        if bb:
            print(f"  {tag:<11} n={bb['n']:>4}  mean {bb['mean'] * 100:+.2f}pp  "
                  f"{bb['worse']}/{bb['blocks']} blocks this bad "
                  f"({bb['pct']:.1f}th percentile)")
        else:
            print(f"  {tag:<11} too few events to bootstrap")

    print(f"\n{'=' * 78}\nBREADTH — does it matter how many brokers qualified?"
          f"\n{'=' * 78}")
    print(f"  The duplication was implicitly weighting by this. Report only; not a rule.")
    for lo, hi, lbl in ((1, 1, "1 broker"), (2, 2, "2 brokers"), (3, 99, "3+ brokers")):
        sub = [e for e in m_ded if lo <= e.get("n_brokers", 1) <= hi]
        s = stat(p, sub, 5)
        print(f"    {lbl:<12} {fmt(s)}")

    verdict = {}
    sm, sb = stat(p, m_ded, 5), stat(p, b_ded, 5)
    lift5 = ((sm["mean_excess"] - sb["mean_excess"]) * 100) if sm and sb else None
    fm, fb = folds(p, m_ded, 5), folds(p, b_ded, 5)
    pos = sum(1 for a, b in zip(fm, fb) if a and b
              and a["mean_excess"] > b["mean_excess"])
    avail = sum(1 for a, b in zip(fm, fb) if a and b)
    print(f"\n{'=' * 78}\nVERDICT\n{'=' * 78}")
    holds = lift5 is not None and lift5 >= 0.7 and pos >= 3
    print(f"  deduped 5d lift {lift5:+.2f}pp (threshold +0.70pp), "
          f"sub-periods {pos}/{avail} (threshold 3)")
    print(f"  [{'HOLDS' if holds else 'RESTATE'}] " + (
        "the momentum board survives a stricter count — keep trading it"
        if holds else
        "the board's published claims must be restated on the page and in Telegram"))
    verdict = {"lift5_deduped": lift5, "folds_positive": pos, "folds_available": avail,
               "holds": holds}

    (PANEL / "dedup_audit.json").write_text(json.dumps({
        "generated_at": datetime.now(WIB).isoformat(),
        "events_broker_day": len(raw), "events_stock_day": len(ded),
        "verdict": verdict,
    }, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {PANEL / 'dedup_audit.json'}")
    return 0


def selftest() -> int:
    fails = []

    def check(name, got, want):
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<52} got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    evs = [
        {"symbol": "AAA", "i": 10, "net": 1e9, "broker": "X", "x3": 0.01, "x5": 0.02},
        {"symbol": "AAA", "i": 10, "net": 5e9, "broker": "Y", "x3": 0.01, "x5": 0.02},
        {"symbol": "AAA", "i": 10, "net": 2e9, "broker": "Z", "x3": 0.01, "x5": 0.02},
        {"symbol": "BBB", "i": 10, "net": 3e9, "broker": "X", "x3": -0.03, "x5": -0.04},
        {"symbol": "AAA", "i": 11, "net": 1e9, "broker": "X", "x3": 0.05, "x5": 0.06},
    ]
    d = dedupe_stock_days(evs)
    check("collapses to unique (symbol, i)", len(d), 3)
    check("matches the unique-key count",
          len(d), len({(e["symbol"], e["i"]) for e in evs}))

    aaa10 = next(r for r in d if r["symbol"] == "AAA" and r["i"] == 10)
    check("counts every qualifying broker", aaa10["n_brokers"], 3)
    check("keeps the LARGEST broker as representative", aaa10["broker"], "Y")
    check("net_max is the largest single net", aaa10["net_max"], 5e9)
    check("net_sum totals the group", aaa10["net_sum"], 8e9)
    check("forward return is unchanged by dedup", aaa10["x5"], 0.02)

    bbb = next(r for r in d if r["symbol"] == "BBB")
    check("a single-broker day is untouched", bbb["n_brokers"], 1)
    check("its return survives", bbb["x5"], -0.04)

    check("output is sorted by (i, symbol)",
          [(r["i"], r["symbol"]) for r in d],
          [(10, "AAA"), (10, "BBB"), (11, "AAA")])
    check("idempotent", len(dedupe_stock_days(d)), 3)
    check("empty input is safe", dedupe_stock_days([]), [])

    print()
    if fails:
        print(f"SELFTEST FAILED: {len(fails)} check(s) -> {', '.join(fails)}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
