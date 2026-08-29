#!/usr/bin/env python3
"""Thesis #12 — volume dispersion as a proxy for clip size, tested where check 0 PASSES.

Pre-registered in `reference/dispersion.md` before the tf=60 backfill and before any
forward return.

    Var(V_b) / E(V_b) = E[S^2]/E[S]     <- size-biased mean trade size; the arrival rate
                                           cancels, so clip size is recoverable WITHOUT
                                           observing trade counts.

Validated against gross-panel truth at within-symbol rho +0.512 (hourly, k=10). That
correlation is why the pass bar is ATTENUATED to +0.5pp: blockdom.md set +1.0pp on the true
variable, and a proxy correlated r recovers roughly r times a linear effect.

The structural move this study exists to exploit: CHECK 0 GATES PREDICTION, NOT MEASUREMENT.
The overlap window (where check 0 fails) gave the proxy its meaning; this runs in the good
window (2024-09..2026-08, check 0 +0.97pp) to ask whether it pays.

Kill conditions (dispersion.md sec 7), declared before the numbers: flat or inverted
gradient; null within 0.3pp; **failing the RVOL-tercile orthogonality check while passing the
headline is a REFUTATION, not a partial pass** -- a dispersion effect confined to one RVOL
tercile is RVOL wearing a dispersion label.

Reads the panel and h60 store. Touches nothing the momentum board imports.

    py scripts/dispersion_test.py
    py scripts/dispersion_test.py --quick
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel, panel_fingerprint  # noqa: E402
import lift_lib as LL  # noqa: E402
import trade_backtest as TB  # noqa: E402
from vpin_lib import spearman  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INTRADAY = ROOT / "data" / "intraday"

OPEN_HHMM, CLOSE_HHMM = "09:00", "16:00"   # 08:00 is the auction hour, 16:00 the MOC
MIN_BUCKETS = 5
SEASONAL_LOOKBACK = 60
DISP_K = 10                                # from dispersion.md sec 2's plateau, not fitted here
NORM_LOOKBACK = 60

N_QUINTILES = 5
HORIZONS = (3, 5, 10)
PRIMARY_K = 5

BAR_Q5MQ1_PP = 0.5        # ATTENUATED: 1.0pp true-variable bar x rho 0.512
BAR_MONOTONE = 4
BAR_NULL_PP = 0.3
BAR_FOLDS = 3
BAR_CHECK0 = 0.005
BAR_TERCILES = 2          # of 3 -- the orthogonality check
BAR_VALIDATE_RHO = 0.40   # trailing-seasonal correlation vs gross truth
MIN_FOLD_N = 50


def log(m: str = "") -> None:
    print(m, flush=True)


# ------------------------------------------------------------------ features

def hourly_sessions(sym: str) -> dict[str, dict[str, float]]:
    f = INTRADAY / f"h60-{sym}.csv.gz"
    if not f.exists():
        return {}
    out: dict[str, dict[str, float]] = defaultdict(dict)
    with gzip.open(f, "rt", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if OPEN_HHMM <= r["hhmm"] < CLOSE_HHMM:
                try:
                    out[r["date"]][r["hhmm"]] = float(r["volume"])
                except (TypeError, ValueError):
                    pass
    return out


def dispersion_series(sym: str) -> dict[str, float]:
    """{date: d(i)} with TRAILING seasonals only -- no look-ahead anywhere.

    The seasonal median is mandatory: IDX intraday volume is U-shaped, so a raw dispersion
    index would mostly measure the time of day.
    """
    ses = hourly_sessions(sym)
    dates = sorted(ses)
    hist: dict[str, list[float]] = defaultdict(list)
    out: dict[str, float] = {}
    for d in dates:
        xs = []
        for h, v in ses[d].items():
            prior = hist[h][-SEASONAL_LOOKBACK:]
            if len(prior) >= 20:
                m = st.median(prior)
                if m > 0:
                    xs.append(v / m)
        if len(xs) >= MIN_BUCKETS:
            mu = st.fmean(xs)
            if mu > 0:
                out[d] = st.pvariance(xs) / mu
        for h, v in ses[d].items():
            hist[h].append(v)
    return out


def rolling(vals: list[float], k: int) -> list[float]:
    return [st.fmean(vals[max(0, j - k + 1):j + 1]) for j in range(len(vals))]


def rel_to_own_history(dates: list[str], vals: list[float]) -> dict[str, float]:
    """v(i) / median(v over the prior NORM_LOOKBACK, STRICTLY before i). Within-symbol,
    look-ahead-free. Pooled raw dispersion is deliberately not used: the cross-sectional
    signal is only +0.172 and would rank names by liquidity."""
    out = {}
    for j, d in enumerate(dates):
        prior = vals[max(0, j - NORM_LOOKBACK):j]
        if len(prior) >= 20:
            m = st.median(prior)
            if m > 0:
                out[d] = vals[j] / m
    return out


def build_rows(p: Panel) -> list[dict]:
    di = {d: i for i, d in enumerate(p.dates)}
    rows = []
    for sym in sorted(p.raw_close):
        disp = dispersion_series(sym)
        if len(disp) < NORM_LOOKBACK + DISP_K:
            continue
        ds = sorted(disp)
        d10 = rolling([disp[d] for d in ds], DISP_K)
        drel = rel_to_own_history(ds, d10)

        # Amihud secondary: |r| / value, blocks cross at one price, churn grinds the tape
        cl, vol = p.raw_close.get(sym, {}), p.volume.get(sym, {})
        adj = p.close.get(sym, {})
        ami_by_date, ami_vals, ami_dates = {}, [], []
        idxs = sorted(cl)
        for n, i in enumerate(idxs):
            if n == 0:
                continue
            j = idxs[n - 1]
            v = cl.get(i, 0) * vol.get(i, 0)
            a0, a1 = adj.get(j), adj.get(i)
            if v > 0 and a0 and a1:
                ami_by_date[p.dates[i]] = abs(a1 / a0 - 1) / v * 1e12
        ad = sorted(ami_by_date)
        if ad:
            ami_vals = rolling([ami_by_date[d] for d in ad], DISP_K)
            ami_dates = ad
        arel = rel_to_own_history(ami_dates, ami_vals) if ami_dates else {}

        for d, v in drel.items():
            i = di.get(d)
            if i is None:
                continue
            # rvol5, computed inline to match overlay_test's formula without its 120-session cost
            win = list(range(i - 19, i + 1))
            if win[0] < 0 or not all(j in vol for j in win):
                continue
            vv = [vol[j] for j in win]
            v20 = sum(vv) / 20
            if v20 <= 0:
                continue
            row = {"symbol": sym, "i": i, "date": d, "disp_rel": v,
                   "rvol5": (sum(vv[-5:]) / 5) / v20}
            if d in arel:
                row["ami_rel"] = arel[d]
            ok = False
            for k in HORIZONS:
                x = p.excess_return(sym, i, k, entry_lag=1)
                row[f"x{k}"] = x
                if k == PRIMARY_K and x is not None:
                    ok = True
            if ok:
                rows.append(row)
    return rows


# ------------------------------------------------------------------ stats

def cuts_of(vals: list[float], n: int = N_QUINTILES) -> list[float]:
    s = sorted(vals)
    return [s[int(len(s) * j / n)] for j in range(1, n)]


def bucket(v: float, cuts: list[float]) -> int:
    q = 1
    for c in cuts:
        if v >= c:
            q += 1
    return min(q, N_QUINTILES)


def q5mq1(obs: list[tuple]) -> float | None:
    hi = [x for q, x in obs if q == N_QUINTILES]
    lo = [x for q, x in obs if q == 1]
    if not hi or not lo:
        return None
    return (st.fmean(hi) - st.fmean(lo)) * 100.0


def gradient(rows: list[dict], qk: str, k: int) -> list[dict]:
    out = []
    for q in range(1, N_QUINTILES + 1):
        xs = [r[f"x{k}"] for r in rows if r.get(qk) == q and r.get(f"x{k}") is not None]
        out.append({"q": q, "n": len(xs),
                    "mean_pp": (st.fmean(xs) * 100) if xs else None,
                    "hit": (sum(1 for x in xs if x > 0) / len(xs)) if xs else None})
    return out


def monotone(grad: list[dict]) -> int:
    return sum(1 for a, b in zip(grad, grad[1:])
               if a["mean_pp"] is not None and b["mean_pp"] is not None
               and b["mean_pp"] >= a["mean_pp"])


def shift_null(rows: list[dict], field: str, sign: int, k: int, draws: int, seed: int = 7) -> dict:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get(f"x{k}") is not None and r.get(field) is not None:
            by[r["symbol"]].append(r)
    for v in by.values():
        v.sort(key=lambda r: r["i"])
    rng = random.Random(seed)
    vals = []
    for _ in range(draws):
        sh = []
        for ser in by.values():
            n = len(ser)
            if n < 2:
                continue
            off = rng.randrange(n)
            for j, host in enumerate(ser):
                sh.append((sign * ser[(j + off) % n][field], host[f"x{k}"]))
        if len(sh) < 100:
            continue
        c = cuts_of([s for s, _ in sh])
        v = q5mq1([(bucket(s, c), x) for s, x in sh])
        if v is not None:
            vals.append(v)
    if not vals:
        return {"draws": 0}
    return {"draws": len(vals), "mean_pp": st.fmean(vals),
            "sd_pp": st.pstdev(vals) if len(vals) > 1 else 0.0}


def folds_of(p: Panel, rows: list[dict], qk: str, k: int, n: int = 4) -> list[dict]:
    idxs = [r["i"] for r in rows]
    lo, hi = min(idxs), max(idxs) + 1
    size = (hi - lo) / n
    out = []
    for j in range(n):
        a, b = lo + int(j * size), lo + int((j + 1) * size)
        obs = [(r[qk], r[f"x{k}"]) for r in rows
               if a <= r["i"] < b and r.get(f"x{k}") is not None]
        out.append({"fold": j + 1, "from": p.dates[a], "to": p.dates[min(b, len(p.dates) - 1)],
                    "n": len(obs), "q5mq1_pp": q5mq1(obs) if len(obs) >= MIN_FOLD_N else None})
    return out


def validate_against_truth() -> dict:
    """Check 8 -- does the instrument still measure clip size with TRAILING seasonals only?"""
    agg = defaultdict(lambda: [0.0, 0.0])
    for f in sorted(glob.glob(str(PANEL / "gross-*.csv.gz"))):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    bv, bf = float(r["buy_value"]), float(r["buy_freq"])
                except (TypeError, ValueError):
                    continue
                if bv > 0 and bf > 0:
                    a = agg[(r["symbol"], r["date"])]
                    a[0] += bv
                    a[1] += bf
    truth = {k: v[0] / v[1] for k, v in agg.items()}
    A, B, nsym = [], [], 0
    for sym in sorted({s for s, _ in truth}):
        disp = dispersion_series(sym)
        ds = sorted(set(disp) & {d for s, d in truth if s == sym})
        if len(ds) < 40:
            continue
        d10 = rolling([disp[d] for d in ds], DISP_K)[DISP_K - 1:]
        tv = rolling([math.log(truth[(sym, d)]) for d in ds], DISP_K)[DISP_K - 1:]
        mp, mt = st.fmean(d10), st.fmean(tv)
        A += [x - mp for x in d10]
        B += [x - mt for x in tv]
        nsym += 1
    return {"rho": spearman(A, B) if A else None, "n": len(A), "symbols": nsym}


def momentum_candidates(p: Panel) -> list[dict]:
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
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=str(PANEL / "dispersion_test.json"))
    args = ap.parse_args()
    n_boot = 200 if args.quick else 2000
    n_null = 50 if args.quick else 200

    log("=" * 78)
    log("THESIS #12 -- VOLUME DISPERSION.  Pre-registered: reference/dispersion.md")
    log("=" * 78)

    p = Panel()
    p.load()

    cands = momentum_candidates(p)
    c0 = TB.check_zero(p, cands, k=PRIMARY_K, bar=BAR_CHECK0)
    pooled = c0.get("pooled", {})
    log(f"\ncheck 0: {100 * (pooled.get('lift') or 0):+.2f}pp on n={pooled.get('n')} -> "
        f"{'PASS' if c0.get('ok') else 'FAIL'}")
    if not c0.get("ok"):
        log("  [!!] known-good rule fails here. Stopping.")
        return 4

    log("\ncheck 8: re-validating the instrument with TRAILING seasonals only...")
    val = validate_against_truth()
    log(f"  within-symbol rho(disp10, log ticket) = {val['rho']:+.3f} "
        f"(n={val['n']}, {val['symbols']} symbols, bar +{BAR_VALIDATE_RHO})")

    rows = build_rows(p)
    log(f"\npopulation: {len(rows)} symbol-days, {len({r['symbol'] for r in rows})} symbols, "
        f"{len({r['i'] for r in rows})} sessions")
    log(f"  window {p.dates[min(r['i'] for r in rows)]} .. {p.dates[max(r['i'] for r in rows)]}")

    uncond = [r[f"x{PRIMARY_K}"] for r in rows if r.get(f"x{PRIMARY_K}") is not None]
    log(f"  unconditional baseline: {100 * st.fmean(uncond):+.3f}pp (n={len(uncond)})")

    results = {}
    for label, field, sign in (("disp_rel", "disp_rel", +1), ("amihud", "ami_rel", -1)):
        sub = [r for r in rows if r.get(field) is not None]
        if not sub:
            continue
        qk = f"q_{label}"
        c = cuts_of([sign * r[field] for r in sub])
        for r in sub:
            r[qk] = bucket(sign * r[field], c)

        grad = gradient(sub, qk, PRIMARY_K)
        pd_: dict[int, list] = {}
        for r in sub:
            x = r.get(f"x{PRIMARY_K}")
            if x is not None:
                pd_.setdefault(r["i"], []).append((r[qk], x))
        boot = LL.date_block_bootstrap(pd_, q5mq1, n_boot=n_boot)
        tails = [r["i"] for r in sub if r[qk] in (1, N_QUINTILES) and r.get(f"x{PRIMARY_K}") is not None]
        nb = LL.blocks_with_treatment(tails)
        null = shift_null(sub, field, sign, PRIMARY_K, n_null)
        fr = folds_of(p, sub, qk, PRIMARY_K)
        head = (grad[-1]["mean_pp"] - grad[0]["mean_pp"]) \
            if grad[-1]["mean_pp"] is not None and grad[0]["mean_pp"] is not None else None

        # ORTHOGONALITY: re-sort INSIDE each RVOL tercile
        rv = sorted(r["rvol5"] for r in sub)
        t1, t2 = rv[len(rv) // 3], rv[2 * len(rv) // 3]
        terc = []
        for n_, (a, b) in enumerate((( -1e9, t1), (t1, t2), (t2, 1e9)), 1):
            cell = [r for r in sub if a <= r["rvol5"] < b]
            g = gradient(cell, qk, PRIMARY_K) if cell else []
            v = (g[-1]["mean_pp"] - g[0]["mean_pp"]) \
                if g and g[-1]["mean_pp"] is not None and g[0]["mean_pp"] is not None else None
            terc.append({"tercile": n_, "n": len(cell), "q5mq1_pp": v})

        results[label] = {"gradient": grad, "q5mq1_pp": head, "monotone": monotone(grad),
                          "bootstrap": boot, "blocks": nb, "null": null, "folds": fr,
                          "terciles": terc}

        log("\n" + "-" * 78)
        log(f"{label}   (Q5 = most block-like)")
        log("-" * 78)
        log(f"{'quintile':>10} {'n':>8} {'mean pp':>10} {'hit':>8}")
        for g in grad:
            log(f"{'Q' + str(g['q']):>10} {g['n']:>8} "
                f"{(g['mean_pp'] if g['mean_pp'] is not None else float('nan')):>+10.3f} "
                f"{(g['hit'] or 0) * 100:>7.1f}%")
        lo_s = f"{boot['lo']:+.2f}" if boot.get("lo") is not None else "n/a"
        hi_s = f"{boot['hi']:+.2f}" if boot.get("hi") is not None else "n/a"
        log(f"\nQ5-Q1 {head:+.3f}pp  band [{lo_s}, {hi_s}]  monotone {monotone(grad)}/4  blocks {nb}")
        log(f"null {null.get('mean_pp', float('nan')):+.3f}pp (sd {null.get('sd_pp', float('nan')):.3f})")
        log("RVOL terciles (orthogonality): " + "  ".join(
            f"T{t['tercile']}:{t['q5mq1_pp']:+.2f}({t['n']})" if t["q5mq1_pp"] is not None
            else f"T{t['tercile']}:n/a" for t in terc))
        for f in fr:
            v = f"{f['q5mq1_pp']:+.3f}pp" if f["q5mq1_pp"] is not None else "n/a"
            log(f"  fold {f['fold']} {f['from']} .. {f['to']} n={f['n']:>6} {v}")

    # ------------------------------------------------------------------ verdict
    r = results.get("disp_rel", {})
    boot = r.get("bootstrap", {})
    terc_pos = sum(1 for t in r.get("terciles", []) if (t["q5mq1_pp"] or 0) > 0)
    folds_pos = sum(1 for f in r.get("folds", []) if (f["q5mq1_pp"] or 0) > 0)
    checks = [
        (f"1 Q5-Q1 >= +{BAR_Q5MQ1_PP}pp (attenuated)",
         r.get("q5mq1_pp") is not None and r["q5mq1_pp"] >= BAR_Q5MQ1_PP),
        ("1b band clear of zero",
         boot.get("lo") is not None and (boot["lo"] > 0 or boot["hi"] < 0)),
        (f"2 monotone >= {BAR_MONOTONE}/4", r.get("monotone", 0) >= BAR_MONOTONE),
        (f"3 ORTHOGONALITY: >= {BAR_TERCILES}/3 RVOL terciles positive", terc_pos >= BAR_TERCILES),
        (f"4 null within +/-{BAR_NULL_PP}pp", abs(r.get("null", {}).get("mean_pp", 99)) <= BAR_NULL_PP),
        (f"5 folds >= {BAR_FOLDS}/4", folds_pos >= BAR_FOLDS),
        ("6 check 0", bool(c0.get("ok"))),
        ("7 blocks >= 15", LL.is_inferential(r.get("blocks", 0))),
        (f"8 trailing-seasonal rho >= +{BAR_VALIDATE_RHO}",
         (val.get("rho") or 0) >= BAR_VALIDATE_RHO),
    ]
    log("\n" + "=" * 78)
    for lab, ok in checks:
        log(f"  [{'PASS' if ok else 'FAIL'}]  {lab}")
    verdict = "PASS" if all(ok for _, ok in checks) else "FAIL"
    log(f"\nVERDICT: {verdict}")
    if verdict == "FAIL" and checks[0][1] and not checks[3][1]:
        log("NOTE: headline passed but ORTHOGONALITY failed. Per dispersion.md sec 7 that is a")
        log("      REFUTATION, not a partial pass -- the effect is RVOL, not dispersion.")
    log("=" * 78)

    Path(args.out).write_text(json.dumps({
        "study": "thesis #12 volume dispersion",
        "preregistered": "reference/dispersion.md",
        "panel_fingerprint": panel_fingerprint(),
        "check_zero": c0, "instrument_validation": val,
        "n_rows": len(rows), "baseline_pp": 100 * st.fmean(uncond),
        "results": results, "checks": {k: v for k, v in checks}, "verdict": verdict,
    }, indent=2, default=float), encoding="utf-8")
    log(f"\nwrote {args.out}")
    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
