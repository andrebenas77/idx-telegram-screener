#!/usr/bin/env python3
"""Thesis #13 -- the measurement cascade. Framework: reference/flow-direction.md section 5.

FIVE GATES, ZERO REQUESTS. Everything here reads data already on disk: the 41 cached
true-aggressor tapes, the m5 and h60 bar stores, and the daily panel. Nothing is fetched, so
this can be run as often as one likes and it cannot cost anything.

The order is deliberate and is the whole design:

  Gate 0  how much resolution the clock actually has, and the ceiling that implies
  Gate 1  is the statistic RELIABLE  -- estimated on ~12,000 symbol-days, SE about 0.01
  Gate 2  does it BEAT TWO PRICES    -- estimated on 29 tapes, n_eff about 20
  Gate 3  does the desk RATE OF CHANGE exist at all
  Gate 4  estimand hygiene: the unsigned arm is a different object and is scored separately

Reliability runs before the tape validation because it is the precise measurement. Screening a
noisy instrument out on 12,000 rows costs nothing; screening it out on 29 rows is a coin flip.

A FAILURE HERE IS THE EXPECTED OUTCOME, not an error. Twelve flow theses have been run in this
repo and none has shipped. Gate 3 in particular is pre-registered as expected-to-fail. The script
exits non-zero on any failure so that `build_flow_board.py` cannot be run on a dead cascade by
accident, and the board that does ship is labelled with whatever this wrote.

Usage:
    py scripts/flowdir_measure.py --json ../data/panel/flowdir_measure.json
    py scripts/flowdir_measure.py --max-symbols 20        # quick pass while iterating
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import flowdir_lib as F  # noqa: E402
import intraday_lib as il  # noqa: E402
import tape_lib as tl  # noqa: E402
import vpin_validate as VV  # noqa: E402
from alpha_lib import PANEL, Panel, panel_fingerprint  # noqa: E402
from vpin_lib import realized_vol, spearman  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TAPE = ROOT / "data" / "tape"
INTRADAY = ROOT / "data" / "intraday"

BUCKETS_PER_DAY = 24        # the vpin_validate convention, kept so TRUE is comparable to V0

# Pass bars, from reference/flow-direction.md section 5. Declared there before this file existed.
BAR_RELIABILITY = 0.30
BAR_UNSIGNED_RHO = 0.40
BAR_ROC_LAG1 = 0.15
BAR_ROC_ACF_DEPARTURE = 0.10


# --------------------------------------------------------------------------- ground truth

def load_cached_tape(path: Path):
    """Raw rows from a cached tape. Both stored formats, zero requests.

    `invezgo_client.running_trade_all` reads only the .json.gz form and would need a configured
    key to construct. This reads the store directly and hands the rows to `tape_lib.parse_prints`,
    so the NORMALISATION (aggressor, board, share volume) still has exactly one definition.
    """
    if path.name.endswith(".json.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, list) else (raw.get("data") or raw.get("rows") or [])
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def true_daily(prints, nb: int = BUCKETS_PER_DAY):
    """(unsigned, signed) aggressor imbalance over `nb` equal-volume buckets. RG only.

    Unsigned is the VPIN object thesis #8 measured; signed is the one this study is about. Both
    are computed from the SAME clock so the two arms cannot be compared across different
    bucketings by accident.

    NG is excluded: negotiated volume is pre-arranged rather than liquidity taken from a book, so
    one crossed block would enter as maximal one-sidedness on the least informative trade of the
    session.
    """
    rg = [p for p in prints if p.get("board") == "RG" and p.get("volume", 0) > 0
          and p.get("aggressor") in ("BUY", "SELL")]
    tot = sum(p["volume"] for p in rg)
    if tot <= 0:
        return None, None
    size = tot / nb
    buckets, cur, filled = [], [], 0.0
    for p in rg:
        rem = p["volume"]
        while rem > 0:
            take = min(size - filled, rem)
            cur.append((p["aggressor"], take))
            filled += take
            rem -= take
            if filled >= size - 1e-9:
                buckets.append(cur)
                cur, filled = [], 0.0
    uns, sgn = [], []
    for b in buckets[:nb]:                      # drop the partial tail
        buy = sum(v for a, v in b if a == "BUY")
        sell = sum(v for a, v in b if a == "SELL")
        t = buy + sell
        if t > 0:
            uns.append(abs(buy - sell) / t)
            sgn.append((buy - sell) / t)
    return ((statistics.fmean(uns) if uns else None),
            (statistics.fmean(sgn) if sgn else None))


def collect_tapes(freq):
    """Every cached ticker-day that survives the truncation guard, with its bar-derived arms.

    The guard is `vpin_validate.truncated`, reused rather than reimplemented. It matters more
    here than anywhere: a truncated tape covers only the open, where flow is most one-sided, so
    it scores as maximally directional. Including three of them once dragged a correlation in
    this repo from +0.559 to +0.210.
    """
    rows, dropped = [], []
    for p in sorted(TAPE.glob("*/*.gz")):
        stem = p.name.replace(".json.gz", "").replace(".csv.gz", "")
        parts = stem.split("-")
        sym, date = parts[0], "-".join(parts[1:4])
        prints = tl.parse_prints(load_cached_tape(p))
        if not prints:
            dropped.append([sym, date, "no usable prints"])
            continue
        expected = freq.get((sym, date))
        if VV.truncated(prints, expected or 0):
            dropped.append([sym, date, "TRUNCATED %d prints vs ~%.0f expected"
                            % (len(prints), expected or 0)])
            continue
        uns, sgn = true_daily(prints)
        if uns is None or sgn is None:
            dropped.append([sym, date, "no RG buckets"])
            continue
        # Integrity: our clock must reproduce the one V0 used, or TRUE is not the same object.
        vv_uns, _ = VV.tr_daily(prints)
        if vv_uns is None or abs(vv_uns - uns) > 1e-9:
            dropped.append([sym, date, "clock disagrees with vpin_validate.tr_daily"])
            continue
        rows.append({"sym": sym, "date": date, "true_unsigned": uns, "true_signed": sgn,
                     "n_prints": len(prints)})
    return rows, dropped


# --------------------------------------------------------------------------- bar arms

def day_bars(store: dict, date: str):
    return F.continuous(store.get(date) or [])


def bar_arms(bars, seas):
    """Every bar-derived statistic for one session, scored on the same bars.

    Rivals are computed here beside the instrument rather than in a separate pass, so there is no
    way for the instrument to be evaluated on a different sample than the thing it must beat.
    """
    if len(bars) < 3:
        return None
    clvs = [F.clv(b.h, b.l, b.c) for b in bars]
    usable = [c for c in clvs if c is not None]
    if not usable:
        return None
    o, c = bars[0].o, bars[-1].c
    ret = (c / o - 1.0) if o and o > 0 else None
    return {
        "sflow": F.sflow(bars),
        "sflow_seas": F.sflow_seasonal(bars, seas) if seas else None,
        "sflow_equal": F.sflow_equal(bars),
        "ret": ret,
        "ret_sign": (0.0 if ret is None else (1.0 if ret > 0 else (-1.0 if ret < 0 else 0.0))),
        "unsigned": (statistics.fmean([abs(2 * x - 1) for x in usable])),
        "eff_bars": F.effective_bars(bars),
        "sum_w2": (1.0 / F.effective_bars(bars)) if F.effective_bars(bars) else None,
        "var_bar": (statistics.pvariance([2 * x - 1 for x in usable])
                    if len(usable) > 1 else None),
        "rvol": realized_vol([b.c for b in bars]),
    }


def series_for(sym: str, prefix: str, seasonal: bool):
    """Daily (date, sflow) for one symbol, optionally seasonally normalised.

    The seasonal is rebuilt at every session from the previous 60 sessions ONLY. Full-sample
    per-bucket medians are a look-ahead, and thesis #12 lost its instrument to exactly that
    (rho +0.512 -> +0.384 once it was removed).
    """
    path = INTRADAY / ("%s-%s.csv.gz" % (prefix, sym))
    if not path.exists():
        return [], []
    store = {d: F.continuous(v) for d, v in _read_store(prefix, sym).items()}
    dates = sorted(d for d, v in store.items() if len(v) >= 3)
    out = []
    for k, d in enumerate(dates):
        if seasonal:
            prior = dates[max(0, k - F.SEAS_WIN):k]
            if len(prior) < 20:
                out.append(None)
                continue
            seas = F.trailing_seasonal(store, prior)
            out.append(F.sflow_seasonal(store[d], seas))
        else:
            out.append(F.sflow(store[d]))
    return dates, out


_STORE_CACHE: dict = {}


def _read_store(prefix: str, sym: str):
    key = (prefix, sym)
    if key not in _STORE_CACHE:
        if prefix == "m5":
            _STORE_CACHE[key] = il.read_bars(sym)
        else:
            _STORE_CACHE[key] = _read_prefixed(prefix, sym)
    return _STORE_CACHE[key]


def _read_prefixed(prefix: str, sym: str):
    """read_bars for a store other than m5. `intraday_lib.bars_path` is hardwired to the m5
    prefix, so the h60 comparison arm needs its own reader; the Bar shape is identical."""
    p = INTRADAY / ("%s-%s.csv.gz" % (prefix, sym))
    if not p.exists():
        return {}
    days: dict = {}
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                b = il.Bar(r["date"], r["hhmm"], float(r["open"]), float(r["high"]),
                           float(r["low"]), float(r["close"]), float(r["volume"] or 0))
            except (TypeError, ValueError, KeyError):
                continue
            if b.hhmm < il.OPEN_HHMM:
                continue
            days.setdefault(b.date, []).append(b)
    return {d: sorted(v, key=lambda x: x.hhmm) for d, v in days.items()}


def store_symbols(prefix: str):
    out = []
    for f in sorted(glob.glob(str(INTRADAY / ("%s-*.csv.gz" % prefix)))):
        s = os.path.basename(f)[len(prefix) + 1:-7]
        if s != "COMPOSITE":
            out.append(s)
    return out


# --------------------------------------------------------------------------- gates

def gate0(tapes, syms):
    """Resolution: effective bars per session and the ceiling that implies. Reported, not gating.

    The ceiling assumes each bar carries the session value plus independent noise. Every
    instrument here EXCEEDS it, which is the finding: the estimator and the truth share a channel
    the model does not contain, and on a price-derived statistic that channel is the price path.
    Gate 2 is what settles whether the instrument is anything more than that channel.
    """
    sd_truth = statistics.pstdev([t["true_signed"] for t in tapes]) if len(tapes) > 2 else None
    out = {"sd_true_signed": sd_truth, "clocks": {}}
    for prefix in ("m5", "h60"):
        w2, vb, eb = [], [], []
        for s in syms:
            store = _read_store(prefix, s)
            for d, raw in store.items():
                bars = F.continuous(raw)
                a = bar_arms(bars, None)
                if not a or a["sum_w2"] is None or a["var_bar"] is None:
                    continue
                w2.append(a["sum_w2"])
                vb.append(a["var_bar"])
                eb.append(a["eff_bars"])
        if not w2:
            continue
        mw2, mvb = statistics.fmean(w2), statistics.fmean(vb)
        out["clocks"][prefix] = {
            "n_sessions": len(w2), "mean_sum_w2": mw2,
            "effective_bars": statistics.fmean(eb), "var_bar": mvb,
            "ceiling_vs_true_signed": (F.ceiling_vs_truth(mw2, sd_truth, mvb)
                                       if sd_truth else None)}
    return out


def gate1(syms):
    """Split-half reliability of sflow, odd bars against even bars, Spearman-Brown corrected.

    Reported two ways and GATED ON THE WITHIN-SYMBOL number, which is the conservative one and
    the one that matches how the statistic is actually used: `sflow_rel` is a within-symbol
    sorter, so pooled reliability -- which is inflated by between-symbol differences in bar shape
    -- would flatter it. Being stricter than the pre-registration allows is safe; the reverse
    would not be.
    """
    pooled_a, pooled_b, within = [], [], []
    for s in syms:
        a_s, b_s = [], []
        for d, raw in _read_store("m5", s).items():
            bars = F.continuous(raw)
            if len(bars) < 10:
                continue
            a, b = F.split_half(bars)
            if a is None or b is None:
                continue
            a_s.append(a)
            b_s.append(b)
        if len(a_s) >= 30:
            r = spearman(a_s, b_s)
            if r is not None:
                within.append(r)
        pooled_a += a_s
        pooled_b += b_s
    r_pool = spearman(pooled_a, pooled_b)
    r_within = statistics.fmean(within) if within else None
    sb_pool = F.spearman_brown(r_pool)
    sb_within = F.spearman_brown(r_within)
    # The board does not sort on a single session -- it sorts on sflow5, a 5-session mean, and
    # then on sflow_rel. Spearman-Brown lengthens reliability the same way it corrects a
    # half-length split: r_k = k*r / (1 + (k-1)*r). The daily figure alone understates what the
    # sorter carries; the lengthened one alone overstates what one session tells you. Both are
    # recorded and the GATE stays on the daily number that was pre-registered.
    kk = F.SMOOTH_K
    sb5 = (kk * sb_within / (1 + (kk - 1) * sb_within)) if sb_within and sb_within > 0 else None
    return {"n_symbol_days": len(pooled_a), "n_symbols": len(within),
            "half_pooled": r_pool, "half_within": r_within,
            "reliability_pooled": sb_pool, "reliability_within": sb_within,
            "reliability_sflow5_implied": sb5,
            "attenuation_ceiling_within": (math.sqrt(sb_within)
                                           if sb_within and sb_within > 0 else None),
            "attenuation_ceiling_sflow5": (math.sqrt(sb5) if sb5 and sb5 > 0 else None),
            "bar": BAR_RELIABILITY,
            "ok": bool(sb_within is not None and sb_within >= BAR_RELIABILITY)}


def gate2(tapes):
    """Does the instrument beat the best FREE PRICE-ONLY rival at measuring true signed flow?

    The statistic is the INCREMENT, not the level, because Gate 0 showed every arm sits above its
    own information ceiling -- so a high correlation may be nothing but the price path, which two
    numbers off the daily bar give away for free.

    SPECIFICATION ERROR, RECORDED RATHER THAN EDITED AWAY (2026-08-29). The pre-registration
    listed `sflow_seas` among the Gate 2 rivals while section 4 of the SAME document mandates the
    trailing seasonal as the instrument normalisation. Both cannot hold: the seasonally normalised
    series is a member of the instrument family, not a rival to it. Measured, it scores higher
    than the plain form, so the primary as written fails on the instrument losing to a better
    version of ITSELF -- which says nothing about the price-path question this gate exists to
    answer. Requiring it as a rival also silently shrank the sample from 29 tapes to 21, because
    the trailing seasonal needs 20 prior sessions and several tapes sit near the start of the m5
    store.

    Both readings are computed and both are reported, so the error stays visible in the payload
    rather than only in prose. The GATING statistic is the increment over price-only rivals.

    The max over rivals is recomputed INSIDE every bootstrap draw: taking it once on the full
    sample and bootstrapping only the instrument would ignore that the max is itself a statistic
    and would report a band too narrow in the direction of a pass.

    Clusters are calendar DATES -- 2026-08-05 contributed nine tapes, so treating rows as
    independent would overstate precision by roughly a factor of two.
    """
    PRICE_RIVALS = ["ret", "ret_sign", "sflow_equal"]
    REGISTERED_RIVALS = PRICE_RIVALS + ["sflow_seas"]

    def arm(sample, key):
        xs, ys = [], []
        for r in sample:
            v = r["m5"].get(key)
            if v is not None:
                xs.append(v)
                ys.append(r["true_signed"])
        return spearman(xs, ys) if len(xs) > 3 else None

    def run(instrument, rivals, label):
        rows = [t for t in tapes
                if t.get("m5") and t["m5"].get(instrument) is not None
                and all(t["m5"].get(r) is not None for r in rivals)]
        if len(rows) < 8:
            return {"label": label, "n": len(rows), "ok": False,
                    "reason": "too few tapes carrying a full rival set"}

        def increment(sample):
            a_ = arm(sample, instrument)
            if a_ is None:
                return None
            best = None
            for k in rivals:
                v = arm(sample, k)
                if v is not None and (best is None or v > best):
                    best = v
            return None if best is None else a_ - best

        band = F.cluster_bootstrap(rows, [r["date"] for r in rows], increment)
        return {"label": label, "instrument": instrument, "rivals": rivals,
                "n": len(rows), "n_dates": len({r["date"] for r in rows}),
                "n_effective": F.effective_n([r["date"] for r in rows]),
                "rho_instrument": arm(rows, instrument),
                "rho_rivals": {k: arm(rows, k) for k in rivals},
                "increment": band["point"], "lo10": band.get("lo"), "hi90": band.get("hi"),
                "ok": bool(band.get("lo") is not None and band["lo"] > 0)}

    as_registered = run("sflow", REGISTERED_RIVALS, "as pre-registered: sflow vs 4 rivals")
    gating = run("sflow_seas", PRICE_RIVALS, "GATING: sflow_seas vs price-only rivals")
    plain = run("sflow", PRICE_RIVALS, "sflow vs price-only rivals, full sample")

    h60 = [t for t in tapes if t.get("h60") and t["h60"].get("sflow") is not None]
    rho_h60 = (spearman([t["h60"]["sflow"] for t in h60],
                        [t["true_signed"] for t in h60]) if len(h60) > 3 else None)

    return {"as_registered": as_registered, "gating": gating, "plain_vs_price": plain,
            "rho_h60_sflow_for_contrast": rho_h60, "n_h60": len(h60),
            "specification_error": ("sflow_seas was pre-registered as a rival while section 4 "
                                    "mandates it as the instrument normalisation; the gating "
                                    "reading uses price-only rivals and both are reported"),
            "ok": bool(gating.get("ok"))}


def gate3(syms, max_symbols: int):
    """Does the desk RATE OF CHANGE exist -- is there any memory in the level to differentiate?

    Two conditions, both required, because either alone is gameable:

      * lag-1 autocorrelation of the daily level >= +0.15. A white level has nothing to build.
      * the ROC's own autocorrelation must depart from `white_noise_roc_acf` by >= 0.10 at some
        lag <= 5. Differencing a smoothed white series produces strong autocorrelation entirely
        mechanically (+0.70 +0.40 +0.10 -0.20 -0.50); a measured ACF that MATCHES those numbers
        is evidence of no dynamics, and reading it as "flow builds then fades" is the trap.

    Run on the seasonally-normalised series, which is the one the board would sort on.
    """
    lags = defaultdict(list)
    roc_lags = defaultdict(list)
    coll = []
    n_used = 0
    for s in syms[:max_symbols]:
        dates, ser = series_for(s, "m5", seasonal=True)
        clean = [x for x in ser if x is not None]
        if len(clean) < 40:
            continue
        n_used += 1
        for L in range(1, 6):
            v = F.acf(ser, L)
            if v is not None:
                lags[L].append(v)
        lv = [F.mean_k(ser, i) for i in range(len(ser))]
        rc = [F.roc(lv, i) for i in range(len(lv))]
        for L in range(1, 6):
            v = F.acf(rc, L)
            if v is not None:
                roc_lags[L].append(v)
        pairs = [(a, b) for a, b in zip(rc, lv) if a is not None and b is not None]
        if len(pairs) >= 20:
            v = spearman([a for a, _ in pairs], [b for _, b in pairs])
            if v is not None:
                coll.append(v)
    if not lags:
        return {"ok": False, "reason": "no symbol had enough seasonally-normalised sessions"}
    level_acf = [statistics.fmean(lags[L]) for L in range(1, 6)]
    roc_acf = [statistics.fmean(roc_lags[L]) if roc_lags[L] else None for L in range(1, 6)]
    theory = F.white_noise_roc_acf(5)
    dep = [abs(a - b) for a, b in zip(roc_acf, theory) if a is not None]
    return {"n_symbols": n_used, "level_acf_lag1_5": level_acf,
            "roc_acf_lag1_5": roc_acf, "white_noise_roc_acf": theory,
            "max_departure_from_white": (max(dep) if dep else None),
            "corr_roc_level": (statistics.fmean(coll) if coll else None),
            "corr_roc_level_identity_if_white": 1.0 / math.sqrt(2),
            "bar_lag1": BAR_ROC_LAG1, "bar_departure": BAR_ROC_ACF_DEPARTURE,
            "ok": bool(level_acf[0] >= BAR_ROC_LAG1
                       and dep and max(dep) >= BAR_ROC_ACF_DEPARTURE)}


def gate4(tapes):
    """Estimand hygiene. The unsigned arm is a DIFFERENT object and is scored separately.

    Thesis #8's surviving conclusions -- that a cheap proxy should be the tick rule, and that true
    toxicity runs NEGATIVE against realized volatility at -0.483 -- attach to the unsigned object.
    Carrying them across to a signed study would be borrowing an endorsement the signed statistic
    never earned. So: if the unsigned arm cannot both track unsigned truth and reproduce the
    negative volatility relation, no unsigned column ships and no thesis-#8 conclusion is cited.
    """
    rows = [t for t in tapes if t.get("m5") and t["m5"].get("unsigned") is not None]
    if len(rows) < 8:
        return {"n": len(rows), "ok": False, "reason": "too few tapes"}
    xs = [r["m5"]["unsigned"] for r in rows]
    r_true = spearman(xs, [r["true_unsigned"] for r in rows])
    rv = [(r["m5"]["unsigned"], r["m5"]["rvol"]) for r in rows if r["m5"].get("rvol") is not None]
    r_vol = spearman([a for a, _ in rv], [b for _, b in rv]) if len(rv) > 3 else None
    true_vol = spearman([r["true_unsigned"] for r in rows if r["m5"].get("rvol") is not None],
                        [r["m5"]["rvol"] for r in rows if r["m5"].get("rvol") is not None])
    return {"n": len(rows), "rho_unsigned_vs_true": r_true,
            "rho_unsigned_vs_realized_vol": r_vol,
            "rho_true_vs_realized_vol": true_vol,
            "thesis8_reference": -0.483,
            "bar_rho": BAR_UNSIGNED_RHO,
            "ok": bool(r_true is not None and r_true >= BAR_UNSIGNED_RHO
                       and r_vol is not None and r_vol < 0)}


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("--max-symbols", type=int, default=10**6)
    a = ap.parse_args()

    p = Panel()
    p.load_prices()          # load_prices mutates in place and returns None; load() returns self
    print("panel: %d symbols" % len(p.close))
    freq = VV.load_freq(p)
    print("gross panel ticker-days (for the truncation guard): %d" % len(freq))

    tapes, dropped = collect_tapes(freq)
    print("\ntapes: %d usable, %d rejected" % (len(tapes), len(dropped)))
    for d in dropped:
        print("   DROP %-6s %s  %s" % (d[0], d[1], d[2]))

    syms = store_symbols("m5")[:a.max_symbols]
    print("\nm5 store: %d symbols" % len(syms))

    # attach the bar arms for every tape day, on both clocks
    for t in tapes:
        for prefix in ("m5", "h60"):
            store = _read_store(prefix, t["sym"])
            dates = sorted(d for d in store)
            if t["date"] not in store:
                continue
            seas = None
            if prefix == "m5":
                k = dates.index(t["date"])
                prior = dates[max(0, k - F.SEAS_WIN):k]
                cont = {d: F.continuous(store[d]) for d in prior}
                if len(prior) >= 20:
                    seas = F.trailing_seasonal(cont, prior)
            t[prefix] = bar_arms(day_bars(store, t["date"]), seas)

    print("\n" + "=" * 78)
    g0 = gate0(tapes, syms)
    print("GATE 0 -- resolution ceiling (reported, not gating)")
    print("  sd(TRUE signed) = %.4f" % (g0["sd_true_signed"] or 0))
    for k, v in g0["clocks"].items():
        print("  %-4s %6d sessions | %5.2f effective bars | var(2clv-1) %.3f | ceiling %.3f"
              % (k, v["n_sessions"], v["effective_bars"], v["var_bar"],
                 v["ceiling_vs_true_signed"] or 0))

    print("\n" + "=" * 78)
    g1 = gate1(syms)
    print("GATE 1 -- split-half reliability of sflow   [bar %.2f, gated on WITHIN-symbol]"
          % BAR_RELIABILITY)
    print("  n=%d symbol-days over %d symbols" % (g1["n_symbol_days"], g1["n_symbols"]))
    print("  half-length rho   pooled %+.3f   within-symbol %+.3f"
          % (g1["half_pooled"] or 0, g1["half_within"] or 0))
    print("  Spearman-Brown    pooled %+.3f   within-symbol %+.3f   -> %s"
          % (g1["reliability_pooled"] or 0, g1["reliability_within"] or 0,
             "PASS" if g1["ok"] else "FAIL"))
    if g1.get("reliability_sflow5_implied"):
        print("  implied reliability of the 5-session sorter sflow5  %+.3f  (ceiling %.3f)"
              % (g1["reliability_sflow5_implied"], g1["attenuation_ceiling_sflow5"] or 0))
    if g1.get("attenuation_ceiling_within"):
        print("  daily attenuation ceiling sqrt(rho_xx) = %.3f  (one session cannot correlate above this)"
              % g1["attenuation_ceiling_within"])

    print("\n" + "=" * 78)
    g2 = gate2(tapes)
    print("GATE 2 -- increment over the best free rival   [bar: 10th pct of increment > 0]")
    for key in ("as_registered", "plain_vs_price", "gating"):
        blk = g2[key]
        print("  " + blk["label"])
        if blk.get("reason"):
            print("    " + blk["reason"])
            continue
        print("    n=%d over %d dates, effective n %.1f | rho(%s) %+.3f"
              % (blk["n"], blk["n_dates"], blk["n_effective"], blk["instrument"],
                 blk["rho_instrument"] or 0))
        print("    rivals  " + "   ".join("%s %+.3f" % (k, v or 0)
                                          for k, v in blk["rho_rivals"].items()))
        print("    increment %+.3f   date-clustered 10/90 [%+.3f, %+.3f]   -> %s"
              % (blk["increment"] or 0, blk["lo10"] or 0, blk["hi90"] or 0,
                 "PASS" if blk["ok"] else "FAIL"))
    if g2.get("rho_h60_sflow_for_contrast") is not None:
        print("  for contrast, h60 sflow vs TRUE signed %+.3f on n=%d"
              % (g2["rho_h60_sflow_for_contrast"], g2["n_h60"]))
    print("  NOTE " + g2["specification_error"])

    print("\n" + "=" * 78)
    g3 = gate3(syms, a.max_symbols)
    print("GATE 3 -- does the rate of change exist   [lag1 >= %.2f AND departure >= %.2f]"
          % (BAR_ROC_LAG1, BAR_ROC_ACF_DEPARTURE))
    if g3.get("reason"):
        print("  " + g3["reason"])
    else:
        print("  level  ACF lags 1-5 : " + " ".join("%+.3f" % x for x in g3["level_acf_lag1_5"]))
        print("  ROC    ACF lags 1-5 : " + " ".join("%+.3f" % (x or 0)
                                                    for x in g3["roc_acf_lag1_5"]))
        print("  white-noise filter  : " + " ".join("%+.3f" % x
                                                    for x in g3["white_noise_roc_acf"]))
        print("  max departure from white %.3f | corr(ROC, level) %+.3f vs %.3f if white   -> %s"
              % (g3["max_departure_from_white"] or 0, g3["corr_roc_level"] or 0,
                 g3["corr_roc_level_identity_if_white"], "PASS" if g3["ok"] else "FAIL"))

    print("\n" + "=" * 78)
    g4 = gate4(tapes)
    print("GATE 4 -- estimand hygiene, the unsigned arm   [rho >= %.2f AND rho vs vol < 0]"
          % BAR_UNSIGNED_RHO)
    if g4.get("reason"):
        print("  " + g4["reason"])
    else:
        print("  rho(unsigned bar arm, TRUE unsigned)  %+.3f" % (g4["rho_unsigned_vs_true"] or 0))
        print("  rho(unsigned bar arm, realized vol)   %+.3f" %
              (g4["rho_unsigned_vs_realized_vol"] or 0))
        print("  rho(TRUE unsigned,    realized vol)   %+.3f   (thesis #8 measured %.3f)"
              % (g4["rho_true_vs_realized_vol"] or 0, g4["thesis8_reference"]))
        print("  -> %s" % ("PASS" if g4["ok"] else "FAIL"))

    gates = {"gate0_resolution": g0, "gate1_reliability": g1, "gate2_increment": g2,
             "gate3_roc_exists": g3, "gate4_estimand": g4}
    failed = [k for k, v in gates.items() if v.get("ok") is False]
    print("\n" + "=" * 78)
    print("CASCADE: %d of 4 gating checks passed" % (4 - len(failed)))
    for k in failed:
        print("   FAILED %s" % k)
    print("\nGate 3 was pre-registered as EXPECTED TO FAIL (reference/flow-direction.md section 5).")
    print("A gate failure constrains what the board may claim; it is not an error.")

    payload = {"panel_fingerprint": panel_fingerprint(), "buckets_per_day": BUCKETS_PER_DAY,
               "n_tapes_usable": len(tapes), "tapes_dropped": dropped,
               "tapes": [{k: v for k, v in t.items() if k in
                          ("sym", "date", "true_unsigned", "true_signed", "n_prints")}
                         for t in tapes],
               "gates": gates, "failed": failed}
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(payload, indent=1, default=float), encoding="utf-8")
        print("\nwrote %s" % a.json)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
