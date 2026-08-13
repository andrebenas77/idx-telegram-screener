#!/usr/bin/env python3
"""Validation harness for the accumulation board — and the coalition A/B.

TWO MODES

  --mode coalition   Settles the question the calibration set raised: should the gate
                     require ONE broker to clear the size floor, or the SUM over the
                     one-sided brokers? Runs on the FREE net-only panel, so n is large
                     and it costs no API calls. See "the substitution" below.

  --mode gate        The full §6 protocol from reference/accumulation.md, using the real
                     gross partition. Requires backfill_gross.py to have run.

THE SUBSTITUTION (why the coalition A/B can run on free data)

    The coalition question is about AGGREGATION, not about how one-sidedness is measured.
    Both gates use the identical broker filter; they differ only in whether the size
    floor is applied per-broker or to the coalition sum. So the filter can be a PROXY
    without biasing the comparison, as long as it is the same proxy on both sides.

    The proxy is `softrun20` — the share of the last 20 sessions a broker was net
    positive — which is computable from the net-only panel already on disk. A broker net
    positive on 18 of 20 sessions is behaviourally one-sided even when the exact
    buy/(buy+sell) ratio is unavailable.

    This is NOT a substitute for the real gate. `osr` and `softrun` are different
    quantities: CC on BREN was net positive most days while running 57% two-way churn,
    and only `osr` catches that. So a coalition win here is a STRUCTURAL result about
    aggregation, to be confirmed on the gross partition in --mode gate before it changes
    what ships.

THE DECIDING TEST is not "does B beat the baseline" — B fires strictly more often than A,
so it inherits A's edge diluted by whatever else it admits. The question is whether the
MARGINAL events are any good:

    B_only = events firing under the coalition gate but NOT under the single-broker gate

If B_only's mean excess is comparable to A's, the coalition is finding real campaigns that
the single-broker floor was rejecting for being spread across accounts. If B_only is ~0,
the extra names are noise and the single-broker form is right.

Usage:
    py accum_test.py --mode coalition
    py accum_test.py --mode coalition --theta-adtv 10,20,40
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import accum_lib  # noqa: E402
from alpha_lib import Panel, summarise  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "data" / "panel"
WIB = timezone(timedelta(hours=7))

BN = 1_000_000_000
HORIZONS = (3, 5, 10)
SOFTRUN_MIN = 0.75          # proxy for one-sidedness; see the module docstring
NET_FLOOR_IDR = 10 * BN
FOLDS = 4
SEED = 20260813             # fixed: a shuffled null that moves between runs proves nothing


def tainted_symbols() -> set[str]:
    """Names whose price series still has an unexplained jump.

    backfill_panel.py's adjustment check records residual breaks — a move so large it can
    only be an unhandled split, bonus or rights issue. COCO showed +248.8% across
    2026-07-06/07 in this build. An unadjusted 3.5x is not a return, and a handful of them
    can carry a whole mean on their own, so they are excluded rather than averaged in.
    """
    rep = PANEL / "backfill_report.json"
    if not rep.exists():
        return set()
    try:
        breaks = json.loads(rep.read_text(encoding="utf-8")).get("residual_breaks") or []
    except Exception:
        return set()
    out = set()
    for b in breaks:
        if isinstance(b, dict):
            s = b.get("symbol") or b.get("sym")
            if s:
                out.add(str(s).upper())
        elif isinstance(b, str):
            out.add(b.split()[0].upper())
    return out


def load_universe_membership() -> dict[str, set[str]]:
    """{date: {symbols in the top-N by value that day}} from universe-*.csv.gz."""
    out: dict[str, set[str]] = defaultdict(set)
    for f in sorted(PANEL.glob("universe-*.csv.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    if int(r["rank_value"]) <= 20:
                        out[r["date"]].add(r["symbol"])
                except (TypeError, ValueError, KeyError):
                    continue
    return out


def broker_series(p: Panel) -> dict[str, dict[str, dict[int, float]]]:
    """{sym: {broker: {i: net}}} — reshaped from Panel.flows for windowed lookups."""
    out: dict[str, dict[str, dict[int, float]]] = defaultdict(lambda: defaultdict(dict))
    for (sym, broker), series in p.flows.items():
        for i, v in series:
            out[sym][broker][i] = v
    return out


def qualifying_brokers(bs: dict[str, dict[int, float]], i: int, w: int = 20):
    """Brokers whose recent flow is persistently one-directional, with their window net.

    Returns [(broker, net_w, softrun)] for brokers clearing SOFTRUN_MIN. The window is
    the w sessions ENDING AT i inclusive — day i's flow is known at the close, and the
    entry lag in excess_return() is what keeps this honest.
    """
    lo = i - w + 1
    out = []
    for broker, series in bs.items():
        vals = [series.get(j) for j in range(lo, i + 1)]
        seen = [v for v in vals if v is not None]
        if len(seen) < max(5, w // 4):        # too little activity to characterise
            continue
        pos = sum(1 for v in seen if v > 0)
        sr = pos / len(seen)
        if sr >= SOFTRUN_MIN:
            out.append((broker, sum(seen), sr))
    return out


def build_events(p: Panel, flows: dict, universe: dict[str, set[str]],
                 theta_adtv: float, w: int = 20) -> dict[str, list]:
    """Events under both gates, plus the marginal set."""
    single, coalition = [], []
    for i, d in enumerate(p.dates):
        if i < w or i + max(HORIZONS) + 1 >= len(p.dates):
            continue
        members = universe.get(d)
        if not members:
            continue
        for sym in members:
            bs = flows.get(sym)
            if not bs:
                continue
            adtv = (p.adtv.get(sym) or {}).get(i)
            if not adtv:
                continue
            floor = max(NET_FLOOR_IDR, theta_adtv * adtv)
            qual = qualifying_brokers(bs, i, w)
            if not qual:
                continue
            best = max((n for _, n, _ in qual), default=0.0)
            total = sum(n for _, n, _ in qual if n > 0)
            if best >= floor:
                single.append((sym, i, best, len(qual)))
            if total >= floor:
                coalition.append((sym, i, total, len(qual)))
    skey = {(s, i) for s, i, _, _ in single}
    only = [e for e in coalition if (e[0], e[1]) not in skey]
    return {"single": single, "coalition": coalition, "coalition_only": only}


def baseline_events(p: Panel, universe: dict[str, set[str]], w: int = 20) -> list:
    """Every (symbol, day) in the universe — the MATCHED baseline.

    Matched, not zero and not the whole panel: the board only ever chooses among
    top-20-by-value names, so the honest question is whether it beats owning one of those
    at random on the same day, which in IDX's most-traded names is itself a real edge.
    """
    out = []
    for i, d in enumerate(p.dates):
        if i < w or i + max(HORIZONS) + 1 >= len(p.dates):
            continue
        for sym in universe.get(d, ()):
            if (p.adtv.get(sym) or {}).get(i):
                out.append((sym, i, 0.0, 0))
    return out


def measure(p: Panel, events: list, k: int) -> dict:
    xs = []
    for sym, i, _, _ in events:
        x = p.excess_return(sym, i, k, entry_lag=1)   # entry lag is MANDATORY
        if x is not None:
            xs.append(x)
    return summarise(xs) if xs else {"n": 0, "mean_excess": None}


def by_fold(p: Panel, events: list, k: int, folds: int = FOLDS,
            lo_i: int | None = None, hi_i: int | None = None) -> list:
    """Split by CALENDAR PERIOD, not by event index — equal event counts per fold would
    hide a regime where the rule stopped firing altogether.

    lo_i/hi_i bound the split to the range the DATA actually covers. Splitting the whole
    475-session panel when the gross partition spans 59 sessions puts every event in the
    last fold and reports the other three as n/a, which reads as a stability result and
    is not one.
    """
    if not p.dates:
        return []
    a = 0 if lo_i is None else lo_i
    b = len(p.dates) if hi_i is None else hi_i + 1
    size = max(1, (b - a)) / folds
    out = []
    for f in range(folds):
        lo, hi = a + int(f * size), a + int((f + 1) * size)
        sub = [e for e in events if lo <= e[1] < hi]
        out.append(measure(p, sub, k))
    return out


def shuffle_broker_labels(flows: dict, rng: random.Random) -> dict:
    """Null 1 — reassign broker identities within each symbol. Tests 'is it identity?'"""
    out = {}
    for sym, bs in flows.items():
        codes = list(bs)
        shuffled = codes[:]
        rng.shuffle(shuffled)
        out[sym] = {new: bs[old] for old, new in zip(codes, shuffled)}
    return out


def shift_dates(flows: dict, rng: random.Random, n_dates: int) -> dict:
    """Null 2 — circularly shift each (symbol, broker) series by a random offset.

    THE IMPORTANT ONE. It preserves each broker's autocorrelation and each stock's return
    distribution while destroying flow-to-price alignment. Persistence is highly
    autocorrelated, so a leaking harness sails through null 1 and fails only here.
    """
    out = {}
    for sym, bs in flows.items():
        out[sym] = {}
        for broker, series in bs.items():
            off = rng.randrange(n_dates)
            out[sym][broker] = {(i + off) % n_dates: v for i, v in series.items()}
    return out


# ==================================================================== gate mode

def load_gross_indexed(p: Panel) -> dict[str, dict[str, list]]:
    """{sym: {broker: [(i, buy_value, sell_value, buy_freq, buy_avg), ...]}} sorted by i.

    Sparse and index-keyed. A dense array per (symbol, broker) over 475 sessions would be
    ~2M cells for 53 names, most of them zero.
    """
    out: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for path in sorted(PANEL.glob("gross-*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                i = p.didx.get(r["date"])
                if i is None:
                    continue
                try:
                    bv = float(r["buy_value"] or 0)
                    sv = float(r["sell_value"] or 0)
                    bf = float(r["buy_freq"] or 0)
                except (TypeError, ValueError):
                    continue
                ba = r.get("buy_avg")
                try:
                    ba = float(ba) if ba not in (None, "") else None
                except (TypeError, ValueError):
                    ba = None
                out[r["symbol"]][r["broker"]].append((i, bv, sv, bf, ba))
    for sym in out:
        for b in out[sym]:
            out[sym][b].sort()
    return dict(out)


def win(series: list, lo: int, hi: int) -> dict:
    """Sum a sparse (i, bv, sv, bf, ba) series over the inclusive index range."""
    bv = sv = bf = 0.0
    for i, b, s, fq, _ in series:
        if lo <= i <= hi:
            bv += b
            sv += s
            bf += fq
    return {"bv": bv, "sv": sv, "bf": bf, "net": bv - sv, "gross": bv + sv}


def xr_day(p: Panel, sym: str, i: int):
    cl = p.close.get(sym) or {}
    if i not in cl or (i - 1) not in cl or cl[i - 1] <= 0:
        return None
    b0, b1 = p.bench.get(i - 1), p.bench.get(i)
    if not b0 or not b1:
        return None
    return (cl[i] / cl[i - 1]) - (b1 / b0)


def xr_win(p: Panel, sym: str, i: int, w: int):
    cl, j = p.close.get(sym) or {}, i - w
    if i not in cl or j not in cl or cl[j] <= 0:
        return None
    b0, b1 = p.bench.get(j), p.bench.get(i)
    if not b0 or not b1:
        return None
    return (cl[i] / cl[j]) - (b1 / b0)


def dd_win(p: Panel, sym: str, i: int, w: int):
    cl = p.close.get(sym) or {}
    hist = [cl[j] for j in range(max(0, i - w + 1), i + 1) if j in cl]
    if len(hist) < 3 or max(hist) <= 0:
        return None
    return hist[-1] / max(hist) - 1


def rvol_win(p: Panel, sym: str, i: int):
    vol = p.volume.get(sym) or {}
    v5 = [vol[j] for j in range(i - 4, i + 1) if j in vol]
    v20 = [vol[j] for j in range(i - 19, i + 1) if j in vol]
    if len(v5) < 3 or len(v20) < 10 or not sum(v20):
        return None
    return (sum(v5) / len(v5)) / (sum(v20) / len(v20))


def gate_events(p: Panel, g: dict, universe: dict, hot: dict, theta_osr: float,
                theta_adtv: float, absorb_mode: str, use_tilt: bool,
                alpha: dict) -> tuple[list, list]:
    """Level 1 (broker-day) and Level 2 (board rows) events under one grid point.

    Uses accum_lib for every rule, which is the whole point: the thing measured here and
    the thing build_accum_board.py prints are the same functions, so they cannot drift.
    """
    l1, l2 = [], []
    n_brokers = len(alpha) or None
    idx_days = sorted({i for sym in g for b in g[sym] for i, *_ in g[sym][b]})
    if not idx_days:
        return l1, l2
    lo_i, hi_i = min(idx_days), max(idx_days)

    for i in range(lo_i + 20, hi_i + 1):
        if i + max(HORIZONS) + 1 >= len(p.dates):
            continue
        members = hot.get(i) or []
        for sym in members:
            series_by_b = g.get(sym)
            if not series_by_b:
                continue
            adtv = (p.adtv.get(sym) or {}).get(i)
            if not adtv:
                continue
            ctx = {"adtv": adtv, "rvol5": rvol_win(p, sym, i),
                   "dd20": dd_win(p, sym, i, 20), "xr": xr_day(p, sym, i),
                   "xr5": xr_win(p, sym, i, 5), "xr20": xr_win(p, sym, i, 20),
                   "absorb_mode": absorb_mode,
                   # the sweep axes, threaded into the BUCKET as well as into Level 1
                   "theta_osr": theta_osr, "theta_adtv": theta_adtv}
            best = None
            for code, series in series_by_b.items():
                w5 = win(series, i - 4, i)
                w20 = win(series, i - 19, i)
                w60 = win(series, i - 59, i)
                o20 = accum_lib.osr(w20["bv"], w20["sv"], adtv, window=20)
                if o20 is None:
                    continue
                o5 = accum_lib.osr(w5["bv"], w5["sv"], adtv, window=5)
                day = next((t for t in series if t[0] == i), None)
                net_today = (day[1] - day[2]) if day else None
                nets = {p.dates[t[0]]: t[1] - t[2] for t in series}
                d5 = [p.dates[j] for j in range(i - 4, i + 1)]
                d20 = [p.dates[j] for j in range(i - 19, i + 1)]
                xrs = {p.dates[j]: xr_day(p, sym, j) for j in range(i - 19, i + 1)}
                xrs = {k: v for k, v in xrs.items() if v is not None}

                # ---- Level 1: the declared broker-day event
                if o20 >= theta_osr and w20["net"] >= max(NET_FLOOR_IDR,
                                                          theta_adtv * adtv):
                    l1.append((sym, i, w20["net"], 0))

                fv = {"osr20": o20,
                      "adtv_pct20": accum_lib.adtv_pct(w20["net"], adtv),
                      "softrun20": accum_lib.softrun(nets, d20),
                      "absorb20": accum_lib.absorb_score(nets, xrs, d20),
                      "cost_gap20": None,
                      "slice_z20": None}
                tilt = accum_lib.quality_tilt((alpha.get(code) or {}).get("rank"),
                                              n_brokers) if use_tilt else 1.0
                wf = accum_lib.window_factor(w5["net"], w20["net"], w60["net"])
                score = accum_lib.stealth_score(fv, tilt=tilt, wf=wf)
                bucket = accum_lib.classify_bucket({
                    **ctx, "osr5": o5, "osr20": o20,
                    "osr1d": accum_lib.osr(day[1] if day else 0,
                                           day[2] if day else 0, adtv, window=1),
                    "net5": w5["net"], "gross20": w20["gross"],
                    "absorb5": accum_lib.absorb_score(nets, xrs, d5),
                    "absorb_today": accum_lib.absorb_today(net_today,
                                                           ctx["xr"], adtv),
                    "softrun5": accum_lib.softrun(nets, d5),
                    "softrun20": fv["softrun20"], "stealth": score})
                # The structural hard gate from accumulation.md 4.3, applied BEFORE the
                # bucket is allowed to count. This is what makes theta_osr / theta_adtv
                # actually bite on Level 2; without it all nine grid cells returned the
                # same count.
                if not accum_lib.hard_gate(o20, w20["net"], adtv,
                                           theta_osr, theta_adtv):
                    continue
                if bucket in ("absorption", "stealth"):
                    if best is None or score > best[0]:
                        best = (score, bucket)
            if best:
                l2.append((sym, i, best[0], 0))
    return l1, l2


def hot_index(p: Panel, universe: dict, top: int = 20, lookback: int = 40) -> dict:
    """{i: [symbols]} — trailing-window membership, NOT same-day rank.

    Same-day rank would exclude the entry day by construction: stealth accumulation
    happens on quiet sessions, and BREN traded under the top-20 cut on 2026-08-11, the
    day before +13.3%.
    """
    # load_universe_membership() already returns a SET of symbols per date, filtered to
    # rank_value <= 20 at load time — not a list of row dicts.
    out = {}
    for i in range(len(p.dates)):
        seen: set[str] = set()
        for j in range(i, max(-1, i - lookback), -1):
            seen |= universe.get(p.dates[j], set())
        out[i] = sorted(seen)
    return out


def fmt(m: dict) -> str:
    if not m or not m.get("n"):
        return "      n=0"
    return f"n={m['n']:>5}  mean={m['mean_excess'] * 100:+.2f}pp  median={m['median_excess'] * 100:+.2f}pp"


def run_momentum_filter(args) -> int:
    """Does one-sidedness IMPROVE the momentum board, rather than replace it?

    The last combination the evidence has not ruled out. One-sidedness failed as a
    standalone signal (§7c: 5d lift −0.83pp, negative in all 18 grid cells). The momentum
    rule is the only accumulation rule in this repo that ever survived a walk-forward
    (+0.96pp/3d, +1.40pp/5d, 4 of 4 sub-periods, n=2,502). So: keep the validated rule and
    ask whether `osr20` sorts its events into better and worse halves.

    The event definition is copied EXACTLY from build_momentum_board.build() — same
    constants, same order, same is_momentum() call — so the split is measured on the
    population that was actually validated, not on a lookalike.

    THE DECIDING COMPARISON is high-osr against low-osr WITHIN the momentum set, not
    against the universe. A filter earns its place only by separating: if both halves
    return the same, `osr` carries no information here either, and adding it would just
    cut n for nothing.
    """
    from momentum_setup import is_momentum  # noqa: E402
    from overlay_test import features        # noqa: E402

    MIN_VALUE, MIN_ADTV_PCT = 500e6, 10.0
    RVOL_MIN, RVOL_MAX, DD_MIN, RSI_MIN = 1.5, 3.0, -0.10, 55.0

    p = Panel().load()
    g = load_gross_indexed(p)
    if not g:
        print("no gross-*.csv.gz — run backfill_gross.py first", file=sys.stderr)
        return 1
    bad = tainted_symbols()
    for s in bad:
        g.pop(s, None)

    covered = sorted({i for sym in g for b in g[sym] for i, *_ in g[sym][b]})
    lo_i, hi_i = min(covered), max(covered)
    print("MOMENTUM + ONE-SIDEDNESS — does osr improve a rule that already works?")
    print(f"  gross coverage : {len(g)} symbols, {len(covered)} sessions "
          f"({p.dates[lo_i]} .. {p.dates[hi_i]})")
    print(f"  event defn     : net>=max(Rp500m, 10% ADTV), buying 2 of last 3,")
    print(f"                   rvol5 in [1.5,3.0), dd60>=-10%, rsi>=55  "
          f"(verbatim from build_momentum_board)")
    print(f"  returns        : excess vs IHSG, entry_lag=1, k={HORIZONS}\n")

    events = []
    for (sym, broker), series in p.flows.items():
        if sym not in g or broker not in g[sym]:
            continue
        by_i = dict(series)
        for i in range(max(lo_i + 20, 20), hi_i + 1):
            if i + max(HORIZONS) + 1 >= len(p.dates):
                continue
            net = by_i.get(i)
            if net is None or net < MIN_VALUE:
                continue
            a = (p.adtv.get(sym) or {}).get(i)
            if not a or net < (MIN_ADTV_PCT / 100.0) * a:
                continue
            if sum(1 for j in range(i - 2, i + 1) if by_i.get(j, 0) > 0) < 2:
                continue
            f = features(p, sym, i)
            if not f or f.get("rvol5") is None:
                continue
            if not is_momentum(f, RVOL_MIN, DD_MIN, RSI_MIN, RVOL_MAX):
                continue
            w20 = win(g[sym][broker], i - 19, i)
            o20 = accum_lib.osr(w20["bv"], w20["sv"], a, window=20)
            events.append((sym, i, o20, broker))

    if not events:
        print("no momentum events inside the gross window", file=sys.stderr)
        return 1

    scored = [e for e in events if e[2] is not None]
    print(f"MOMENTUM EVENTS (the validated population)")
    for k in HORIZONS:
        print(f"  k={k:<3} {fmt(measure(p, events, k))}")
    base = {k: (measure(p, events, k).get("mean_excess") or 0.0) for k in HORIZONS}
    print(f"  with an osr reading: {len(scored)}/{len(events)} "
          f"({len(events) - len(scored)} below the definedness floor)\n")

    results = {}
    for th in [float(x) for x in args.theta_osr.split(",")]:
        hi = [e for e in scored if e[2] >= th]
        lo = [e for e in scored if e[2] < th]
        print(f"  osr20 >= {th:.2f}   kept {len(hi)}  dropped {len(lo)}")
        row = {"threshold": th, "n_kept": len(hi), "n_dropped": len(lo)}
        for k in HORIZONS:
            mh, ml = measure(p, hi, k), measure(p, lo, k)
            dh = ((mh["mean_excess"] - base[k]) * 100
                  if mh.get("mean_excess") is not None else None)
            sep = ((mh["mean_excess"] - ml["mean_excess"]) * 100
                   if (mh.get("mean_excess") is not None
                       and ml.get("mean_excess") is not None) else None)
            print(f"    k={k:<3} kept {fmt(mh)}"
                  + (f"  vs all momentum {dh:+.2f}pp" if dh is not None else "")
                  + (f"   separation {sep:+.2f}pp" if sep is not None else ""))
            print(f"        dropped {fmt(ml)}")
            row[k] = {"kept_mean": mh.get("mean_excess"),
                      "dropped_mean": ml.get("mean_excess"),
                      "delta_vs_all_pp": dh, "separation_pp": sep}
        results[f"{th:.2f}"] = row
        print()

    print("READING: 'separation' is kept minus dropped at the same horizon. A filter that")
    print("carries information makes it clearly positive. Near zero means osr sorts the")
    print("momentum population at random and only costs sample size.")

    out = PANEL / "accum_momentum_filter.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now(WIB).isoformat(),
        "mode": "momentum-filter",
        "coverage": {"symbols": len(g), "sessions": len(covered),
                     "from": p.dates[lo_i], "to": p.dates[hi_i]},
        "n_events": len(events), "n_with_osr": len(scored),
        "momentum_mean": base, "by_threshold": results,
    }, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


def run_gate(args) -> int:
    """The §6 protocol against real one-sidedness. This is the gate that decides whether
    the board may ever emit a trade."""
    p = Panel().load()
    universe = load_universe_membership()
    if not universe:
        print("no universe-*.csv.gz — run build_universe.py first", file=sys.stderr)
        return 1
    g = load_gross_indexed(p)
    if not g:
        print("no gross-*.csv.gz — run backfill_gross.py first", file=sys.stderr)
        return 1
    bad = tainted_symbols()
    for s in bad:
        g.pop(s, None)

    alpha = {}
    ap_path = PANEL / "broker_alpha.json"
    if ap_path.exists():
        try:
            raw = json.loads(ap_path.read_text(encoding="utf-8"))
            ranked = raw.get("ranked") or raw.get("brokers") or []
            alpha = {r["broker"]: {"rank": n + 1}
                     for n, r in enumerate(ranked) if r.get("broker")}
        except Exception:
            alpha = {}

    hot = hot_index(p, universe)
    covered = sorted({p.dates[i] for sym in g for b in g[sym] for i, *_ in g[sym][b]})
    print("GATE TEST — one-sidedness (osr), the only untested claim left")
    print(f"  gross coverage : {len(g)} symbols, {len(covered)} sessions "
          f"({covered[0]} .. {covered[-1]})")
    if bad:
        print(f"  excluded       : {', '.join(sorted(bad))} (unadjusted corporate action)")
    print(f"  returns        : excess vs IHSG, entry_lag=1, k={HORIZONS}")

    # Matched baseline restricted to the sessions the gross partition actually covers,
    # otherwise the board's events and the baseline are measured over different regimes.
    lo_i = p.didx[covered[0]] + 20
    hi_i = p.didx[covered[-1]]
    base = [(sym, i, 0.0, 0) for i in range(lo_i, hi_i + 1)
            for sym in hot.get(i, [])
            if sym in g and (p.adtv.get(sym) or {}).get(i)
            and i + max(HORIZONS) + 1 < len(p.dates)]
    print(f"\nMATCHED BASELINE (top-20 universe, same sessions)")
    for k in HORIZONS:
        print(f"  k={k:<3} {fmt(measure(p, base, k))}")
    bm = {k: (measure(p, base, k).get("mean_excess") or 0.0) for k in HORIZONS}

    grid_osr = [float(x) for x in args.theta_osr.split(",")]
    grid_adtv = [float(x) / 100.0 for x in args.theta_adtv.split(",")]
    results, cells_pos = {}, 0

    for mode in ("today", "window"):
        print(f"\n{'=' * 74}\nabsorb_mode = {mode}\n{'=' * 74}")
        for to in grid_osr:
            for ta in grid_adtv:
                l1, l2 = gate_events(p, g, universe, hot, to, ta, mode, True, alpha)
                m5 = measure(p, l2, 5)
                lift5 = ((m5["mean_excess"] - bm[5]) * 100
                         if m5.get("mean_excess") is not None else None)
                if lift5 is not None and lift5 > 0 and mode == "today":
                    cells_pos += 1
                tag = f"osr>={to:.2f} adtv>={ta * 100:.0f}%"
                print(f"  {tag:<22} L1 n={len(l1):>5}  L2 n={len(l2):>4}  "
                      + (f"5d lift={lift5:+.2f}pp" if lift5 is not None else "5d n/a"))
                results[f"{mode}|{to}|{ta}"] = {
                    "n_l1": len(l1), "n_l2": len(l2),
                    "lift5": lift5, "mean5": m5.get("mean_excess")}

    # ---- the declared design point, in full
    to, ta = 0.80, 0.20
    l1, l2 = gate_events(p, g, universe, hot, to, ta, "today", True, alpha)
    print(f"\n{'=' * 74}\nDESIGN POINT osr>=0.80, net>=20% ADTV, absorb_mode=today"
          f"\n{'=' * 74}")
    for k in HORIZONS:
        m = measure(p, l2, k)
        lift = ((m["mean_excess"] - bm[k]) * 100
                if m.get("mean_excess") is not None else None)
        print(f"  L2 k={k:<3} {fmt(m)}"
              + (f"   lift={lift:+.2f}pp" if lift is not None else ""))
    folds = by_fold(p, l2, 5, lo_i=lo_i, hi_i=hi_i)
    bfolds = by_fold(p, base, 5, lo_i=lo_i, hi_i=hi_i)
    bits, pos = [], 0
    for fo, bf in zip(folds, bfolds):
        if fo.get("mean_excess") is None or bf.get("mean_excess") is None:
            bits.append("   n/a")
        else:
            d = (fo["mean_excess"] - bf["mean_excess"]) * 100
            bits.append(f"{d:+6.2f}")
            pos += d > 0
    print(f"  k=5 sub-periods: {' '.join(bits)}    {pos}/{FOLDS} positive")

    # ---- no-tilt comparison (declared: drop the tilt if it does not help)
    _, l2_nt = gate_events(p, g, universe, hot, to, ta, "today", False, alpha)
    m_t, m_nt = measure(p, l2, 5), measure(p, l2_nt, 5)
    print(f"  tilt on : {fmt(m_t)}")
    print(f"  tilt off: {fmt(m_nt)}")

    # ---- nulls
    rng = random.Random(SEED)
    print(f"\nNULL CONTROLS (k=5, design point)")
    # Null 1: broker labels shuffled within each symbol.
    g1 = {}
    for sym, bs in g.items():
        codes = list(bs)
        sh = codes[:]
        rng.shuffle(sh)
        g1[sym] = {new: bs[old] for old, new in zip(codes, sh)}
    # Null 2: circular date shift per (symbol, broker) — preserves autocorrelation and
    # the return distribution while destroying flow-to-price alignment. The strict one.
    n_d = len(p.dates)
    g2 = {}
    for sym, bs in g.items():
        g2[sym] = {}
        for code, series in bs.items():
            off = rng.randrange(n_d)
            g2[sym][code] = sorted(((i + off) % n_d, bv, sv, bf, ba)
                                   for i, bv, sv, bf, ba in series)
    for label, gg in (("broker-label shuffle", g1), ("date shift (strict)", g2)):
        _, nl2 = gate_events(p, gg, universe, hot, to, ta, "today", True, alpha)
        m = measure(p, nl2, 5)
        lift = ((m["mean_excess"] - bm[5]) * 100
                if m.get("mean_excess") is not None else None)
        flag = ("   <-- EXCEEDS +-0.3pp: harness leak"
                if lift is not None and abs(lift) > 0.30 else "")
        print(f"  {label:<24} {fmt(m)}"
              + (f"   lift={lift:+.2f}pp{flag}" if lift is not None else ""))
    # Null 3: universe-only — same (sym, day) count drawn at random from the universe.
    rng3 = random.Random(SEED + 1)
    sample = rng3.sample(base, min(len(l2), len(base))) if base else []
    m3 = measure(p, sample, 5)
    l3 = ((m3["mean_excess"] - bm[5]) * 100
          if m3.get("mean_excess") is not None else None)
    print(f"  {'universe-only random':<24} {fmt(m3)}"
          + (f"   lift={l3:+.2f}pp" if l3 is not None else ""))

    # ---- anchoring check: in-sample motivation, never confirmation
    print(f"\nANCHORING (in-sample motivation, NOT evidence)")
    horizon_cut = len(p.dates) - max(HORIZONS) - 1
    for sym, d in (("BREN", "2026-08-11"), ("PTRO", "2026-08-11"),
                   ("CUAN", "2026-08-11"), ("DSSA", "2026-08-05")):
        j = p.didx.get(d)
        if j is None:
            print(f"  {sym} {d}: not a session in the panel")
        elif j >= horizon_cut:
            # These names moved inside the last two weeks, so a k=10 forward return does
            # not exist yet and the harness excludes the day BY DESIGN. Printing "no"
            # here reads as "the rule missed it", which is a different and false claim.
            print(f"  {sym} {d}: excluded — no {max(HORIZONS)}d forward return yet "
                  f"(panel ends {p.dates[-1]})")
        else:
            hit = any(s2 == sym and i2 == j for s2, i2, _, _ in l2)
            print(f"  {sym} {d}: {'FIRES' if hit else 'no'}")

    # ---- verdict against the declared bar
    m5 = measure(p, l2, 5)
    m3h, m10 = measure(p, l2, 3), measure(p, l2, 10)
    lift5 = ((m5["mean_excess"] - bm[5]) * 100
             if m5.get("mean_excess") is not None else None)
    lift3 = ((m3h["mean_excess"] - bm[3]) * 100
             if m3h.get("mean_excess") is not None else None)
    lift10 = ((m10["mean_excess"] - bm[10]) * 100
              if m10.get("mean_excess") is not None else None)
    print(f"\n{'=' * 74}\nVERDICT against the bar declared in accumulation.md 6.6"
          f"\n{'=' * 74}")
    checks = [
        ("1. L2 5d lift >= +1.2pp", lift5 is not None and lift5 >= 1.2,
         f"{lift5:+.2f}pp" if lift5 is not None else "n/a"),
        ("2. 5d positive in >=3 of 4 sub-periods", pos >= 3, f"{pos}/4"),
        ("3. 10d lift >= 3d lift",
         lift10 is not None and lift3 is not None and lift10 >= lift3,
         f"10d {lift10:+.2f} vs 3d {lift3:+.2f}"
         if (lift10 is not None and lift3 is not None) else "n/a"),
        ("5. >=7 of 9 grid cells positive", cells_pos >= 7, f"{cells_pos}/9"),
        ("n. L2 n >= 250", m5.get("n", 0) >= 250, f"n={m5.get('n', 0)}"),
    ]
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<42} {detail}")
    passed = all(ok for _, ok, _ in checks)
    print(f"\n  {'GATE PASSED' if passed else 'GATE NOT PASSED'} — "
          + ("trade-plan integration may proceed"
             if passed else "board stays observation-mode, no trade signals"))

    out = PANEL / "accum_gate_test.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now(WIB).isoformat(), "mode": "gate",
        "coverage": {"symbols": len(g), "sessions": len(covered),
                     "from": covered[0], "to": covered[-1]},
        "baseline_mean": bm, "grid": results,
        "design_point": {"theta_osr": to, "theta_adtv": ta,
                         "n_l1": len(l1), "n_l2": len(l2),
                         "lift3": lift3, "lift5": lift5, "lift10": lift10,
                         "folds_positive": pos},
        "passed": passed,
        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
    }, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0 if passed else 3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="coalition",
                    choices=("coalition", "gate", "momentum-filter"))
    ap.add_argument("--theta-adtv", default="10,20,40",
                    help="size floor as %% of ADTV; the declared grid")
    ap.add_argument("--theta-osr", default="0.70,0.80,0.90",
                    help="one-sidedness floor; the declared grid (gate mode)")
    ap.add_argument("--window", type=int, default=20)
    args = ap.parse_args()

    if args.mode == "gate":
        return run_gate(args)
    if args.mode == "momentum-filter":
        return run_momentum_filter(args)

    p = Panel().load()
    universe = load_universe_membership()
    if not universe:
        print("no universe-*.csv.gz — run build_universe.py first", file=sys.stderr)
        return 1
    bad = tainted_symbols()
    if bad:
        universe = {d: (s - bad) for d, s in universe.items()}
        print(f"  EXCLUDED (unadjusted corporate action): {', '.join(sorted(bad))}")
    flows = broker_series(p)

    print(f"COALITION A/B — single-broker floor vs coalition sum")
    print(f"  panel     : {len(p.turnover)} symbols, {len(p.dates)} sessions "
          f"({p.dates[0]} .. {p.dates[-1]})")
    print(f"  universe  : {len(universe)} dated top-20 sets")
    print(f"  filter    : softrun{args.window} >= {SOFTRUN_MIN} (PROXY for osr — see "
          f"module docstring)")
    print(f"  floor     : max(Rp10bn, theta x ADTV20)")
    print(f"  returns   : excess vs IHSG, entry_lag=1, k={HORIZONS}\n")

    base = baseline_events(p, universe, args.window)
    print("MATCHED BASELINE (every top-20 name, every session)")
    for k in HORIZONS:
        print(f"  k={k:<3} {fmt(measure(p, base, k))}")
    base_mean = {k: (measure(p, base, k).get("mean_excess") or 0.0) for k in HORIZONS}

    results = {}
    for theta_pct in [float(x) for x in args.theta_adtv.split(",")]:
        theta = theta_pct / 100.0
        ev = build_events(p, flows, universe, theta, args.window)
        print(f"\n{'=' * 74}\ntheta_adtv = {theta_pct:.0f}%   "
              f"single={len(ev['single']):,}  coalition={len(ev['coalition']):,}  "
              f"coalition-only={len(ev['coalition_only']):,}\n{'=' * 74}")
        row = {"theta_pct": theta_pct,
               "n": {k: len(v) for k, v in ev.items()}}
        for k in HORIZONS:
            print(f"  k={k}")
            for name in ("single", "coalition", "coalition_only"):
                m = measure(p, ev[name], k)
                lift = ((m["mean_excess"] - base_mean[k]) * 100) if m.get("mean_excess") is not None \
                    else None
                print(f"    {name:<16} {fmt(m)}"
                      + (f"   lift={lift:+.2f}pp" if lift is not None else ""))
                row.setdefault(name, {})[k] = {"n": m.get("n"), "mean": m.get("mean_excess"),
                                               "lift_pp": lift}
        # Sub-period stability at the decision horizon.
        print(f"  k=5 by sub-period (lift over matched baseline):")
        for name in ("single", "coalition", "coalition_only"):
            folds = by_fold(p, ev[name], 5)
            bfolds = by_fold(p, base, 5)
            bits = []
            for f, bf in zip(folds, bfolds):
                if f.get("mean_excess") is None or bf.get("mean_excess") is None:
                    bits.append("   n/a")
                else:
                    bits.append(f"{(f['mean_excess'] - bf['mean_excess']) * 100:+6.2f}")
            pos = sum(1 for f, bf in zip(folds, bfolds)
                      if f.get("mean_excess") is not None and bf.get("mean_excess") is not None
                      and f["mean_excess"] > bf["mean_excess"])
            print(f"    {name:<16} {' '.join(bits)}    {pos}/{FOLDS} positive")
            row.setdefault(name, {})["folds_positive"] = pos
        results[f"{theta_pct:.0f}"] = row

    # ---- nulls, at the declared design point
    theta = 0.20
    rng = random.Random(SEED)
    print(f"\n{'=' * 74}\nNULL CONTROLS at theta=20%, k=5\n{'=' * 74}")
    for label, nf in (("broker-label shuffle", shuffle_broker_labels(flows, rng)),
                      ("date shift (the strict one)",
                       shift_dates(flows, rng, len(p.dates)))):
        nev = build_events(p, nf, universe, theta, args.window)
        for name in ("single", "coalition"):
            m = measure(p, nev[name], 5)
            lift = ((m["mean_excess"] - base_mean[5]) * 100) if m.get("mean_excess") is not None else None
            flag = ""
            if lift is not None and abs(lift) > 0.30:
                flag = "   <-- EXCEEDS +-0.3pp: harness leak"
            print(f"  {label:<28} {name:<11} {fmt(m)}"
                  + (f"   lift={lift:+.2f}pp{flag}" if lift is not None else ""))

    out = PANEL / "accum_coalition_test.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now(WIB).isoformat(),
        "mode": "coalition", "proxy": f"softrun{args.window}>={SOFTRUN_MIN}",
        "baseline_mean": base_mean, "results": results,
    }, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
