"""K1 — OCCUPANCY. Count the joint-lift state before modelling anything.

Framework: reference/lift.md section 6. This runs BEFORE any forward return is computed,
deliberately: the whole point is to learn whether the cell the screener would fire on is
visited often enough, by enough distinct tickers, on enough distinct market episodes, to
ever support a claim. Publishing the ladder first is what stops a 25-observation cell from
later being written up as a finding.

KILL if k>=3 runs come from fewer than 30 distinct tickers, OR fewer than 8 distinct
30-day calendar blocks, OR the base rate of L is below 2%.

Zero API calls. Everything here reads the panel already on disk.

Usage:
    py lift_probe.py                 # WEAK lift on the 25-month flows panel
    py lift_probe.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import broker_profile as bp
import lift_lib as L
from alpha_lib import PANEL, Panel, panel_fingerprint

COHORT_FRAC = 0.25          # top/bottom quartile by lateness, as in chaser_test.py


def cohorts(scores: dict[str, dict], frac: float = COHORT_FRAC
            ) -> tuple[set[str], set[str]]:
    """(accumulators, chasers) from the point-in-time lateness scores.

    Field is xr_trail (lateness), NOT xr_same. Same-day correlation cannot separate
    "chose to buy strength" from "crossed the spread" — it is a price-impact-and-size
    artifact, and is plausibly why "chasers are foreign" was found. Lateness ends its
    window strictly at i-1 and its disjoint-halves Spearman is +0.793.

    HIGH lateness = arrives after the move = CHASER.
    LOW  lateness = arrives before it      = ACCUMULATOR.
    """
    if not scores:
        return set(), set()
    ranked = sorted(scores.items(), key=lambda kv: kv[1]["score"])
    n = max(1, int(len(ranked) * frac))
    return {b for b, _ in ranked[:n]}, {b for b, _ in ranked[-n:]}


def build_states(p: Panel, grid: dict[int, dict]) -> dict[str, dict[int, tuple]]:
    """{sym: {i: (L_A, L_C)}} using the WEAK lift: cohort net value > 0.

    WEAK because the flows panel carries net_value only. The STRONG form (paid up above
    VWAP) needs buy_avg, which exists only on the 7-month gross panel — that is stage 2.
    Running WEAK first is deliberate: it has 25 months and 161 names, so if the state is
    not even OCCUPIED here it will never be occupied on a third of the data.
    """
    by_sym: dict[str, dict[int, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for (sym, broker), series in p.flows.items():
        for i, net in series:
            by_sym[sym][i][broker] = net

    out: dict[str, dict[int, tuple]] = {}
    for sym, days in by_sym.items():
        st: dict[int, tuple] = {}
        for i, brokers in days.items():
            sc = bp.scores_for(grid, i)
            if not sc:
                continue                      # before the first point-in-time estimate
            acc, cha = cohorts(sc)
            if not acc or not cha:
                continue
            na = sum(v for b, v in brokers.items() if b in acc)
            nc = sum(v for b, v in brokers.items() if b in cha)
            st[i] = (1 if na > 0 else 0, 1 if nc > 0 else 0)
        if st:
            out[sym] = st
    return out


def twin_states(p: Panel, universe: set[str],
                covered: dict[str, set[int]]) -> dict[str, dict[int, bool]]:
    """The K2 price-only twin: close above the day's midpoint. Zero broker data.

    Restricted to exactly the (sym, i) cells the broker state covers, so the two ladders
    are computed on the same sessions and are directly comparable. Comparing a twin built
    on all sessions against a broker state built on a subset would flatter the twin's
    counts for a reason that has nothing to do with either signal.
    """
    out: dict[str, dict[int, bool]] = {}
    for sym in universe:
        d: dict[int, bool] = {}
        for i in covered.get(sym, ()):
            c = L.price_twin(p.high.get(sym, {}).get(i),
                             p.low.get(sym, {}).get(i),
                             p.raw_close.get(sym, {}).get(i))
            if c is not None:
                d[i] = c
        if d:
            out[sym] = d
    return out


def occupancy(flags: dict[str, dict[int, bool]], label: str, max_k: int = 6) -> dict:
    """The ladder: runs reaching each age, with distinct tickers and distinct 30-day
    calendar blocks behind each. Distinct blocks is the number that matters — 200 events
    from 5 market episodes is 5 observations wearing a costume."""
    all_runs: list[tuple[str, int, int]] = []
    n_days = n_on = 0
    for sym, d in flags.items():
        n_days += len(d)
        n_on += sum(1 for v in d.values() if v)
        for onset, ln in L.runs_strict(d):
            all_runs.append((sym, onset, ln))

    rows = []
    for k in range(1, max_k + 1):
        reach = [(s, o, ln) for (s, o, ln) in all_runs if ln >= k]
        # the session on which age k is attained
        idxs = [o + k - 1 for (_, o, ln) in reach]
        rows.append({
            "k": k,
            "continuation": None,   # filled below
            "runs_reaching": len(reach),
            "distinct_tickers": len({s for (s, _, _) in reach}),
            "distinct_blocks": L.blocks_with_treatment(idxs),
            "inferential": L.is_inferential(L.blocks_with_treatment(idxs)),
        })

    for a_, b_ in zip(rows, rows[1:]):
        a_["continuation"] = (b_["runs_reaching"] / a_["runs_reaching"]
                              if a_["runs_reaching"] else None)

    lens = [ln for (_, _, ln) in all_runs]
    return {
        "label": label,
        "ticker_days": n_days,
        "state_on_days": n_on,
        "base_rate": (n_on / n_days) if n_days else None,
        "onsets": len(all_runs),
        "median_run": statistics.median(lens) if lens else None,
        "max_run": max(lens) if lens else None,
        "ladder": rows,
    }


def recoverability(joint: dict[str, dict[int, bool]],
                   twin: dict[str, dict[int, bool]]) -> dict:
    """How much of the broker joint-lift state is recoverable from the price twin alone.

    This is the K2 redundancy check in its cheapest form. If the broker state is largely a
    relabelling of "close above the midpoint", the broker data is decorative and this is
    the sixth momentum thesis. Reported as agreement, plus the confusion table so the
    reader can see WHICH way it fails.
    """
    tp = fp = fn = tn = 0
    for sym, d in joint.items():
        t = twin.get(sym, {})
        for i, j in d.items():
            c = t.get(i)
            if c is None:
                continue
            if j and c:
                tp += 1
            elif j and not c:
                fn += 1
            elif (not j) and c:
                fp += 1
            else:
                tn += 1
    n = tp + fp + fn + tn
    return {
        "n": n,
        "agreement": ((tp + tn) / n) if n else None,
        "P_twin_given_joint": (tp / (tp + fn)) if (tp + fn) else None,
        "P_joint_given_twin": (tp / (tp + fp)) if (tp + fp) else None,
        "table": {"both": tp, "joint_only": fn, "twin_only": fp, "neither": tn},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("--panel", type=Path, default=PANEL)
    a = ap.parse_args()

    p = Panel(a.panel).load()
    print(f"panel: {len(p.close)} symbols x {len(p.dates)} sessions "
          f"({p.dates[0]} -> {p.dates[-1]})")

    o = bp.build_observations(p)
    print(f"observations: {len(o):,} broker-days")
    grid = bp.schedule(p, o, field="xr_trail")
    print(f"point-in-time cohort grid: {len(grid)} estimates, "
          f"first usable session {min(grid) if grid else 'n/a'}")
    if not grid:
        print("no cohort estimates — panel shorter than the 250-session window",
              file=sys.stderr)
        return 2

    states = build_states(p, grid)
    covered = {s: set(d) for s, d in states.items()}
    print(f"state built on {len(states)} symbols\n")

    joint = {s: {i: (v == (1, 1)) for i, v in d.items()} for s, d in states.items()}
    a_only = {s: {i: (v == (1, 0)) for i, v in d.items()} for s, d in states.items()}
    c_only = {s: {i: (v == (0, 1)) for i, v in d.items()} for s, d in states.items()}
    twin = twin_states(p, set(states), covered)

    results = [
        occupancy(joint, "JOINT (both cohorts net buying)"),
        occupancy(a_only, "A_ONLY (accumulators only)"),
        occupancy(c_only, "C_ONLY (chasers only)"),
        occupancy(twin, "TWIN (price only: close > midpoint)"),
    ]

    for r in results:
        print(f"--- {r['label']}")
        print(f"    ticker-days {r['ticker_days']:,} | state on {r['state_on_days']:,} "
              f"| base rate {r['base_rate']:.1%} | onsets {r['onsets']:,} "
              f"| median run {r['median_run']} | max {r['max_run']}")
        print("      k   runs  tickers  blocks  P(reach k+1 | reached k)  inferential")
        for row in r["ladder"]:
            c = row["continuation"]
            cs = f"{c:6.1%}" if c is not None else "     -"
            print(f"      {row['k']}  {row['runs_reaching']:6,}  {row['distinct_tickers']:7}"
                  f"  {row['distinct_blocks']:6}  {cs:>23}  "
                  f"{'yes' if row['inferential'] else 'NO'}")
        print()

    rec = recoverability(joint, twin)
    print("--- K2 redundancy: is JOINT just the price twin?")
    print(f"    n {rec['n']:,} | agreement {rec['agreement']:.1%} "
          f"| P(twin|joint) {rec['P_twin_given_joint']:.1%} "
          f"| P(joint|twin) {rec['P_joint_given_twin']:.1%}")
    print(f"    {rec['table']}")

    j = results[0]
    k3 = next(r for r in j["ladder"] if r["k"] == 3)
    verdict = []
    if j["base_rate"] is not None and j["base_rate"] < 0.02:
        verdict.append(f"KILL: base rate {j['base_rate']:.2%} < 2%")
    if k3["distinct_tickers"] < 30:
        verdict.append(f"KILL: k>=3 from {k3['distinct_tickers']} tickers < 30")
    if k3["distinct_blocks"] < 8:
        verdict.append(f"KILL: k>=3 from {k3['distinct_blocks']} calendar blocks < 8")
    print("\n=== K1 VERDICT ===")
    print("\n".join(verdict) if verdict else
          "PASS - occupancy clears the pre-registered floor; proceed to K2.")

    if a.json:
        a.json.write_text(json.dumps(
            {"panel_fingerprint": panel_fingerprint(), "cohort_field": "xr_trail",
             "cohort_frac": COHORT_FRAC, "occupancy": results,
             "recoverability": rec, "verdict": verdict or ["PASS"]},
            indent=1), encoding="utf-8")
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
