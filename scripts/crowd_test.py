#!/usr/bin/env python3
"""Is Telegram chatter telling us anything that RVOL and momentum do not?

The obvious test — does crowding predict forward returns — is not answerable yet. The
chatter history starts 2026-07-24 and the panel ends 2026-08-06, so only about three
sessions have a complete 3-day forward return. Reporting a hit rate off that would be
noise dressed as evidence.

A different question IS answerable from seven sessions, because it is cross-sectional
rather than forward-looking: **how much does chatter overlap with what price and volume
already say?** That decides whether the chatter feed is worth keeping as a separate
input at all.

  - If crowded names are simply the high-RVOL names, the momentum board already captures
    the signal and chatter adds cost without information.
  - If chatter is largely orthogonal, it is a genuinely separate attention channel and
    worth capturing forward until there is enough history to test properly.

Usage:
    py scripts/crowd_test.py
"""
from __future__ import annotations

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import Panel, summarise  # noqa: E402
from overlay_test import features  # noqa: E402

HISTORY = Path(__file__).resolve().parent.parent / "data" / "history.csv"


def spearman(xs, ys) -> float | None:
    """Rank correlation — robust to the heavy skew in post counts."""
    n = len(xs)
    if n < 10:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def main() -> int:
    p = Panel().load()
    print(f"panel: {p.describe()}")

    rows = list(csv.DictReader(HISTORY.open(encoding="utf-8")))
    dates = sorted({r["date"] for r in rows})
    print(f"chatter: {len(rows)} rows over {len(dates)} sessions "
          f"({dates[0]} .. {dates[-1]})\n")

    # Join chatter to price/volume features on the same (date, ticker).
    joined = []
    for r in rows:
        i = p.didx.get(r["date"])
        sym = r["ticker"].strip().upper()
        if i is None or sym not in p.close:
            continue
        f = features(p, sym, i)
        if not f or f.get("rvol5") is None:
            continue
        joined.append({"date": r["date"], "sym": sym,
                       "posts": int(r["posts"]), "channels": int(r["channels"]),
                       "f": f, "i": i})

    print(f"joined to panel: {len(joined)} observations "
          f"({len(rows) - len(joined)} rows dropped — ticker outside the 112-name "
          f"panel universe or insufficient history)\n")
    if len(joined) < 20:
        print("Too few to say anything. Stop here.")
        return 1

    # ---- 1. Is chatter just RVOL?
    print("1. DOES CHATTER DUPLICATE PRICE/VOLUME?  (Spearman rank correlation)")
    for label, key in (("RVOL5", "rvol5"), ("RSI(14)", "rsi"),
                       ("drawdown from 60d high", "dd60"),
                       ("20d range %", "range_pct")):
        rho = spearman([j["posts"] for j in joined],
                       [j["f"][key] for j in joined])
        if rho is None:
            continue
        strength = ("largely redundant" if abs(rho) > 0.5 else
                    "partly overlapping" if abs(rho) > 0.25 else
                    "essentially independent")
        print(f"   posts vs {label:<24} rho {rho:+.2f}   ({strength})")

    # ---- 2. Do the two screens pick the same names?
    print("\n2. DO THE CROWDED AND MOMENTUM SCREENS OVERLAP?")
    by_date = defaultdict(list)
    for j in joined:
        by_date[j["date"]].append(j)
    inter, top_n, mom_n = 0, 0, 0
    for d, js in by_date.items():
        js.sort(key=lambda x: -x["posts"])
        top = {x["sym"] for x in js[:20]}
        mom = {x["sym"] for x in js
               if 1.5 <= x["f"]["rvol5"] < 3.0 and x["f"]["dd60"] >= -0.10
               and x["f"]["rsi"] >= 55}
        inter += len(top & mom)
        top_n += len(top)
        mom_n += len(mom)
    print(f"   top-20 crowded: {top_n} name-days | momentum setup: {mom_n} name-days")
    if top_n and mom_n:
        print(f"   overlap: {inter} name-days ({inter / top_n:.0%} of crowded, "
              f"{inter / mom_n:.0%} of momentum)")
    else:
        print(f"   overlap: {inter} name-days")

    # ---- 3. Forward returns, if any are complete
    print("\n3. FORWARD RETURNS (expected to be too thin to use)")
    for k in (3, 5):
        xs = [p.excess_return(j["sym"], j["i"], k) for j in joined]
        xs = [x for x in xs if x is not None]
        if len(xs) < 30:
            print(f"   {k}d: only {len(xs)} complete observations — NOT REPORTABLE")
            continue
        s = summarise(xs)
        print(f"   {k}d: n={s['n']} hit {s['hit_rate']:.1%} "
              f"mean {s['mean_excess']:+.2%}   <- anecdote, not evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
