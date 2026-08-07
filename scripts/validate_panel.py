#!/usr/bin/env python3
"""End-to-end validation of the backfilled panel.

The panel is the product of several transformations that could each be silently wrong:
a cumulative->daily first difference, a string->number coercion, a shares/lots unit
convention, and a corporate-action back-adjustment. None of them fail loudly. So check
the output against facts established independently.

  1. GROUND TRUTH. RX on BBCA for 2026-08-04 must come out at exactly
     Rp32,399,812,500 — the figure Sectors reported and the Invezgo single-day
     endpoint confirmed. This exercises the whole chain end to end.
  2. NO IMPOSSIBLE MOVES. No adjusted daily return may exceed the IDX auto-rejection
     band; a survivor means a missed corporate action.
  3. COVERAGE. Every symbol present, no month empty, no suspicious gaps.
  4. CONSERVATION. Daily net flows across brokers should roughly cancel — IDX is a
     closed market, so every buyer has a seller. With only the top ~20 brokers per
     stock captured it will not cancel exactly, but a wild imbalance means trouble.

Usage:
    py scripts/validate_panel.py
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

PANEL = Path(__file__).resolve().parent.parent / "data" / "panel"

TRUTH = {"date": "2026-08-04", "symbol": "BBCA", "broker": "RX",
         "net_value": 32_399_812_500}

FAILURES: list[str] = []
WARNINGS: list[str] = []


def arb_limit(price: float) -> float:
    if price <= 200:
        return 0.35
    if price <= 5000:
        return 0.25
    return 0.20


def read_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main() -> int:
    flow_files = sorted(PANEL.glob("flows-*.csv.gz"))
    px_files = sorted(PANEL.glob("prices-*.csv.gz"))
    if not flow_files or not px_files:
        print(f"No panel found in {PANEL}. Run scripts/backfill_panel.py first.")
        return 2

    print(f"panel: {len(flow_files)} flow partitions, {len(px_files)} price "
          f"partitions in {PANEL}\n")

    # ---------------------------------------------------------------- 1. ground truth
    print("1. GROUND TRUTH — the number three independent sources agree on")
    month = TRUTH["date"][:7]
    hit = None
    for r in read_gz(PANEL / f"flows-{month}.csv.gz"):
        if (r["date"] == TRUTH["date"] and r["symbol"] == TRUTH["symbol"]
                and r["broker"] == TRUTH["broker"]):
            hit = int(r["net_value"])
            break
    if hit is None:
        check(False, "RX/BBCA/2026-08-04 present in panel", "row not found")
    else:
        diff = abs(hit - TRUTH["net_value"])
        check(diff <= 1,
              "RX/BBCA/2026-08-04 net flow reconciles",
              f"panel {hit:,} vs truth {TRUTH['net_value']:,} (diff {diff:,})")

    # ------------------------------------------------------------------- 2. ARB scan
    print("\n2. NO IMPOSSIBLE MOVES — adjusted returns inside auto-rejection bands")
    by_sym: dict[str, dict[str, float]] = defaultdict(dict)
    n_px = 0
    for f in px_files:
        for r in read_gz(f):
            try:
                by_sym[r["symbol"]][r["date"]] = float(r["close_adj"])
                n_px += 1
            except (KeyError, TypeError, ValueError):
                pass
    breaks = []
    for sym, series in by_sym.items():
        days = sorted(series)
        for d0, d1 in zip(days, days[1:]):
            p0, p1 = series[d0], series[d1]
            if p0 <= 0:
                continue
            ret = p1 / p0 - 1
            if abs(ret) > arb_limit(p0) + 0.05:
                breaks.append((sym, d0, d1, ret))
    check(not breaks, f"no impossible moves across {n_px:,} adjusted price rows",
          "" if not breaks else
          "; ".join(f"{s} {a}->{b} {r:+.0%}" for s, a, b, r in breaks[:6]))

    # ------------------------------------------------------------------ 3. coverage
    print("\n3. COVERAGE")
    rows = 0
    syms, brokers, dates = set(), set(), set()
    per_month: dict[str, int] = defaultdict(int)
    for f in flow_files:
        for r in read_gz(f):
            rows += 1
            syms.add(r["symbol"])
            brokers.add(r["broker"])
            dates.add(r["date"])
            per_month[r["date"][:7]] += 1
    print(f"       {rows:,} broker-day rows | {len(syms)} symbols | "
          f"{len(brokers)} brokers | {len(dates)} trading days")
    check(len(syms) >= 100, "symbol coverage", f"{len(syms)} symbols")
    check(len(dates) >= 400, "trading-day coverage",
          f"{len(dates)} days across {len(per_month)} months")
    empty = [m for m, n in per_month.items() if n == 0]
    check(not empty, "no empty months", ", ".join(empty))

    thin = sorted(per_month.items(), key=lambda kv: kv[1])[:3]
    print(f"       thinnest months: "
          + ", ".join(f"{m} ({n:,})" for m, n in thin))
    # Partial first/last months are expected at the window edges, not a defect.

    # Per-DAY density. An incremental merge once emptied a single session — flows are
    # a first difference, so the window's opening date has no diff, and deleting the
    # window then re-adding only diffed rows wiped it. That hole was invisible to the
    # idempotency test (both runs reproduced it) and to the month totals. A day far
    # below the typical count is the symptom worth failing on.
    per_day: dict[str, int] = defaultdict(int)
    for f in flow_files:
        for r in read_gz(f):
            per_day[r["date"]] += 1
    counts = sorted(per_day.values())
    med = counts[len(counts) // 2] if counts else 0
    holes = sorted(d for d, n in per_day.items() if n < med * 0.2)
    # The newest session can legitimately be thin if the vendor is mid-update.
    holes = [d for d in holes if d != max(per_day)] if per_day else []
    check(not holes, "no session is near-empty relative to the median",
          f"median {med:,}/day; suspect: " + ", ".join(
              f"{d} ({per_day[d]:,})" for d in holes[:5]))

    # -------------------------------------------------------------- 4. conservation
    print("\n4. CONSERVATION — buyers and sellers should broadly offset")
    day_sym: dict[tuple, list] = defaultdict(list)
    for f in flow_files:
        for r in read_gz(f):
            day_sym[(r["date"], r["symbol"])].append(int(r["net_value"]))
    ratios = []
    for (_, _), vals in day_sym.items():
        gross = sum(abs(v) for v in vals)
        if gross > 0:
            ratios.append(abs(sum(vals)) / gross)
    if ratios:
        ratios.sort()
        med = ratios[len(ratios) // 2]
        p90 = ratios[int(len(ratios) * 0.9)]
        print(f"       |net|/gross per stock-day: median {med:.1%}, p90 {p90:.1%}")
        # Only the top ~20 brokers per stock are captured, so residual imbalance is
        # expected — it is the uncaptured tail. A median above ~40% would instead
        # suggest the first-difference or sign convention is wrong.
        if med > 0.40:
            WARNINGS.append(
                f"median |net|/gross {med:.1%} is high — expected from a top-20 "
                f"broker subset, but verify the cumulative->daily diff if it grows")
        print(f"       (residual = the uncaptured broker tail, top ~20 per stock only)")

    # ------------------------------------------------------------------- report card
    report_path = PANEL / "backfill_report.json"
    if report_path.exists():
        rep = json.loads(report_path.read_text(encoding="utf-8"))
        failed = rep.get("symbols_failed") or []
        notes = rep.get("action_notes") or {}
        unhandled = {s: n["unhandled"] for s, n in notes.items() if n.get("unhandled")}
        print("\n5. BACKFILL REPORT")
        check(not failed, "all symbols fetched", ", ".join(failed))
        if unhandled:
            WARNINGS.append(
                f"{len(unhandled)} symbol(s) have bonus/rights actions with no ratio "
                f"field, so they are UNADJUSTED: {sorted(unhandled)[:8]}")

    print("\n" + "=" * 66)
    if WARNINGS:
        print("WARNINGS")
        for w in WARNINGS:
            print(f"  ! {w}")
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED — do not score on this panel:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED — panel is safe to score.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
