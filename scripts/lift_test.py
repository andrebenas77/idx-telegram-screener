"""K2/K3 — does the joint-lift AGE state carry first-passage information over a PRICE-ONLY
twin? Framework: reference/lift.md sections 5-7.

The functional is deliberately NOT a conditional mean forward return. That is what the
prior chaser test measured, on a near-martingale, where it is close to zero by
construction. First passage against ASYMMETRIC barriers (+2.0 ATR before -1.5 ATR) is a
different functional of the return law and is non-trivial even at zero drift whenever
returns are serially dependent. It also has a hard, non-estimated benchmark: the driftless
value b/(a+b) = 0.4286.

Everything is reported as an INCREMENT over an age-matched, same-calendar-day price-only
twin built with ZERO broker data. If the broker state adds nothing over "close above the
day's midpoint", this is the sixth momentum thesis and it should die here.

Zero API calls.

Usage:
    py lift_test.py --json ../data/panel/lift_test.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import lift_lib as L
import lift_probe as probe
import broker_profile as bp
from alpha_lib import PANEL, Panel, panel_fingerprint


def adjusted_bars(p: Panel, sym: str) -> dict[int, tuple[float, float, float, float]]:
    """{i: (open, high, low, close)} on the ADJUSTED basis.

    Corporate actions are applied as a per-day factor f = close_adj / raw_close. A barrier
    walk on RAW bars books an ex-date as a barrier touch: 17 of the 20 worst "stop losses"
    in the earlier trade backtest were ex-dates, and PACK 2026-01-12 was raw -91.7%
    against adjusted +9.4%. Structure is judged on raw bars everywhere else in this repo;
    a BARRIER is a return question, so it takes the adjusted series.
    """
    out = {}
    hi, lo, rc, op, ca = (p.high.get(sym, {}), p.low.get(sym, {}),
                          p.raw_close.get(sym, {}), p.open.get(sym, {}),
                          p.close.get(sym, {}))
    for i, r in rc.items():
        h, l, o, c = hi.get(i), lo.get(i), op.get(i), ca.get(i)
        if not r or r <= 0 or h is None or l is None or c is None:
            continue
        f = c / r
        out[i] = ((o * f) if o else None, h * f, l * f, c)
    return out


def outcomes_for(p: Panel, flags: dict[str, dict[int, bool]],
                 bars: dict[str, dict], label: str) -> list[dict]:
    """One row per ONSET-ANCHORED observation: a run of `flags` that reached age k.

    Entry is the OPEN of the session after age k is attained — the signal uses only
    information available at that session's close, and the fill is the next printable
    price. Referencing the same session's close would be look-ahead; the entry-fill work
    established one-shot at the next open as the honest convention.
    """
    rows = []
    for sym, d in flags.items():
        b = bars.get(sym)
        if not b:
            continue
        idxs = sorted(b)
        pos = {i: n for n, i in enumerate(idxs)}
        for onset, ln in L.runs_strict(d):
            for k in range(1, ln + 1):
                j = onset + k - 1              # session on which age k is attained
                e = j + 1                      # entry session
                if j not in pos or e not in b:
                    continue
                n = pos[j]
                if n < L.ATR_N:
                    continue
                win = idxs[n - L.ATR_N:n + 1]
                atrp = L.atr_pct([b[x][1] for x in win], [b[x][2] for x in win],
                                 [b[x][3] for x in win])
                entry = b[e][0]
                if entry is None or atrp is None:
                    continue
                fwd = [x for x in idxs if x >= e][:L.MAX_HOLD]
                res = L.first_passage(entry, atrp,
                                      [b[x][1] for x in fwd], [b[x][2] for x in fwd])
                rows.append({"state": label, "sym": sym, "i": e, "k": k,
                             "bin": L.age_bin(k), "outcome": res, "atrp": atrp})
    return rows


def pi_by_bin(rows: list[dict]) -> dict:
    out = {}
    for ab in ("1", "2", "3+"):
        sub = [r for r in rows if r["bin"] == ab]
        st = L.pi_hat([r["outcome"] for r in sub])
        idxs = [r["i"] for r in sub if r["outcome"] != "unresolved"]
        st["n_blocks"] = L.blocks_with_treatment(idxs)
        st["inferential"] = L.is_inferential(st["n_blocks"])
        st["distinct_tickers"] = len({r["sym"] for r in sub})
        out[ab] = st
    return out


def paired_delta(a_rows: list[dict], b_rows: list[dict], ab: str,
                 b_bin: str | None = "match") -> dict:
    """Delta_pi for one age bin, PAIRED ON CALENDAR DAY.

    For each session that carries resolved observations of BOTH states, take the
    difference of that day's win rates, then bootstrap those daily differences over
    30-day blocks. Pairing on the day removes the market component, which is the dominant
    source of variance on IDX and roughly a third of the standard error.
    """
    def by_date(rows, want):
        d = defaultdict(list)
        for r in rows:
            if (want is None or r["bin"] == want) and r["outcome"] in ("win", "loss"):
                d[r["i"]].append(1.0 if r["outcome"] == "win" else 0.0)
        return d

    # b_bin=None pools ALL ages on the comparison side. Required for the UNCONDITIONAL
    # benchmark: age is undefined for a randomly chosen day, so age-matching against it is
    # not just wrong, it silently compares thousands of treated rows against the handful
    # of rows that happen to sit at the start of a symbol's history.
    A = by_date(a_rows, ab)
    B = by_date(b_rows, ab if b_bin == "match" else None)
    shared = sorted(set(A) & set(B))
    per_date = {i: [statistics.fmean(A[i]) - statistics.fmean(B[i])] for i in shared}
    bs = L.date_block_bootstrap(per_date,
                                lambda xs: statistics.fmean(xs) if xs else None)
    bs["n_shared_dates"] = len(shared)
    bs["n_blocks"] = L.blocks_with_treatment(shared)
    bs["inferential"] = L.is_inferential(bs["n_blocks"])
    return bs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("--panel", type=Path, default=PANEL)
    a = ap.parse_args()

    p = Panel(a.panel).load()
    o = bp.build_observations(p)
    grid = bp.schedule(p, o, field="xr_trail")
    states = probe.build_states(p, grid)
    covered = {s: set(d) for s, d in states.items()}
    print(f"panel {len(p.close)} x {len(p.dates)} | state on {len(states)} symbols")

    flagsets = {
        "JOINT":  {s: {i: (v == (1, 1)) for i, v in d.items()} for s, d in states.items()},
        "A_ONLY": {s: {i: (v == (1, 0)) for i, v in d.items()} for s, d in states.items()},
        "C_ONLY": {s: {i: (v == (0, 1)) for i, v in d.items()} for s, d in states.items()},
        # K3 decomposition: either cohort net buying, ignoring who. Signed net flow is
        # itself long-memory, so this will show persistence on its own. If JOINT does not
        # beat it, the "both cohorts" framing is decorative.
        "ANY":    {s: {i: (v != (0, 0)) for i, v in d.items()} for s, d in states.items()},
        # MARGINAL states: cohort buying REGARDLESS of what the other cohort does. This is
        # the natural reading of "the accumulator state" -- A_ONLY is conditional, since it
        # excludes every day the chasers were also buying, which is the JOINT population.
        # A_ANY = A_ONLY + JOINT, so if the two marginals disagree the difference is
        # attributable to the joint days rather than to accumulators as such.
        "A_ANY":  {s: {i: (v[0] == 1) for i, v in d.items()} for s, d in states.items()},
        "C_ANY":  {s: {i: (v[1] == 1) for i, v in d.items()} for s, d in states.items()},
        "TWIN":   probe.twin_states(p, set(states), covered),
        # UNCONDITIONAL. Every covered session is an "event". Without this, "beats the
        # twin" is uninterpretable: if the twin SELECTS BAD DAYS then everything beats it
        # and a positive Delta_pi means nothing. The twin is a redundancy control, not a
        # performance benchmark, and conflating the two is how a null becomes a finding.
        "ALL":    {s_: {i: True for i in d} for s_, d in covered.items()},
    }
    # THE ARBITER between two readings of the twin comparison. A referee argued the twin
    # is the correct control (JOINT beats it by +2 to +6pp) and that unconditional is the
    # wrong null. But JOINT and the twin are ANTI-matched, not matched: P(twin|JOINT) is
    # 29.5% against a 37.5% base rate, i.e. joint-lift days close WEAK. So "JOINT beats
    # the twin" may be nothing but the short-horizon reversal effect -- strong-close days
    # mean-revert -- which is obtainable with ZERO broker data by simply inverting the
    # twin. If NOTTWIN reproduces JOINT's edge, the flow data is adding nothing.
    tw = flagsets["TWIN"]
    flagsets["NOTTWIN"] = {s_: {i: (not v) for i, v in d.items()} for s_, d in tw.items()}

    bars = {s: adjusted_bars(p, s) for s in states}
    allrows = {k: outcomes_for(p, f, bars, k) for k, f in flagsets.items()}

    print(f"\ndriftless null pi = {L.DRIFTLESS_NULL:.4f}   "
          f"(barriers +{L.UP_ATR} / -{L.DOWN_ATR} ATR, max hold {L.MAX_HOLD}d)")
    print("pi >= 0.500 is what clears a +0.25 ATR cost hurdle\n")

    summary = {}
    for name, rows in allrows.items():
        summary[name] = pi_by_bin(rows)
        print(f"--- {name}")
        print("    age      n   pi      E[R]    unres  tickers  blocks  inferential")
        for ab in ("1", "2", "3+"):
            s = summary[name][ab]
            if s["pi"] is None:
                print(f"    {ab:>3}  no resolved paths")
                continue
            print(f"    {ab:>3}  {s['n_resolved']:5,}  {s['pi']:.3f}  "
                  f"{s['E_R']:+.3f}  {s['unresolved']:6,}  {s['distinct_tickers']:7}"
                  f"  {s['n_blocks']:6}  {'yes' if s['inferential'] else 'NO'}")
        print()

    print("=== BASELINE: Delta_pi vs UNCONDITIONAL, paired on calendar day ===")
    print("    unconditional pi = a randomly chosen entry, same day, same universe")
    base = {}
    for name in ("JOINT", "A_ONLY", "A_ANY", "C_ONLY", "C_ANY", "ANY", "TWIN", "NOTTWIN"):
        base[name] = {}
        row = []
        for ab in ("1", "2", "3+"):
            d = paired_delta(allrows[name], allrows["ALL"], ab, b_bin=None)
            base[name][ab] = d
            pt = d.get("point")
            row.append("   n/a" if pt is None else
                       (f"{pt:+.3f} [{d['lo']:+.3f},{d['hi']:+.3f}]"
                        if d.get("lo") is not None else f"{pt:+.3f}"))
        print(f"    {name:7} k=1 {row[0]:26} k=2 {row[1]:26} k=3+ {row[2]}")
    print()

    print("=== ATR MIX: are the arms comparable in volatility? ===")
    print("    (if JOINT days are higher-ATR, a fixed bp cost is a SMALLER ATR hurdle)")
    for name in ("JOINT", "ANY", "TWIN", "NOTTWIN", "ALL"):
        vals = [r["atrp"] for r in allrows[name] if r.get("atrp")]
        if vals:
            print(f"    {name:8} mean ATR% {statistics.fmean(vals):6.2%}   "
                  f"median {statistics.median(vals):6.2%}   n {len(vals):,}")
    print()

    print("=== K2: Delta_pi = pi(state) - pi(TWIN), paired on calendar day ===")
    deltas = {}
    for name in ("JOINT", "A_ONLY", "A_ANY", "C_ONLY", "C_ANY", "ANY", "TWIN", "NOTTWIN"):
        deltas[name] = {}
        print(f"--- {name} vs TWIN")
        for ab in ("1", "2", "3+"):
            d = paired_delta(allrows[name], allrows["TWIN"], ab)
            deltas[name][ab] = d
            pt = d.get("point")
            if pt is None:
                print(f"    {ab:>3}  no shared dates")
                continue
            band = (f"[{d['lo']:+.3f}, {d['hi']:+.3f}]"
                    if d.get("lo") is not None else "[band unavailable]")
            flag = "" if d["inferential"] else "   <- DESCRIPTIVE ONLY"
            print(f"    {ab:>3}  Delta_pi {pt:+.3f}  80% {band}  "
                  f"dates {d['n_shared_dates']:4}  blocks {d['n_blocks']}{flag}")
        print()

    if a.json:
        a.json.write_text(json.dumps(
            {"panel_fingerprint": panel_fingerprint(),
             "barriers": {"up": L.UP_ATR, "down": L.DOWN_ATR,
                          "driftless_null": L.DRIFTLESS_NULL,
                          "max_hold": L.MAX_HOLD},
             "pi": summary, "delta_vs_twin": deltas,
             "delta_vs_unconditional": base}, indent=1), encoding="utf-8")
        print(f"wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
