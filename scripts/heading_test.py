#!/usr/bin/env python3
"""RSI heading, and whether the broker leg earns its place.

Framework and PASS BARS are pre-registered in `reference/rsi-heading.md`, written before
this file computed a single forward return. Read it first; the bars are not adjustable.

    H-B (the owner's idea). Among stock-days that pass the board's PRICE leg, the ones whose
         RSI came up to 55+ from below it over the last 20 sessions do better than the ones
         that were already at 55+ twenty sessions ago.
    H-A  The broker leg adds to the price leg: price-leg days WITH a qualifying accumulator
         beat price-leg days without one.
    H-C  (read-out only) an identity-free broker measure: how concentrated the day's net
         buying was in its five largest buyers.

Two primary tests, both at k=5 with entry at the next close, exactly as check 0 measures the
board. Everything else printed here is a read-out and is labelled so.

`--occupancy` counts the cells and stops WITHOUT computing a return. It was run and its
output pasted into rsi-heading.md sec 4 before the full run, so the cells were known to be
populated before anyone could see which of them paid.

Reads the panel and writes its own JSON. Touches nothing the momentum board imports.

    py scripts/heading_test.py --occupancy
    py scripts/heading_test.py
    py scripts/heading_test.py --quick          # 300 bootstrap draws, 50 null draws
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
import build_momentum_board as B  # noqa: E402  the live gate constants, never a copy
import lift_lib as LL  # noqa: E402
import trade_backtest as TB  # noqa: E402
from momentum_setup import is_momentum  # noqa: E402
from overlay_test import features  # noqa: E402

# ------------------------------------------------------------------ declared constants

BACK = 20                 # sessions back for "then". Owner's call: a one-month swing.
BACK_SENS = (10, 40)      # sign must agree at half and double the lookback
HORIZONS = (5, 10, 20)
PRIMARY_K = 5             # the board's horizon; check 0 is measured here
FLOOR_IDR = 5e9           # median daily value over the PRIOR 20 sessions, signal day excluded
FLOOR_WIN = 20
LEVEL_BANDS = ((55.0, 65.0), (65.0, 75.0), (75.0, 100.01))   # today's RSI, for the matched contrast
N_Q = 5

BAR_PP = 0.5              # pass bar 1, both primary tests
BAR_NULL_PP = 0.3         # pass bar 4
BAR_FOLDS = 3             # pass bar 5 (of 4)
BAR_CHECK0 = 0.005        # pass bar 6
MIN_ARM_FOLD_N = 20       # a fold with a thinner arm reports n/a, not stability

APPROACH_LO, APPROACH_RISE = 45.0, 10.0   # read-out cell: RSI 45-55 and up 10+ points in BACK


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ features

def lean(p: Panel, sym: str, i: int) -> dict | None:
    """rvol5, rsi and dd60 exactly as overlay_test.features() computes them, and nothing else.

    features() also builds a 120-session volatility percentile on every call, which this
    study never reads and which turns 60,000 stock-days into minutes on the box. `parity()`
    holds the two together.
    """
    cl = p.close.get(sym) or {}
    if not all(j in cl for j in range(i - 59, i + 1)):
        return None
    px = [cl[j] for j in range(i - 59, i + 1)]
    if px[-1] <= 0:
        return None
    rets = [px[k] / px[k - 1] - 1 for k in range(1, len(px)) if px[k - 1] > 0]
    ag = sum(max(0.0, r) for r in rets[-14:]) / 14
    al = sum(max(0.0, -r) for r in rets[-14:]) / 14
    rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    vo = p.volume.get(sym) or {}
    v5 = [vo[j] for j in range(i - 4, i + 1) if j in vo]
    v20 = [vo[j] for j in range(i - 19, i + 1) if j in vo]
    rvol5 = (sum(v5) / len(v5)) / (sum(v20) / len(v20)) if v5 and v20 and sum(v20) else None
    return {"rsi": rsi, "rvol5": rvol5, "dd60": px[-1] / max(px) - 1}


def parity(p: Panel, n: int = 300, seed: int = 7) -> int:
    """lean() against features() on a random sample. Any difference is a refusal to run."""
    rng = random.Random(seed)
    syms = sorted(p.close)
    bad = checked = 0
    while checked < n:
        s = rng.choice(syms)
        i = rng.choice(sorted(p.close[s]))
        a, b = lean(p, s, i), features(p, s, i)
        if (a is None) != (b is None):
            bad += 1
        elif a is not None:
            for key in ("rsi", "rvol5", "dd60"):
                x, y = a[key], b[key]
                if (x is None) != (y is None) or (x is not None and abs(x - y) > 1e-12):
                    bad += 1
        checked += 1
    return bad


def broker_days(p: Panel) -> tuple[dict, dict]:
    """Per (symbol, day): how many brokers pass the board's own leg-1 rule, and the day's
    positive broker nets (for the identity-free concentration read-out)."""
    qual: dict[tuple[str, int], int] = {}
    buys: dict[tuple[str, int], list] = {}
    for (sym, _broker), series in p.flows.items():
        adtv = p.adtv.get(sym, {})
        by_i = dict(series)
        for i, net in series:
            if net > 0:
                buys.setdefault((sym, i), []).append(net)
            if net < B.MIN_VALUE:
                continue
            a = adtv.get(i)
            if not a or net < (B.MIN_ADTV_PCT / 100.0) * a:
                continue
            if sum(1 for j in range(i - 2, i + 1) if by_i.get(j, 0) > 0) < 2:
                continue
            qual[(sym, i)] = qual.get((sym, i), 0) + 1
    return qual, buys


def build_rows(p: Panel) -> list[dict]:
    """Every stock-day with features. The price-leg days carry RSI 'then' at each lookback."""
    qual, buys = broker_days(p)
    rows = []
    for sym in sorted(p.close):
        tn = p.turnover.get(sym, {})
        for i in sorted(p.close[sym]):
            f = lean(p, sym, i)
            if not f or f["rvol5"] is None:
                continue
            prior = [tn[j] for j in range(i - FLOOR_WIN, i) if tn.get(j)]
            med = statistics.median(prior) if len(prior) >= FLOOR_WIN // 2 else 0.0
            r = {"symbol": sym, "i": i, "rsi": f["rsi"], "rvol5": f["rvol5"], "dd60": f["dd60"],
                 "liquid": med >= FLOOR_IDR, "median_value": med,
                 "P": is_momentum(f, B.RVOL_MIN, B.DD_MIN, B.RSI_MIN, B.RVOL_MAX),
                 "n_qual": qual.get((sym, i), 0)}
            r["A"] = r["n_qual"] > 0
            in_band = B.RVOL_MIN <= f["rvol5"] < B.RVOL_MAX and f["dd60"] >= B.DD_MIN
            if r["P"] or (in_band and APPROACH_LO <= f["rsi"] < B.RSI_MIN):
                for b in (BACK,) + BACK_SENS:
                    g = lean(p, sym, i - b)
                    r[f"then{b}"] = g["rsi"] if g else None
                top = sorted(buys.get((sym, i), []), reverse=True)[:5]
                r["conc5"] = (sum(top) / tn[i]) if tn.get(i) else None
            rows.append(r)
    return rows


def attach_returns(p: Panel, rows: list[dict]) -> None:
    for r in rows:
        for k in HORIZONS:
            r[f"x{k}"] = p.excess_return(r["symbol"], r["i"], k, entry_lag=1)


# ------------------------------------------------------------------ statistics

def diff_pp(obs: list[tuple]) -> float | None:
    """stat() for the date-block bootstrap. obs = [(arm, excess), ...]; arm 1 minus arm 0."""
    a = [x for arm, x in obs if arm == 1]
    b = [x for arm, x in obs if arm == 0]
    if not a or not b:
        return None
    return (statistics.fmean(a) - statistics.fmean(b)) * 100.0


def diff_suff(obs: list[tuple]) -> float | None:
    """diff_pp over per-date sufficient statistics (sum1, n1, sum0, n0).

    A difference of two means needs only each date's sums and counts, and a bootstrap that
    resamples whole dates never looks inside one. Carrying one tuple per date instead of
    every row makes 2,000 draws over 50,000 stock-days a matter of seconds on the box, and
    is the same number: `selftest()` holds it to diff_pp.
    """
    s1 = sum(o[0] for o in obs)
    n1 = sum(o[1] for o in obs)
    s0 = sum(o[2] for o in obs)
    n0 = sum(o[3] for o in obs)
    if not n1 or not n0:
        return None
    return (s1 / n1 - s0 / n0) * 100.0


def arm_stats(rows: list[dict], key, k: int) -> dict:
    out = {}
    for arm in (1, 0):
        xs = [r[f"x{k}"] for r in rows if key(r) == arm and r.get(f"x{k}") is not None]
        out[arm] = {"n": len(xs),
                    "mean_pp": statistics.fmean(xs) * 100 if xs else None,
                    "median_pp": statistics.median(xs) * 100 if xs else None,
                    "hit": sum(1 for x in xs if x > 0) / len(xs) if xs else None}
    return out


def contrast(p: Panel, rows: list[dict], key, k: int, n_boot: int, n_null: int = 0) -> dict:
    """Arm 1 minus arm 0 at horizon k: point, 10/90 date-block band, folds, blocks, null."""
    use = [r for r in rows if key(r) in (0, 1) and r.get(f"x{k}") is not None]
    acc: dict[int, list] = {}
    for r in use:
        a = acc.setdefault(r["i"], [0.0, 0, 0.0, 0])
        if key(r) == 1:
            a[0] += r[f"x{k}"]
            a[1] += 1
        else:
            a[2] += r[f"x{k}"]
            a[3] += 1
    per_date = {i: [tuple(a)] for i, a in acc.items()}
    out = {"arms": arm_stats(use, key, k),
           "diff_pp": diff_pp([(key(r), r[f"x{k}"]) for r in use]),
           "bootstrap": LL.date_block_bootstrap(per_date, diff_suff, n_boot=n_boot) if per_date else {},
           "blocks_arm1": LL.blocks_with_treatment([r["i"] for r in use if key(r) == 1]),
           "blocks_arm0": LL.blocks_with_treatment([r["i"] for r in use if key(r) == 0]),
           "folds": folds(p, use, key, k)}
    if n_null:
        out["null"] = shift_null(use, key, k, n_null)
    return out


def folds(p: Panel, rows: list[dict], key, k: int, n: int = 4) -> list[dict]:
    """4 equal CALENDAR stretches, never equal event counts."""
    if not rows:
        return []
    idxs = [r["i"] for r in rows]
    lo, hi = min(idxs), max(idxs) + 1
    size = (hi - lo) / n
    out = []
    for j in range(n):
        a, b = lo + int(j * size), lo + int((j + 1) * size)
        sub = [r for r in rows if a <= r["i"] < b]
        n1 = sum(1 for r in sub if key(r) == 1)
        n0 = sum(1 for r in sub if key(r) == 0)
        val = (diff_pp([(key(r), r[f"x{k}"]) for r in sub])
               if min(n1, n0) >= MIN_ARM_FOLD_N else None)
        out.append({"fold": j + 1, "from": p.dates[a], "to": p.dates[min(b, len(p.dates) - 1)],
                    "n1": n1, "n0": n0, "diff_pp": val})
    return out


def shift_null(rows: list[dict], key, k: int, draws: int, seed: int = 7) -> dict:
    """Circularly shift each symbol's ARM series against its own returns.

    Keeps every stock's return distribution and every arm's run structure, destroys the
    alignment between them. The population is held fixed, so it asks exactly one thing:
    given these days, does WHICH of them is labelled arm 1 carry anything?
    """
    by_sym: dict[str, list[dict]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    for v in by_sym.values():
        v.sort(key=lambda r: r["i"])
    rng = random.Random(seed)
    vals = []
    for _ in range(draws):
        obs = []
        for ser in by_sym.values():
            n = len(ser)
            off = rng.randrange(n) if n > 1 else 0
            obs.extend((key(ser[(j + off) % n]), ser[j][f"x{k}"]) for j in range(n))
        v = diff_pp(obs)
        if v is not None:
            vals.append(v)
    if not vals:
        return {"draws": 0}
    s = sorted(vals)
    return {"draws": len(vals), "mean_pp": statistics.fmean(vals),
            "sd_pp": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "p05_pp": s[int(0.05 * len(s))], "p95_pp": s[min(len(s) - 1, int(0.95 * len(s)))]}


def level_matched(rows: list[dict], key, k: int) -> dict:
    """Arm 1 minus arm 0 WITHIN bands of today's RSI, weighted by arm-1 counts.

    A name that came up through 55 sits lower in the RSI range today than one that has been
    above it for a month, so the raw contrast mixes heading with level. This one holds the
    level and leaves the heading.
    """
    num = den = 0.0
    cells = []
    for lo, hi in LEVEL_BANDS:
        sub = [r for r in rows if lo <= r["rsi"] < hi and r.get(f"x{k}") is not None]
        a = [r[f"x{k}"] for r in sub if key(r) == 1]
        b = [r[f"x{k}"] for r in sub if key(r) == 0]
        d = (statistics.fmean(a) - statistics.fmean(b)) * 100 if a and b else None
        cells.append({"band": f"[{lo:.0f},{min(hi, 100):.0f})", "n1": len(a), "n0": len(b), "diff_pp": d})
        if d is not None:
            num += len(a) * d
            den += len(a)
    return {"matched_pp": num / den if den else None, "cells": cells}


def quintile_gradient(rows: list[dict], score, k: int) -> list[dict]:
    use = [r for r in rows if score(r) is not None and r.get(f"x{k}") is not None]
    if len(use) < N_Q * 10:
        return []
    s = sorted(score(r) for r in use)
    cuts = [s[int(len(s) * j / N_Q)] for j in range(1, N_Q)]
    out = []
    for q in range(1, N_Q + 1):
        lo = cuts[q - 2] if q > 1 else float("-inf")
        hi = cuts[q - 1] if q < N_Q else float("inf")
        xs = [r[f"x{k}"] for r in use if lo <= score(r) < hi]
        out.append({"q": q, "n": len(xs), "from": None if q == 1 else lo,
                    "mean_pp": statistics.fmean(xs) * 100 if xs else None,
                    "hit": sum(1 for x in xs if x > 0) / len(xs) if xs else None})
    return out


def fmt(v, spec="+.3f", na="n/a"):
    return na if v is None else format(v, spec)


# ------------------------------------------------------------------ arms

def fresh(back: int):
    """1 = RSI was under the gate `back` sessions ago (came up to it), 0 = already at or above."""
    def key(r):
        t = r.get(f"then{back}")
        return None if t is None else (1 if t < B.RSI_MIN else 0)
    return key


def has_broker(r):
    return 1 if r["A"] else 0


# ------------------------------------------------------------------ occupancy

def occupancy(p: Panel, rows: list[dict]) -> None:
    """Cell counts and nothing else. No forward return is computed on this path."""
    liq = [r for r in rows if r["liquid"]]
    P = [r for r in liq if r["P"]]
    log(f"stock-days with features : {len(rows):>6}")
    log(f"  above the Rp{FLOOR_IDR / 1e9:.0f}bn floor : {len(liq):>6}   ({len({r['symbol'] for r in liq})} names)")
    log(f"  price leg passes (P)   : {len(P):>6}   {100 * len(P) / max(1, len(liq)):.1f}% of floored days, "
        f"{len({r['i'] for r in P})} distinct dates, {len({r['symbol'] for r in P})} names")
    pa = [r for r in P if r["A"]]
    log(f"\nH-A  P with a qualifying broker: {len(pa)}   P without: {len(P) - len(pa)}"
        f"   (board without the floor: {sum(1 for r in rows if r['P'] and r['A'])})")
    log(f"     blocks: with {LL.blocks_with_treatment([r['i'] for r in pa])}, "
        f"without {LL.blocks_with_treatment([r['i'] for r in P if not r['A']])}")
    log(f"     brokers qualifying on a P day: " + "  ".join(
        f"{lab}:{sum(1 for r in P if lo <= r['n_qual'] < hi)}"
        for lab, lo, hi in (("0", 0, 1), ("1", 1, 2), ("2", 2, 3), ("3+", 3, 99))))
    for b in (BACK,) + BACK_SENS:
        key = fresh(b)
        f1 = [r for r in P if key(r) == 1]
        f0 = [r for r in P if key(r) == 0]
        log(f"\nH-B  lookback {b:>2}: came up through {B.RSI_MIN:.0f} (fresh) {len(f1)}   already above (stale) {len(f0)}"
            f"   no RSI then {sum(1 for r in P if key(r) is None)}")
        log(f"     blocks: fresh {LL.blocks_with_treatment([r['i'] for r in f1])}, "
            f"stale {LL.blocks_with_treatment([r['i'] for r in f0])}")
        if b == BACK:
            for lo, hi in LEVEL_BANDS:
                log(f"     RSI today [{lo:.0f},{min(hi, 100):.0f}): fresh {sum(1 for r in f1 if lo <= r['rsi'] < hi):>5}  "
                    f"stale {sum(1 for r in f0 if lo <= r['rsi'] < hi):>5}")
            for j, fo in enumerate(folds_count(p, P, key)):
                log(f"     fold {j + 1} {fo['from']} .. {fo['to']}: fresh {fo['n1']:>4}  stale {fo['n0']:>4}")
            st = [r for r in f0]
            log(f"     within stale: RSI higher than then {sum(1 for r in st if r['rsi'] > r[f'then{b}'])}, "
                f"lower or equal {sum(1 for r in st if r['rsi'] <= r[f'then{b}'])}")
    ap = [r for r in liq if not r["P"] and r.get(f"then{BACK}") is not None
          and APPROACH_LO <= r["rsi"] < B.RSI_MIN and r["rsi"] - r[f"then{BACK}"] >= APPROACH_RISE]
    log(f"\nread-out cell 'approaching' (RVOL in band, DD60 ok, RSI {APPROACH_LO:.0f}-{B.RSI_MIN:.0f}, "
        f"up {APPROACH_RISE:.0f}+ in {BACK}): {len(ap)}")
    c = sorted(r["conc5"] for r in P if r.get("conc5") is not None)
    if c:
        log(f"H-C  top-5 net buying / day's value on P days: "
            + "  ".join(f"p{q}:{c[min(len(c) - 1, int(q / 100 * len(c)))]:.2f}" for q in (10, 25, 50, 75, 90))
            + f"   (n={len(c)})")


def folds_count(p: Panel, rows: list[dict], key, n: int = 4) -> list[dict]:
    idxs = [r["i"] for r in rows]
    lo, hi = min(idxs), max(idxs) + 1
    size = (hi - lo) / n
    out = []
    for j in range(n):
        a, b = lo + int(j * size), lo + int((j + 1) * size)
        sub = [r for r in rows if a <= r["i"] < b]
        out.append({"from": p.dates[a], "to": p.dates[min(b, len(p.dates) - 1)],
                    "n1": sum(1 for r in sub if key(r) == 1), "n0": sum(1 for r in sub if key(r) == 0)})
    return out


# ------------------------------------------------------------------ report helpers

def show(title: str, c: dict) -> None:
    a1, a0 = c["arms"][1], c["arms"][0]
    b = c.get("bootstrap") or {}
    log(f"{title}")
    log(f"    arm 1  n {a1['n']:>5}  mean {fmt(a1['mean_pp'])}pp  median {fmt(a1['median_pp'])}pp  hit {fmt(a1['hit'], '.1%')}")
    log(f"    arm 0  n {a0['n']:>5}  mean {fmt(a0['mean_pp'])}pp  median {fmt(a0['median_pp'])}pp  hit {fmt(a0['hit'], '.1%')}")
    log(f"    arm 1 - arm 0 = {fmt(c['diff_pp'])}pp   band [{fmt(b.get('lo'), '+.2f')}, {fmt(b.get('hi'), '+.2f')}] "
        f"(10/90, {b.get('block')}-day blocks)   blocks {c['blocks_arm1']}/{c['blocks_arm0']}")
    if c.get("null"):
        log(f"    shift null: mean {fmt(c['null'].get('mean_pp'))}pp  sd {fmt(c['null'].get('sd_pp'), '.3f')}  "
            f"p05..p95 [{fmt(c['null'].get('p05_pp'), '+.2f')}, {fmt(c['null'].get('p95_pp'), '+.2f')}]")
    if c.get("folds"):
        log("    folds: " + "   ".join(
            f"{f['fold']}: {fmt(f['diff_pp'], '+.2f')} ({f['n1']}/{f['n0']})" for f in c["folds"]))


def verdict(c: dict, extra: list[tuple[str, bool]], c0_ok: bool) -> tuple[list, str]:
    b = c.get("bootstrap") or {}
    d = c["diff_pp"]
    band_clear = b.get("lo") is not None and b.get("hi") is not None and (b["lo"] > 0 or b["hi"] < 0)
    folds_pos = sum(1 for f in c["folds"] if (f["diff_pp"] or 0) > 0)
    checks = [(f"1 difference >= +{BAR_PP}pp", d is not None and d >= BAR_PP),
              ("1b band clear of zero", band_clear)] + extra + [
              (f"4 null within +/-{BAR_NULL_PP}pp", abs((c.get("null") or {}).get("mean_pp", 99)) <= BAR_NULL_PP),
              (f"5 folds positive >= {BAR_FOLDS}/4", folds_pos >= BAR_FOLDS),
              ("6 check 0", c0_ok),
              ("7 blocks >= 15 in both arms", LL.is_inferential(min(c["blocks_arm1"], c["blocks_arm0"])))]
    return checks, ("PASS" if all(ok for _, ok in checks) else "FAIL")


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    """Synthetic rows with a known answer. No panel, no returns from the market."""
    fails = []
    rng = random.Random(1)

    class FakePanel:
        dates = [f"d{j:03d}" for j in range(640)]

    rows = []
    for i in range(60, 600):                      # 18 thirty-session blocks: inferential
        for s in range(12):
            arm = 1 if (i // 7 + s) % 3 == 0 else 0
            rows.append({"symbol": f"S{s}", "i": i, "rsi": 55 + (s * 3 + i) % 40, "arm": arm,
                         "x5": 0.02 * arm + rng.gauss(0, 0.01)})       # arm 1 earns +2pp by construction
    key = lambda r: r["arm"]
    c = contrast(FakePanel, rows, key, 5, n_boot=200, n_null=40)
    obs = [(key(r), r["x5"]) for r in rows]
    acc: dict[int, list] = {}
    for r in rows:
        a = acc.setdefault(r["i"], [0.0, 0, 0.0, 0])
        a[0 if r["arm"] else 2] += r["x5"]
        a[1 if r["arm"] else 3] += 1
    if abs(diff_pp(obs) - diff_suff([tuple(a) for a in acc.values()])) > 1e-9:
        fails.append("diff_suff must equal diff_pp")
    if not 1.8 < c["diff_pp"] < 2.2:
        fails.append(f"planted +2pp not recovered: {c['diff_pp']}")
    b = c["bootstrap"]
    if not (b["lo"] is not None and b["lo"] > 1.5 and b["hi"] < 2.5):
        fails.append(f"band should bracket +2pp tightly: {b}")
    if abs(c["null"]["mean_pp"]) > 0.3:
        fails.append(f"shift null should centre on zero: {c['null']}")
    if len(c["folds"]) != 4 or any(f["diff_pp"] is None or not 1.5 < f["diff_pp"] < 2.5 for f in c["folds"]):
        fails.append(f"folds: {c['folds']}")
    lm = level_matched(rows, key, 5)
    if lm["matched_pp"] is None or not 1.7 < lm["matched_pp"] < 2.3:
        fails.append(f"level-matched: {lm}")
    flat = [dict(r, x5=rng.gauss(0, 0.01)) for r in rows]                 # no effect planted
    c2 = contrast(FakePanel, flat, key, 5, n_boot=200, n_null=40)
    checks, v = verdict(c2, [], True)
    if v != "FAIL" or abs(c2["diff_pp"]) > 0.4:
        fails.append(f"a no-effect panel must FAIL: {c2['diff_pp']} {v}")
    checks, v = verdict(c, [], True)
    if v != "PASS":
        fails.append(f"a planted +2pp must PASS: {checks}")
    g = quintile_gradient([dict(r, sc=r["x5"]) for r in rows], lambda r: r["sc"], 5)
    if len(g) != N_Q or not all(a["mean_pp"] < b["mean_pp"] for a, b in zip(g, g[1:])):
        fails.append("quintiles of the outcome itself must be monotone")
    t = fresh(20)
    if (t({"then20": 40.0}), t({"then20": 55.0}), t({"then20": None}), t({})) != (1, 0, None, None):
        fails.append("fresh(): under the gate is 1, at the gate is 0, missing is None")
    for f in fails:
        log(f"  FAIL  {f}")
    log(f"selftest: {'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (9 checks)")
    return 1 if fails else 0


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="synthetic data with a known answer")
    ap.add_argument("--occupancy", action="store_true", help="count the cells and stop; no returns")
    ap.add_argument("--quick", action="store_true", help="fewer bootstrap and null draws")
    ap.add_argument("--out", default=str(PANEL / "heading_test.json"))
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    n_boot = 300 if args.quick else 2000
    n_null = 50 if args.quick else 200

    log("=" * 78)
    log("RSI HEADING and THE BROKER LEG.  Pre-registered in reference/rsi-heading.md.")
    log("=" * 78)
    p = Panel()
    p.load()
    log(f"panel: {len(p.raw_close)} symbols x {len(p.dates)} sessions ({p.dates[0]} .. {p.dates[-1]}), "
        f"{len(p.bench)} benchmark days")
    bad = parity(p)
    log(f"parity lean() vs features(): {bad} mismatches on 300 sampled stock-days")
    if bad:
        log("  [!!] the lean feature path disagrees with the board's. Refusing to run.")
        return 3

    rows = build_rows(p)
    if args.occupancy:
        log("\nOCCUPANCY -- counts only. No forward return has been computed.\n")
        occupancy(p, rows)
        return 0

    # ---- check 0 FIRST. A verdict from a hostile window is not a verdict.
    cands = TB.build_candidates(p)
    c0 = TB.check_zero(p, cands, k=PRIMARY_K, bar=BAR_CHECK0)
    pooled = c0.get("pooled", {})
    log(f"\ncheck 0: momentum lift in-window {100 * (pooled.get('lift') or 0):+.2f}pp on n={pooled.get('n')} "
        f"(bar +{100 * BAR_CHECK0:.1f}pp) -> {'PASS' if c0.get('ok') else 'FAIL'}")
    if not c0.get("ok"):
        log("  [!!] the known-good rule does NOT work in this window. Stopping.")
        return 4

    attach_returns(p, rows)
    liq = [r for r in rows if r["liquid"]]
    P = [r for r in liq if r["P"]]
    k = PRIMARY_K

    # ---- baselines, BEFORE any conditional statistic
    def mean_pp(rs, kk=k):
        xs = [r[f"x{kk}"] for r in rs if r.get(f"x{kk}") is not None]
        return (statistics.fmean(xs) * 100 if xs else None), len(xs)
    base, n_base = mean_pp(liq)
    pm, n_p = mean_pp(P)
    log(f"\nbaselines at k={k} (entry next close, excess over IHSG)")
    log(f"  every floored stock-day : {fmt(base)}pp  (n={n_base})")
    log(f"  price leg passes (P)    : {fmt(pm)}pp  (n={n_p})   lift over baseline {fmt(pm - base if pm is not None else None)}pp")
    c_p = contrast(p, liq, lambda r: 1 if r["P"] else 0, k, n_boot)
    show("  P against every other floored stock-day:", c_p)

    # ---- H-A
    log("\n" + "-" * 78 + "\nH-A  does the broker leg add to the price leg?   arm 1 = P with a qualifying broker\n" + "-" * 78)
    hA = contrast(p, P, has_broker, k, n_boot, n_null)
    show(f"  k={k} (PRIMARY)", hA)
    checksA, verdictA = verdict(hA, [], bool(c0.get("ok")))
    for kk in HORIZONS[1:]:
        show(f"  k={kk} (read-out)", contrast(p, P, has_broker, kk, n_boot))
    log("  by number of qualifying brokers (read-out): " + "   ".join(
        f"{lab}: {fmt(mean_pp([r for r in P if lo <= r['n_qual'] < hi])[0], '+.2f')}pp "
        f"(n={mean_pp([r for r in P if lo <= r['n_qual'] < hi])[1]})"
        for lab, lo, hi in (("0", 0, 1), ("1", 1, 2), ("2", 2, 3), ("3+", 3, 99))))

    # ---- H-B
    log("\n" + "-" * 78 + f"\nH-B  RSI heading.   arm 1 = RSI was under {B.RSI_MIN:.0f} {BACK} sessions ago (came up), "
        f"arm 0 = already at or above\n" + "-" * 78)
    keyB = fresh(BACK)
    hB = contrast(p, P, keyB, k, n_boot, n_null)
    show(f"  k={k} (PRIMARY)", hB)
    lm = level_matched([r for r in P if keyB(r) is not None], keyB, k)
    log(f"    level-matched (same RSI band today): {fmt(lm['matched_pp'])}pp   " + "   ".join(
        f"{c['band']}: {fmt(c['diff_pp'], '+.2f')} ({c['n1']}/{c['n0']})" for c in lm["cells"]))
    sens = {}
    for b in BACK_SENS:
        sens[b] = contrast(p, P, fresh(b), k, n_boot)
        show(f"  lookback {b} (sign check)", sens[b])
    d = hB["diff_pp"]
    same_sign = lambda v: v is not None and d is not None and (v > 0) == (d > 0)
    checksB, verdictB = verdict(hB, [
        ("2 level-matched contrast has the same sign", same_sign(lm["matched_pp"])),
        (f"3 same sign at lookbacks {BACK_SENS[0]} and {BACK_SENS[1]}",
         all(same_sign(sens[b]["diff_pp"]) for b in BACK_SENS))], bool(c0.get("ok")))
    for kk in HORIZONS[1:]:
        show(f"  k={kk} (read-out)", contrast(p, P, keyB, kk, n_boot))
    show("  inside the board as it is, P with a broker (read-out)",
         contrast(p, [r for r in P if r["A"]], keyB, k, n_boot))
    stale = [r for r in P if keyB(r) == 0]
    show("  within 'already above': RSI higher than then (1) vs lower or equal (0) (read-out)",
         contrast(p, stale, lambda r: 1 if r["rsi"] > r[f"then{BACK}"] else 0, k, n_boot))
    grad = quintile_gradient(P, lambda r: None if r.get(f"then{BACK}") is None else r["rsi"] - r[f"then{BACK}"], k)
    log("  change in RSI over the lookback, quintiles Q1 (fell most) .. Q5 (rose most) (read-out): " + "   ".join(
        f"Q{g['q']}: {fmt(g['mean_pp'], '+.2f')} ({g['n']})" for g in grad))
    appr = [r for r in liq if not r["P"] and r.get(f"then{BACK}") is not None
            and APPROACH_LO <= r["rsi"] < B.RSI_MIN and r["rsi"] - r[f"then{BACK}"] >= APPROACH_RISE]
    am, an = mean_pp(appr)
    log(f"  'approaching' cell, not yet at the gate (read-out): {fmt(am)}pp (n={an})  against baseline {fmt(base)}pp")
    bal = {}
    for arm in (1, 0):
        sub = [r for r in P if keyB(r) == arm]
        bal[arm] = {m: statistics.fmean(r[m] for r in sub) if sub else None for m in ("rsi", "rvol5", "dd60")}
        bal[arm]["median_value_bn"] = statistics.median(r["median_value"] for r in sub) / 1e9 if sub else None
        bal[arm]["broker_share"] = sum(1 for r in sub if r["A"]) / len(sub) if sub else None
    log("  balance (arm 1 / arm 0): " + "   ".join(
        f"{m} {fmt(bal[1][m], '.2f')}/{fmt(bal[0][m], '.2f')}" for m in ("rsi", "rvol5", "dd60", "median_value_bn", "broker_share")))

    # ---- H-C
    log("\n" + "-" * 78 + "\nH-C  identity-free broker read-out: top-5 net buying as a share of the day's value (READ-OUT)\n" + "-" * 78)
    gradC = quintile_gradient(P, lambda r: r.get("conc5"), k)
    log("  quintiles Q1 (least concentrated) .. Q5 (most): " + "   ".join(
        f"Q{g['q']}: {fmt(g['mean_pp'], '+.2f')} ({g['n']})" for g in gradC))

    # ---- verdicts
    log("\n" + "=" * 78)
    log("H-A  the broker leg adds to the price leg")
    for label, ok in checksA:
        log(f"  [{'PASS' if ok else 'FAIL'}]  {label}")
    log(f"  VERDICT H-A: {verdictA}")
    log("\nH-B  a move up through the gate beats already being above it")
    for label, ok in checksB:
        log(f"  [{'PASS' if ok else 'FAIL'}]  {label}")
    log(f"  VERDICT H-B: {verdictB}")
    log("=" * 78)

    payload = {
        "study": "RSI heading and the broker leg", "preregistered": "reference/rsi-heading.md",
        "panel_fingerprint": panel_fingerprint(),
        "params": {"back": BACK, "back_sens": BACK_SENS, "horizons": HORIZONS, "floor_idr": FLOOR_IDR,
                   "n_boot": n_boot, "n_null": n_null},
        "check_zero": c0, "baseline_pp": base, "n_baseline": n_base, "price_leg_pp": pm, "n_price_leg": n_p,
        "price_leg_vs_rest": c_p, "H_A": hA, "H_A_checks": dict(checksA), "H_A_verdict": verdictA,
        "H_B": hB, "H_B_level_matched": lm, "H_B_sensitivity": {str(b): sens[b] for b in BACK_SENS},
        "H_B_checks": dict(checksB), "H_B_verdict": verdictB, "H_B_change_quintiles": grad,
        "H_B_balance": {str(a): bal[a] for a in bal}, "approaching": {"mean_pp": am, "n": an},
        "H_C_conc5_quintiles": gradC,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2, default=float), encoding="utf-8")
    log(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
