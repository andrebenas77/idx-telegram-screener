#!/usr/bin/env python3
"""Broker Alpha Leaderboard — which IDX brokers actually predict 3-5 day returns?

Scores every broker by how OFTEN the names it accumulates beat the index over the next
3 and 5 sessions, then checks whether that ranking survives out of sample.

Four guards, each of which changes the answer:

1. **Market-adjusted.** Excess over IHSG. The index fell 10.9% across this window, so
   raw returns would rank brokers by beta rather than by skill.
2. **Entry lag of one session.** Flow for day t publishes after the close, so entry is
   at close[t+1]. Scoring from close[t] would book a day of return that was never
   tradeable — the standard way to manufacture a fake edge.
3. **Wilson lower bound**, not the raw hit rate, so a broker with a handful of lucky
   trades cannot top the board.
4. **Walk-forward.** Rank on a training window, measure on the NEXT window, roll. An
   in-sample broker ranking is guaranteed to look profitable; only the out-of-sample
   number means anything.

A null control (broker labels shuffled across the same events) is run every time. If
the null does not land near 50%, the harness is broken and no result should be believed.

Usage:
    py scripts/broker_alpha.py [--min-adtv-pct 3] [--min-value 500e6] [--top-n 15]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel, summarise, wilson_lower  # noqa: E402

HORIZONS = (3, 5)
W = {3: 0.6, 5: 0.4}          # 3-5 day hit-rate weighting, per the agreed spec
MIN_EVENTS = 30               # below this a broker is provisional, not ranked


def build_events(p: Panel, min_value: float, min_adtv_pct: float,
                 run_of: int = 3, run_need: int = 2) -> list[dict]:
    """Accumulation events: (broker, symbol, day) where the broker bought meaningfully.

    Two filters do the work. The rupiah floor removes noise; the **%ADTV floor is the
    important one** — without it the leaderboard just ranks brokers by size, because a
    big house's ordinary day dwarfs a small one's conviction trade.
    """
    events = []
    for (sym, broker), series in p.flows.items():
        adtv = p.adtv.get(sym, {})
        by_i = dict(series)
        for i, net in series:
            if net < min_value:
                continue
            a = adtv.get(i)
            if not a or net < (min_adtv_pct / 100.0) * a:
                continue
            # Require persistence: a single day's print is often just a client order.
            buys = sum(1 for j in range(i - run_of + 1, i + 1) if by_i.get(j, 0) > 0)
            if buys < run_need:
                continue
            ev = {"broker": broker, "symbol": sym, "i": i,
                  "net": net, "adtv_pct": 100.0 * net / a}
            ok = True
            for k in HORIZONS:
                r = p.excess_return(sym, i, k)
                if r is None:
                    ok = False
                    break
                ev[f"x{k}"] = r
            if ok:
                events.append(ev)
    return events


def score_brokers(events: list[dict], min_events: int = MIN_EVENTS,
                  by: str = "hit") -> list[dict]:
    """Rank brokers by weighted Wilson-bounded hit rate (`hit`) or mean excess (`mean`).

    `mean` exists because the observed payoff is right-skewed: across ~81k events the
    hit rate is 48% while mean excess is POSITIVE and the median negative. When most
    outcomes are slightly negative and a minority are strongly positive, a hit-rate
    objective actively discards the part that pays. Both are reported, and both are
    judged on the same walk-forward test — the alternative is a hypothesis about the
    distribution's shape, not licence to keep trying objectives until one works.
    """
    by_broker = defaultdict(list)
    for e in events:
        by_broker[e["broker"]].append(e)

    rows = []
    for broker, evs in by_broker.items():
        row = {"broker": broker, "n": len(evs)}
        alpha = 0.0
        for k in HORIZONS:
            s = summarise([e[f"x{k}"] for e in evs])
            row[f"hit{k}"] = s["hit_rate"]
            row[f"wil{k}"] = s["wilson"]
            row[f"mean{k}"] = s["mean_excess"]
            alpha += W[k] * ((s["wilson"] - 0.5) if by == "hit" else s["mean_excess"])
        row["alpha"] = alpha
        row["provisional"] = len(evs) < min_events
        row["median_adtv_pct"] = sorted(e["adtv_pct"] for e in evs)[len(evs) // 2]
        rows.append(row)

    rows.sort(key=lambda r: (r["provisional"], -r["alpha"]))
    return rows


def walk_forward(events: list[dict], n_dates: int, train: int = 250,
                 test: int = 60, step: int = 60, quintile: float = 0.2,
                 by: str = "hit") -> dict:
    """Rank on [t-train, t), measure on [t, t+test). Roll forward.

    This is the only number that answers "would following this broker have worked?"
    """
    folds, top_x, bot_x, all_x = [], defaultdict(list), defaultdict(list), defaultdict(list)
    start = train
    while start + test <= n_dates:
        tr = [e for e in events if start - train <= e["i"] < start]
        te = [e for e in events if start <= e["i"] < start + test]
        if len(tr) < 200 or not te:
            start += step
            continue

        ranked = [r for r in score_brokers(tr, min_events=20, by=by)
                  if not r["provisional"]]
        if len(ranked) < 10:
            start += step
            continue
        cut = max(1, int(len(ranked) * quintile))
        top = {r["broker"] for r in ranked[:cut]}
        bot = {r["broker"] for r in ranked[-cut:]}

        fold = {"train_start": start - train, "test_start": start,
                "train_events": len(tr), "test_events": len(te),
                "n_brokers": len(ranked), "top": sorted(top)}
        for k in HORIZONS:
            t_x = [e[f"x{k}"] for e in te if e["broker"] in top]
            b_x = [e[f"x{k}"] for e in te if e["broker"] in bot]
            a_x = [e[f"x{k}"] for e in te]
            top_x[k] += t_x
            bot_x[k] += b_x
            all_x[k] += a_x
            fold[f"top_hit{k}"] = summarise(t_x)["hit_rate"] if t_x else None
            fold[f"all_hit{k}"] = summarise(a_x)["hit_rate"] if a_x else None
        folds.append(fold)
        start += step

    out = {"folds": folds, "n_folds": len(folds)}
    for k in HORIZONS:
        out[f"top_{k}"] = summarise(top_x[k])
        out[f"bottom_{k}"] = summarise(bot_x[k])
        out[f"all_{k}"] = summarise(all_x[k])
    return out


def null_control(events: list[dict], seed: int = 7) -> dict:
    """Shuffle broker labels across the same events and re-rank.

    Destroys any broker-specific information while preserving the event distribution.
    A harness that still reports an edge here is broken.
    """
    rng = random.Random(seed)
    labels = [e["broker"] for e in events]
    rng.shuffle(labels)
    shuffled = [{**e, "broker": lab} for e, lab in zip(events, labels)]
    ranked = [r for r in score_brokers(shuffled) if not r["provisional"]]
    if not ranked:
        return {}
    cut = max(1, int(len(ranked) * 0.2))
    top = {r["broker"] for r in ranked[:cut]}
    return {k: summarise([e[f"x{k}"] for e in shuffled if e["broker"] in top])
            for k in HORIZONS}


def pct(x):
    return "  n/a" if x is None else f"{x:6.1%}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-adtv-pct", type=float, default=3.0)
    ap.add_argument("--min-value", type=float, default=500e6)
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--score-by", choices=("hit", "mean"), default="hit")
    ap.add_argument("--train", type=int, default=250)
    ap.add_argument("--test", type=int, default=60)
    ap.add_argument("--step", type=int, default=60)
    args = ap.parse_args()

    print("loading panel…")
    p = Panel().load()
    print(f"  {p.describe()}\n")

    events = build_events(p, args.min_value, args.min_adtv_pct)
    if not events:
        print("No events built — loosen the filters.")
        return 1
    brokers = {e["broker"] for e in events}
    print(f"events: {len(events):,} accumulation events across {len(brokers)} brokers "
          f"(>= Rp{args.min_value/1e6:,.0f}m and >= {args.min_adtv_pct}% of 20d ADTV)")

    base = {k: summarise([e[f"x{k}"] for e in events]) for k in HORIZONS}
    print("\nBASELINE — every accumulation event, market-adjusted")
    for k in HORIZONS:
        s = base[k]
        print(f"  {k}d: hit {s['hit_rate']:.1%}  mean excess {s['mean_excess']:+.2%}"
              f"  median {s['median_excess']:+.2%}  (n={s['n']:,})")
    print("  If these already beat 50%, the edge is in ACCUMULATION generally, not in")
    print("  broker identity — the leaderboard has to beat this bar, not 50%.")

    # ---- null control
    print("\nNULL CONTROL — broker labels shuffled (must land near the baseline)")
    null = null_control(events)
    null_ok = True
    for k in HORIZONS:
        s = null.get(k)
        if not s:
            continue
        delta = s["hit_rate"] - base[k]["hit_rate"]
        flag = "" if abs(delta) < 0.03 else "   <== SUSPICIOUS"
        if abs(delta) >= 0.03:
            null_ok = False
        print(f"  {k}d: top-quintile-of-noise hit {s['hit_rate']:.1%} "
              f"vs baseline {base[k]['hit_rate']:.1%} ({delta:+.1%}){flag}")

    # ---- in-sample leaderboard
    ranked = score_brokers(events, by=args.score_by)
    ranked_real = [r for r in ranked if not r["provisional"]]
    print(f"\nIN-SAMPLE LEADERBOARD — {len(ranked_real)} brokers with >= {MIN_EVENTS} "
          f"events ({len(ranked) - len(ranked_real)} provisional, excluded)")
    print(f"  {'rank':<5}{'brk':<5}{'n':>6}{'hit3':>8}{'hit5':>8}"
          f"{'wil3':>8}{'wil5':>8}{'mean3':>9}{'alpha':>8}{'%ADTV':>8}")
    for rank, r in enumerate(ranked_real[:args.top_n], 1):
        print(f"  {rank:<5}{r['broker']:<5}{r['n']:>6}{r['hit3']:>8.1%}{r['hit5']:>8.1%}"
              f"{r['wil3']:>8.1%}{r['wil5']:>8.1%}{r['mean3']:>+9.2%}"
              f"{r['alpha']:>+8.3f}{r['median_adtv_pct']:>8.1f}")
    print("  … worst 5:")
    for r in ranked_real[-5:]:
        print(f"  {'':<5}{r['broker']:<5}{r['n']:>6}{r['hit3']:>8.1%}{r['hit5']:>8.1%}"
              f"{r['wil3']:>8.1%}{r['wil5']:>8.1%}{r['mean3']:>+9.2%}"
              f"{r['alpha']:>+8.3f}{r['median_adtv_pct']:>8.1f}")

    # ---- walk-forward: the number that matters
    print(f"\nWALK-FORWARD — rank on {args.train} sessions by '{args.score_by}', "
          f"measure on the next {args.test}, roll")
    wf = walk_forward(events, len(p.dates), train=args.train, test=args.test,
                      step=args.step, by=args.score_by)
    if not wf["n_folds"]:
        print("  not enough history for a fold")
    else:
        print(f"  {wf['n_folds']} folds")
        print(f"  {'':<10}{'top quintile':>26}{'all events':>22}{'bottom quintile':>26}")
        for k in HORIZONS:
            t, a, b = wf[f"top_{k}"], wf[f"all_{k}"], wf[f"bottom_{k}"]
            print(f"  {k}d hit  {t['hit_rate']:>12.1%} (n={t['n']:,})"
                  f"{a['hit_rate']:>14.1%} (n={a['n']:,})"
                  f"{b['hit_rate']:>15.1%} (n={b['n']:,})")
            print(f"  {k}d mean {t['mean_excess']:>+12.2%}"
                  f"{a['mean_excess']:>+21.2%}{b['mean_excess']:>+22.2%}")
            edge = t["hit_rate"] - a["hit_rate"]
            print(f"     -> top-quintile edge over all events: {edge:+.1%}"
                  f"{'  (no edge)' if edge <= 0.005 else ''}")

    out = {
        "params": vars(args), "events": len(events),
        "baseline": {str(k): base[k] for k in HORIZONS},
        "null_control": {str(k): null.get(k) for k in HORIZONS},
        "null_ok": null_ok,
        "leaderboard": ranked,
        "walk_forward": {k: v for k, v in wf.items() if k != "folds"},
        "folds": wf.get("folds", []),
    }
    path = PANEL / "broker_alpha.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float),
                    encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
