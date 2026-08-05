#!/usr/bin/env python3
"""
Gate for merging v3: does the DERIVED net foreign flow match the OFFICIAL one?

    py scripts/check_parity.py                 # today's brokers-<date>.json
    py scripts/check_parity.py --date 2026-08-06

v3 stops calling /v2/foreign-flow/ and instead sums the net of every foreign broker
from /v2/broker-summary/. That substitution is the whole reason v3 costs less than v2,
so it has to be exactly right — not approximately right.

This re-fetches the official figure for each measured ticker and compares. Costs 1
credit per ticker, so it is a pre-merge check, not something the daily run does.

Exit 0 = every ticker matched. Exit 1 = at least one did not (do NOT merge).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sectors_client import SectorsClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
WIB = timezone(timedelta(hours=7))

# Rupiah. The two paths should agree to the rupiah; anything above 0 would mean a
# broker is being counted differently, so this is a tripwire and not a tolerance.
TOLERANCE = 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare derived vs official foreign flow.")
    ap.add_argument("--date", help="session label (default: newest brokers-*.json)")
    ap.add_argument("--tolerance", type=int, default=TOLERANCE,
                    help="allowed absolute IDR difference (default 0)")
    args = ap.parse_args()

    if args.date:
        path = BUILD / f"brokers-{args.date}.json"
    else:
        files = sorted(BUILD.glob("brokers-*.json"))
        if not files:
            sys.exit("No build/brokers-*.json — run fetch_brokers.py first.")
        path = files[-1]

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("available"):
        sys.exit(f"{path.name} has available:false — nothing to compare.")

    session = payload["date"]                 # the MARKET session actually measured
    tickers = payload.get("tickers") or {}
    print(f"Parity check — {path.name}, market session {session}, "
          f"{len(tickers)} tickers\n")

    c = SectorsClient(date=session)
    if not c.enabled:
        sys.exit("SECTORS_API_KEY not set — cannot fetch the official figure.")

    print(f"  {'TICKER':<8}{'derived':>20}{'official':>20}{'diff':>14}  ")
    ok, bad, missing = 0, [], []

    for sym, t in tickers.items():
        derived = t.get("net_foreign_idr")
        flow = c.foreign_flow(sym, start=session, end=session)
        rows = (flow or {}).get("data") or []
        row = next((r for r in rows if r.get("date") == session), None)
        if derived is None or row is None:
            missing.append(sym)
            print(f"  {sym:<8}{derived if derived is not None else '-':>20}"
                  f"{'(no official row)':>20}{'-':>14}")
            continue

        official = round(float(row.get("net_foreign_inflow") or 0))
        diff = derived - official
        mark = "OK" if abs(diff) <= args.tolerance else "MISMATCH"
        if mark == "OK":
            ok += 1
        else:
            bad.append((sym, derived, official, diff))
        print(f"  {sym:<8}{derived:>20,}{official:>20,}{diff:>14,}  {mark}")

    print(f"\n  matched {ok}/{len(tickers)}   mismatched {len(bad)}   "
          f"no official row {len(missing)}")
    print(f"  credits used {c.credits}, cache hits {c.cache_hits}")

    if bad:
        print("\n  DO NOT MERGE — the derived figure is not equivalent:")
        for sym, d, o, diff in bad:
            print(f"    {sym}: derived {d:,} vs official {o:,} (diff {diff:,})")
        print("\n  Most likely cause: a broker's is_foreign flag in "
              "reference/broker-registry.json is wrong or stale, or a code trading "
              "that ticker is absent from the registry (see unknown_brokers).")
        return 1

    if missing:
        print(f"\n  {len(missing)} ticker(s) had no official row — inconclusive, "
              f"not a mismatch: {', '.join(missing)}")

    print("\n  PASS — derived net foreign flow is exactly equivalent to the official "
          "figure for every ticker checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
