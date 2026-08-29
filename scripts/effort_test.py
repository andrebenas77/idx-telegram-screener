#!/usr/bin/env python3
"""H1 — effort vs result: is narrowness a coordinate?

Thesis #9, from `idx_quant_skill.md`. Framework and PASS BAR are pre-registered in
`reference/effort.md`, written before this file existed. Read it before reading any number
below; the bar is not adjustable after the fact.

    H1. Among days of elevated relative volume, NARROW-range days behave differently from
        WIDE-range ones, and the effect is monotone in narrowness.

Kill criterion, stated before the numbers arrive (reference/effort.md sec 6-7). Ships only
if ALL hold: nr_self Q5-Q1 at k=5 >= +1.0pp with the 10/90 date-block band clear of zero;
gradient monotone in >=4 of 5 steps; sign stable across all three narrowness definitions;
feature-shift null within +/-0.3pp; >=3 of 4 calendar folds positive; check 0 passes;
blocks_with_treatment >= 15. A flat or INVERTED gradient is a REFUTATION, not a licence to
use the loosest threshold -- one-sidedness (+0.70 -> +0.16 -> -0.39) and joint-lift age
(+0.016 -> +0.003 -> -0.037) both died exactly that way.

This study does NOT partition the exhaustion effect. That effect lives on 197 accumulation
stock-days whose narrow cell is 7-17 events; it cannot be split. See effort.md sec 4.

Reads the panel and writes its own JSON. Touches nothing the momentum board imports.

    py scripts/effort_test.py
    py scripts/effort_test.py --quick        # 200 bootstrap draws, 50 null draws
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel, panel_fingerprint  # noqa: E402
import lift_lib as LL  # noqa: E402
import trade_backtest as TB  # noqa: E402
from trade_lib import tick_size  # noqa: E402

# ------------------------------------------------------------------ declared constants

RVOL_FLOOR = 1.5          # the board's own RVOL_MIN; an existing constant, not a fitted one
LOOKBACK = 20             # sessions of history for rvol5 and the range median
N_QUINTILES = 5
HORIZONS = (3, 5, 10)
PRIMARY_K = 5
PRIMARY_DEF = "nr_self"

BAR_Q5MQ1_PP = 1.0        # pass bar 1
BAR_MONOTONE_STEPS = 4    # pass bar 2 (of N_QUINTILES - 1)
BAR_NULL_PP = 0.3         # pass bar 4
BAR_FOLDS = 3             # pass bar 5 (of 4)
BAR_CHECK0 = 0.005        # pass bar 6
MIN_FOLD_N = 50           # a fold thinner than this reports n/a, not stability

RVOL_BANDS = ((1.5, 2.0), (2.0, 3.0), (3.0, float("inf")))
NARROW_DEFS = ("nr_self", "nr_tick", "nr_lit")


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ features

def build_rows(p: Panel) -> list[dict]:
    """Every symbol-day with 20 sessions of history and elevated volume.

    Structure on RAW bars (alpha_lib.py:8-18). Returns come later, via
    Panel.excess_return, which uses the adjusted series and an entry lag of 1.
    """
    rows = []
    for sym in sorted(p.raw_close):
        cl, hi, lo = p.raw_close[sym], p.high.get(sym, {}), p.low.get(sym, {})
        vol = p.volume.get(sym, {})
        idxs = sorted(cl)
        for i in idxs:
            win = list(range(i - LOOKBACK + 1, i + 1))
            if win[0] < 0 or not all(j in cl and j in hi and j in lo and j in vol for j in win):
                continue
            v = [vol[j] for j in win]
            v20 = sum(v) / LOOKBACK
            v5 = sum(v[-5:]) / 5
            if v20 <= 0:
                continue
            rvol5 = v5 / v20
            if rvol5 < RVOL_FLOOR:
                continue
            c, h, l = cl[i], hi[i], lo[i]
            if not c or h < l:
                continue
            rng = h - l
            med = statistics.median([hi[j] - lo[j] for j in win])
            if med <= 0:
                continue
            rows.append({
                "symbol": sym, "i": i, "rvol5": rvol5,
                "nr_self": rng / med,
                "nr_tick": rng / tick_size(c),
                "nr_lit": rng / c,
            })
    return rows


def attach_returns(p: Panel, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        keep = dict(r)
        ok = False
        for k in HORIZONS:
            x = p.excess_return(r["symbol"], r["i"], k, entry_lag=1)
            keep[f"x{k}"] = x
            if k == PRIMARY_K and x is not None:
                ok = True
        if ok:
            out.append(keep)
    return out


# ------------------------------------------------------------------ quintiles

def breakpoints(values: list[float], n: int = N_QUINTILES) -> list[float]:
    """Pooled quintile cuts on the narrowness SCORE (higher = narrower)."""
    s = sorted(values)
    return [s[int(len(s) * j / n)] for j in range(1, n)]


def bucket(score: float, cuts: list[float]) -> int:
    """1 = widest, N_QUINTILES = narrowest."""
    q = 1
    for c in cuts:
        if score >= c:
            q += 1
    return min(q, N_QUINTILES)


def assign(rows: list[dict], defn: str) -> tuple[list[dict], list[float]]:
    """score = -nr, so a HIGHER score is a NARROWER day and Q5 is the narrowest quintile."""
    cuts = breakpoints([-r[defn] for r in rows])
    for r in rows:
        r[f"q_{defn}"] = bucket(-r[defn], cuts)
    return rows, cuts


# ------------------------------------------------------------------ statistics

def mean_or_none(xs: list[float]):
    return statistics.fmean(xs) if xs else None


def q5_minus_q1(obs: list[tuple]) -> float | None:
    """stat() for the date-block bootstrap. obs = [(quintile, excess), ...]."""
    hi = [x for q, x in obs if q == N_QUINTILES]
    lo = [x for q, x in obs if q == 1]
    if not hi or not lo:
        return None
    return (statistics.fmean(hi) - statistics.fmean(lo)) * 100.0


def gradient(rows: list[dict], defn: str, k: int) -> list[dict]:
    out = []
    for q in range(1, N_QUINTILES + 1):
        xs = [r[f"x{k}"] for r in rows if r[f"q_{defn}"] == q and r.get(f"x{k}") is not None]
        out.append({"q": q, "n": len(xs),
                    "mean_pp": (mean_or_none(xs) or 0) * 100 if xs else None,
                    "median_pp": (statistics.median(xs) * 100) if xs else None,
                    "hit": (sum(1 for x in xs if x > 0) / len(xs)) if xs else None})
    return out


def monotone_steps(grad: list[dict]) -> int:
    """How many of the N-1 quintile steps move in the hypothesised (upward) direction."""
    steps = 0
    for a, b in zip(grad, grad[1:]):
        if a["mean_pp"] is None or b["mean_pp"] is None:
            continue
        if b["mean_pp"] >= a["mean_pp"]:
            steps += 1
    return steps


# ------------------------------------------------------------------ null

def feature_shift_null(p: Panel, rows_all: list[dict], defn: str, k: int,
                       draws: int, seed: int = 7) -> dict:
    """Circularly shift each symbol's FEATURE series, leaving its returns in place.

    Preserves every stock's return distribution and every feature's autocorrelation while
    destroying feature-to-return alignment. Quintile cuts are recomputed on each draw, so a
    draw is a full replay of the ranking step over a matched population.

    The population is held fixed (it is already rvol-filtered on entry), so this null asks
    exactly the right question: GIVEN these days, does the narrowness ORDERING carry
    anything? A near-zero answer here is what licenses reading the real number at all -- if
    the null reproduced the real result, the harness would be leaking.

    This is `accum_test.shift_dates` reasoning applied to features rather than flows. The
    label-shuffle null is deliberately NOT run: it agreed with the real result by
    construction in the accumulation work and could never fail.
    """
    by_sym: dict[str, list[dict]] = {}
    for r in rows_all:
        if r.get(f"x{k}") is None:
            continue
        by_sym.setdefault(r["symbol"], []).append(r)
    for v in by_sym.values():
        v.sort(key=lambda r: r["i"])

    rng = random.Random(seed)
    vals = []
    for _ in range(draws):
        shifted = []
        for sym, ser in by_sym.items():
            n = len(ser)
            if n < 2:
                continue
            off = rng.randrange(n)
            for j, host in enumerate(ser):
                donor = ser[(j + off) % n]
                shifted.append({"nr": donor[defn], "x": host[f"x{k}"]})
        if len(shifted) < 100:
            continue
        cuts = breakpoints([-s["nr"] for s in shifted])
        obs = [(bucket(-s["nr"], cuts), s["x"]) for s in shifted]
        v = q5_minus_q1(obs)
        if v is not None:
            vals.append(v)
    if not vals:
        return {"draws": 0}
    return {"draws": len(vals), "mean_pp": statistics.fmean(vals),
            "sd_pp": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "p05_pp": sorted(vals)[int(0.05 * len(vals))],
            "p95_pp": sorted(vals)[min(len(vals) - 1, int(0.95 * len(vals)))]}


# ------------------------------------------------------------------ folds

def folds(p: Panel, rows: list[dict], defn: str, k: int, n: int = 4) -> list[dict]:
    """4 equal CALENDAR stretches, never equal event counts -- equal event counts would
    hide a regime in which the state stopped occurring at all."""
    idxs = [r["i"] for r in rows]
    lo, hi = min(idxs), max(idxs) + 1
    size = (hi - lo) / n
    out = []
    for j in range(n):
        a, b = lo + int(j * size), lo + int((j + 1) * size)
        sub = [r for r in rows if a <= r["i"] < b]
        obs = [(r[f"q_{defn}"], r[f"x{k}"]) for r in sub if r.get(f"x{k}") is not None]
        val = q5_minus_q1(obs) if len(obs) >= MIN_FOLD_N else None
        out.append({"fold": j + 1, "from": p.dates[a], "to": p.dates[min(b, len(p.dates) - 1)],
                    "n": len(obs), "q5mq1_pp": val})
    return out


# ------------------------------------------------------------------ check 0

def momentum_candidates(p: Panel) -> list[dict]:
    """The known-good rule, deduped to stock-days -- the correct unit for check 0."""
    import momentum_diagnose as MD
    mom, _ = MD.build_all(p)
    seen, out = set(), []
    for sym, i, *_ in mom:
        if (sym, i) in seen:
            continue
        seen.add((sym, i))
        out.append({"symbol": sym, "i": i})
    return out


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="fewer bootstrap and null draws")
    ap.add_argument("--out", default=str(PANEL / "effort_test.json"))
    args = ap.parse_args()

    n_boot = 200 if args.quick else 2000
    n_null = 50 if args.quick else 200

    log("=" * 78)
    log("H1 -- EFFORT VS RESULT.  Pre-registered in reference/effort.md sec 6-7.")
    log("=" * 78)

    p = Panel()
    p.load()
    log(f"panel: {len(p.raw_close)} symbols x {len(p.dates)} sessions "
        f"({p.dates[0]} .. {p.dates[-1]})")

    # ---- check 0 FIRST. A verdict from a hostile window is not a verdict.
    cands = momentum_candidates(p)
    c0 = TB.check_zero(p, cands, k=PRIMARY_K, bar=BAR_CHECK0)
    pooled = c0.get("pooled", {})
    log(f"\ncheck 0: momentum lift in-window {100 * (pooled.get('lift') or 0):+.2f}pp "
        f"on n={pooled.get('n')} (bar +{100 * BAR_CHECK0:.1f}pp) -> "
        f"{'PASS' if c0.get('ok') else 'FAIL'}")
    if not c0.get("ok"):
        log("  [!!] the known-good rule does NOT work in this window. Nothing read from")
        log("       it can separate a bad rule from a bad period. Stopping.")
        return 4

    # ---- population
    rows_all = build_rows(p)
    rows = attach_returns(p, rows_all)
    log(f"\npopulation: rvol5 >= {RVOL_FLOOR} and a computable {PRIMARY_K}d excess")
    log(f"  symbol-days: {len(rows)}   distinct dates: {len({r['i'] for r in rows})}")

    # ---- baselines, computed BEFORE any conditional statistic
    uncond = [x for s in p.raw_close for i in p.raw_close[s]
              for x in [p.excess_return(s, i, PRIMARY_K, entry_lag=1)] if x is not None]
    within = [r[f"x{PRIMARY_K}"] for r in rows if r.get(f"x{PRIMARY_K}") is not None]
    log(f"  unconditional baseline (all symbol-days, n={len(uncond)}): "
        f"{100 * (mean_or_none(uncond) or 0):+.3f}pp")
    log(f"  within-population mean  (n={len(within)}): "
        f"{100 * (mean_or_none(within) or 0):+.3f}pp")

    results = {}
    for defn in NARROW_DEFS:
        assign(rows, defn)
        results[defn] = {}
        for k in HORIZONS:
            grad = gradient(rows, defn, k)
            results[defn][f"k{k}"] = {
                "gradient": grad,
                "q5mq1_pp": (grad[-1]["mean_pp"] - grad[0]["mean_pp"])
                if grad[-1]["mean_pp"] is not None and grad[0]["mean_pp"] is not None else None,
                "monotone_steps": monotone_steps(grad),
            }

    # ---- primary cell: bands + blocks
    per_date: dict[int, list] = {}
    for r in rows:
        x = r.get(f"x{PRIMARY_K}")
        if x is not None:
            per_date.setdefault(r["i"], []).append((r[f"q_{PRIMARY_DEF}"], x))
    boot = LL.date_block_bootstrap(per_date, q5_minus_q1, n_boot=n_boot)
    tail_idx = [r["i"] for r in rows
                if r[f"q_{PRIMARY_DEF}"] in (1, N_QUINTILES) and r.get(f"x{PRIMARY_K}") is not None]
    nblocks = LL.blocks_with_treatment(tail_idx)

    null = feature_shift_null(p, rows, PRIMARY_DEF, PRIMARY_K, n_null)
    fold_rows = folds(p, rows, PRIMARY_DEF, PRIMARY_K)

    # ---- surface read-out
    # Each band gets its OWN band and block count. Without them the eye lands on the most
    # extreme cell and promotes it -- which is how a post-hoc observation becomes a rule.
    surface = []
    for lo_b, hi_b in RVOL_BANDS:
        sub = [r for r in rows if lo_b <= r["rvol5"] < hi_b]
        cell = {"band": f"[{lo_b},{hi_b})", "n": len(sub),
                "quintiles": gradient(sub, PRIMARY_DEF, PRIMARY_K) if sub else []}
        if cell["quintiles"]:
            a, b = cell["quintiles"][0]["mean_pp"], cell["quintiles"][-1]["mean_pp"]
            cell["q5mq1_pp"] = (b - a) if (a is not None and b is not None) else None
            pd_b: dict[int, list] = {}
            for r in sub:
                x = r.get(f"x{PRIMARY_K}")
                if x is not None:
                    pd_b.setdefault(r["i"], []).append((r[f"q_{PRIMARY_DEF}"], x))
            cell["bootstrap"] = LL.date_block_bootstrap(pd_b, q5_minus_q1, n_boot=n_boot)
            tails = [r["i"] for r in sub if r[f"q_{PRIMARY_DEF}"] in (1, N_QUINTILES)
                     and r.get(f"x{PRIMARY_K}") is not None]
            cell["blocks"] = LL.blocks_with_treatment(tails)
            cell["inferential"] = LL.is_inferential(cell["blocks"])
        surface.append(cell)

    # ------------------------------------------------------------------ report
    log("\n" + "-" * 78)
    log(f"PRIMARY  {PRIMARY_DEF}, k={PRIMARY_K}   (Q1 = widest, Q{N_QUINTILES} = narrowest)")
    log("-" * 78)
    log(f"{'quintile':>10} {'n':>7} {'mean pp':>10} {'median pp':>11} {'hit':>7}")
    for g in results[PRIMARY_DEF][f"k{PRIMARY_K}"]["gradient"]:
        log(f"{'Q' + str(g['q']):>10} {g['n']:>7} "
            f"{(g['mean_pp'] if g['mean_pp'] is not None else float('nan')):>+10.3f} "
            f"{(g['median_pp'] if g['median_pp'] is not None else float('nan')):>+11.3f} "
            f"{(g['hit'] or 0) * 100:>6.1f}%")

    q5mq1 = results[PRIMARY_DEF][f"k{PRIMARY_K}"]["q5mq1_pp"]
    steps = results[PRIMARY_DEF][f"k{PRIMARY_K}"]["monotone_steps"]
    log(f"\nQ5-Q1 = {q5mq1:+.3f}pp   band [{boot.get('lo')}, {boot.get('hi')}] "
        f"(10/90, {boot.get('block')}-day blocks, {boot.get('n_dates')} dates)")
    log(f"monotone steps: {steps}/{N_QUINTILES - 1}")
    log(f"blocks_with_treatment: {nblocks}  -> "
        f"{'INFERENTIAL' if LL.is_inferential(nblocks) else 'DESCRIPTIVE'}")
    log(f"feature-shift null: mean {null.get('mean_pp', float('nan')):+.3f}pp "
        f"(sd {null.get('sd_pp', float('nan')):.3f}, {null.get('draws')} draws)")

    log("\nsign stability across narrowness definitions:")
    for d in NARROW_DEFS:
        v = results[d][f"k{PRIMARY_K}"]["q5mq1_pp"]
        log(f"  {d:<9} Q5-Q1 {v:+.3f}pp   monotone "
            f"{results[d][f'k{PRIMARY_K}']['monotone_steps']}/{N_QUINTILES - 1}")

    log("\ncalendar folds:")
    for f in fold_rows:
        v = f"{f['q5mq1_pp']:+.3f}pp" if f["q5mq1_pp"] is not None else "n/a (thin)"
        log(f"  {f['fold']}  {f['from']} .. {f['to']}  n={f['n']:>5}  {v}")

    log("\nRVOL x narrowness surface (READ-OUT, not a result -- every cell is post-hoc):")
    for cell in surface:
        vals = " ".join(
            f"Q{g['q']}:{g['mean_pp']:+.2f}({g['n']})" if g["mean_pp"] is not None else f"Q{g['q']}:n/a"
            for g in cell["quintiles"])
        log(f"  rvol {cell['band']:<12} n={cell['n']:>5}  {vals}")
        b = cell.get("bootstrap") or {}
        if b.get("lo") is not None:
            log(f"       Q5-Q1 {cell.get('q5mq1_pp'):+.2f}pp  band [{b['lo']:+.2f}, {b['hi']:+.2f}]"
                f"  blocks {cell.get('blocks')} -> "
                f"{'inferential' if cell.get('inferential') else 'DESCRIPTIVE'}")

    # ------------------------------------------------------------------ verdict
    signs = [results[d][f"k{PRIMARY_K}"]["q5mq1_pp"] for d in NARROW_DEFS]
    sign_stable = all(s is not None for s in signs) and (
        all(s > 0 for s in signs) or all(s < 0 for s in signs))
    folds_pos = sum(1 for f in fold_rows if (f["q5mq1_pp"] or 0) > 0)
    band_clear = (boot.get("lo") is not None and boot.get("hi") is not None
                  and (boot["lo"] > 0 or boot["hi"] < 0))
    null_ok = abs(null.get("mean_pp", 99)) <= BAR_NULL_PP

    checks = [
        (f"1 Q5-Q1 >= +{BAR_Q5MQ1_PP}pp", q5mq1 is not None and q5mq1 >= BAR_Q5MQ1_PP),
        ("1b band clear of zero", band_clear),
        (f"2 monotone >= {BAR_MONOTONE_STEPS}/{N_QUINTILES - 1}", steps >= BAR_MONOTONE_STEPS),
        ("3 sign stable across definitions", sign_stable),
        (f"4 null within +/-{BAR_NULL_PP}pp", null_ok),
        (f"5 folds positive >= {BAR_FOLDS}/4", folds_pos >= BAR_FOLDS),
        ("6 check 0", bool(c0.get("ok"))),
        ("7 blocks >= 15", LL.is_inferential(nblocks)),
    ]
    log("\n" + "=" * 78)
    for label, ok in checks:
        log(f"  [{'PASS' if ok else 'FAIL'}]  {label}")
    verdict = "PASS" if all(ok for _, ok in checks) else "FAIL"
    log(f"\nVERDICT: {verdict}")
    if verdict == "FAIL":
        log("H1 is refuted under its own pre-registered bar. Record it in reference/effort.md")
        log("beside the other eight and do not re-test it at a looser threshold.")
    log("=" * 78)

    payload = {
        "study": "H1 effort-vs-result",
        "preregistered": "reference/effort.md",
        "panel_fingerprint": panel_fingerprint(),
        "params": {"rvol_floor": RVOL_FLOOR, "lookback": LOOKBACK,
                   "primary_def": PRIMARY_DEF, "primary_k": PRIMARY_K,
                   "n_boot": n_boot, "n_null": n_null},
        "check_zero": c0,
        "n_rows": len(rows),
        "baseline_uncond_pp": 100 * (mean_or_none(uncond) or 0),
        "baseline_within_pp": 100 * (mean_or_none(within) or 0),
        "results": results,
        "bootstrap": boot,
        "blocks_with_treatment": nblocks,
        "inferential": LL.is_inferential(nblocks),
        "null_feature_shift": null,
        "folds": fold_rows,
        "surface": surface,
        "checks": {label: ok for label, ok in checks},
        "verdict": verdict,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2, default=float), encoding="utf-8")
    log(f"\nwrote {args.out}")
    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
