#!/usr/bin/env python3
"""H4 — is the iceberg visible? A MEASUREMENT study on the cached tapes.

Thesis #11. Framework and PASS BAR pre-registered in `reference/vsa-tape.md`.

    H4  On bars satisfying the deseasonalised, tick-scaled trigger, the buy side is
        DOMINATED by one broker, that broker is PASSIVE (its bid is hit, it does not lift
        the offer), and it trades in LARGE clips (slice_z < 0).

Computes NO forward return, so check 0 does not gate it -- which matters, because check 0
blocks every forward-return study on this data (blockdom.md sec 9). This is the one question
the purchased microstructure data can still answer.

THE SAMPLE IS SELECTED: these 29 tapes were pulled for BREN/PTRO surge case studies. A
mechanism can be demonstrated on a selected sample; a payoff cannot. Every line is descriptive.

Two mandatory guards, both from prior silent failures: truncated tapes cached as complete
(vpin_validate.truncated), and the tape/m5 corporate-action mismatch (RAJA 2026-02-27, a 1:5
split that books -80% on an unguarded join).

    py scripts/tape_vsa.py
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

from alpha_lib import PANEL, panel_fingerprint  # noqa: E402
import tape_lib as TL  # noqa: E402
from trade_lib import tick_size  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TAPE_DIR = ROOT / "data" / "tape"
INTRADAY = ROOT / "data" / "intraday"

BUCKET_MIN = 5
OPEN_HHMM = "09:00"
CLOSE_HHMM = "16:00"          # MOC lands entirely here; the thesis's 15:50-16:15 does not exist
SEASONAL_LOOKBACK = 20

TRIG_RVOL = 3.0
TRIG_TICKS = 3                # tick-scaled narrowness
TRIG_MIN_FREQ = 10            # a dom_share over 3 prints is not a measurement
C1_RVOL = 3.0                 # high-volume WIDE control
C2_RVOL_MAX = 1.5             # ordinary-bar control

BAR_JOINT_PCT = 0.60          # pass bar 1
BAR_OVER_C1_PP = 15.0         # pass bar 2
BAR_ALIGN = 0.90              # pass bar 4
BAR_MIN_TAPES = 20

DOM_MIN = 0.40
PASSIVE_MIN = 0.60
CA_TOLERANCE = 0.20           # tape vs m5 session VWAP; median disagreement is 3.7bp


def log(msg: str = "") -> None:
    print(msg, flush=True)


def bucket_of(t: str) -> str:
    return f"{t[:2]}:{int(t[3:5]) // BUCKET_MIN * BUCKET_MIN:02d}"


# ------------------------------------------------------------------ inputs

def load_m5(sym: str) -> dict[str, dict[str, float]]:
    """{date: {hhmm: volume}} from the cached 5-minute store."""
    f = INTRADAY / f"m5-{sym}.csv.gz"
    if not f.exists():
        return {}
    out: dict[str, dict[str, float]] = defaultdict(dict)
    with gzip.open(f, "rt", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out[r["date"]][r["hhmm"]] = float(r["volume"])
            except (TypeError, ValueError):
                continue
    return out


def load_m5_vwap(sym: str, date: str) -> float | None:
    """Proxy session VWAP from the m5 store -- the CA guard's reference."""
    f = INTRADAY / f"m5-{sym}.csv.gz"
    if not f.exists():
        return None
    pv = vv = 0.0
    with gzip.open(f, "rt", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["date"] != date or r["hhmm"] < OPEN_HHMM:
                continue
            try:
                h, l, c, v = float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"])
            except (TypeError, ValueError):
                continue
            pv += ((h + l + c) / 3) * v
            vv += v
    return (pv / vv) if vv > 0 else None


def load_expected_prints() -> dict[tuple[str, str], float]:
    """(symbol, date) -> sum of broker buy_freq, the tape's expected print count."""
    exp: dict[tuple[str, str], float] = defaultdict(float)
    for f in sorted(glob.glob(str(PANEL / "gross-*.csv.gz"))):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    exp[(r["symbol"], r["date"])] += float(r["buy_freq"])
                except (TypeError, ValueError):
                    continue
    return exp


# ------------------------------------------------------------------ bar build

def build_bars(prints: list[dict]) -> dict[str, dict]:
    """Exact per-bar value/freq/OHLC plus per-broker buy detail. No proxying."""
    bars: dict[str, dict] = {}
    for p in prints:
        t = p["time"]
        if not (OPEN_HHMM <= t[:5] < CLOSE_HHMM):
            continue
        b = bars.setdefault(bucket_of(t), {
            "volume": 0.0, "value": 0.0, "freq": 0,
            "o": p["price"], "h": p["price"], "l": p["price"], "c": p["price"],
            "buy": defaultdict(lambda: {"value": 0.0, "freq": 0, "passive_value": 0.0}),
            "buy_value": 0.0, "buy_freq": 0,
        })
        px, vol, val = p["price"], p["volume"], p["value"]
        b["volume"] += vol
        b["value"] += val
        b["freq"] += 1
        b["h"] = max(b["h"], px)
        b["l"] = min(b["l"], px)
        b["c"] = px
        buyer = p.get("buyer")
        if buyer:
            e = b["buy"][buyer]
            e["value"] += val
            e["freq"] += 1
            # aggressor SELL == someone HIT this broker's resting bid == passive absorption.
            # aggressor BUY would mean the broker lifted the offer. tape_lib documents the
            # inversion this prevents: TP's 1,332 "frantic" BREN prints were passive.
            if p.get("aggressor") == "SELL":
                e["passive_value"] += val
            b["buy_value"] += val
            b["buy_freq"] += 1
    return bars


def measure_bar(b: dict) -> dict | None:
    """dom_share / passive_share / slice_z for the bar's dominant BUY broker."""
    if b["buy_value"] <= 0 or b["buy_freq"] < 1 or not b["buy"]:
        return None
    code, e = max(b["buy"].items(), key=lambda kv: kv[1]["value"])
    if e["value"] <= 0 or e["freq"] < 1:
        return None
    vshare = e["value"] / b["buy_value"]
    fshare = e["freq"] / b["buy_freq"]
    return {
        "broker": code,
        "dom_share": vshare,
        "passive_share": e["passive_value"] / e["value"],
        "slice_z": math.log(fshare / vshare) if fshare > 0 and vshare > 0 else None,
    }


def classify(bars: dict, seasonal: dict[str, float]) -> list[dict]:
    rows = []
    for hhmm, b in sorted(bars.items()):
        base = seasonal.get(hhmm)
        if not base or base <= 0:
            continue
        rvol_c = b["volume"] / base
        c = b["c"] or 1.0
        rng = b["h"] - b["l"]
        narrow_tick = (rng / tick_size(c)) <= TRIG_TICKS
        narrow_lit = (rng / c) < 0.005
        m = measure_bar(b)
        rows.append({"hhmm": hhmm, "rvol_c": rvol_c, "freq": b["freq"],
                     "narrow_tick": narrow_tick, "narrow_lit": narrow_lit,
                     "volume": b["volume"], "value": b["value"], **(m or {})})
    return rows


def joint(r: dict) -> bool:
    return (r.get("dom_share") is not None
            and r["dom_share"] > DOM_MIN
            and (r.get("passive_share") or 0) > PASSIVE_MIN
            and (r.get("slice_z") is not None and r["slice_z"] < 0))


def summarise(rows: list[dict], label: str) -> dict:
    n = len(rows)
    if not n:
        return {"label": label, "n": 0}
    def frac(pred):
        return sum(1 for r in rows if pred(r)) / n
    doms = [r["dom_share"] for r in rows if r.get("dom_share") is not None]
    pas = [r["passive_share"] for r in rows if r.get("passive_share") is not None]
    szs = [r["slice_z"] for r in rows if r.get("slice_z") is not None]
    return {
        "label": label, "n": n,
        "joint_pct": 100 * frac(joint),
        "dom_pct": 100 * frac(lambda r: (r.get("dom_share") or 0) > DOM_MIN),
        "passive_pct": 100 * frac(lambda r: (r.get("passive_share") or 0) > PASSIVE_MIN),
        "block_pct": 100 * frac(lambda r: r.get("slice_z") is not None and r["slice_z"] < 0),
        "med_dom": statistics.median(doms) if doms else None,
        "med_passive": statistics.median(pas) if pas else None,
        "med_slice_z": statistics.median(szs) if szs else None,
    }


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(PANEL / "tape_vsa.json"))
    args = ap.parse_args()

    log("=" * 78)
    log("H4 -- IS THE ICEBERG VISIBLE?  Pre-registered: reference/vsa-tape.md")
    log("MEASUREMENT ONLY. No forward return. SELECTED SAMPLE -- descriptive throughout.")
    log("=" * 78)

    expected = load_expected_prints()
    files = sorted(glob.glob(str(TAPE_DIR / "*" / "*.json.gz")))
    log(f"\ntapes on disk: {len(files)}")

    trig, c1, c2 = [], [], []
    kept, dropped, align_hits, align_tot = [], [], 0, 0
    lit_only = tick_only = both = 0
    m5cache: dict[str, dict] = {}

    for f in files:
        key = os.path.basename(f).replace(".json.gz", "")
        sym, date = key.split("-", 1)
        try:
            raw = json.load(gzip.open(f, "rt", encoding="utf-8"))
        except Exception as e:
            dropped.append((key, f"unreadable: {e}"))
            continue
        prints = TL.parse_prints(raw)
        if not prints:
            dropped.append((key, "no parsable prints (all masked?)"))
            continue

        # GUARD 1 -- truncation, against the gross panel's own print count
        exp = expected.get((sym, date), 0.0)
        if exp and len(prints) < 0.5 * exp:
            dropped.append((key, f"TRUNCATED: {len(prints)} prints vs {exp:.0f} expected"))
            continue

        # GUARD 2 -- corporate-action mismatch between the tape and m5 stores
        tv = TL.session_vwap(prints)
        mv = load_m5_vwap(sym, date)
        if tv and mv and not (1 - CA_TOLERANCE <= mv / tv <= 1 + CA_TOLERANCE):
            dropped.append((key, f"STORE MISMATCH (corporate action?): m5/tape VWAP {mv / tv:.3f}"))
            continue

        if sym not in m5cache:
            m5cache[sym] = load_m5(sym)
        hist = m5cache[sym]
        prior = sorted(d for d in hist if d < date)[-SEASONAL_LOOKBACK:]
        if len(prior) < 10:
            dropped.append((key, f"only {len(prior)} prior m5 sessions for the seasonal baseline"))
            continue
        seasonal: dict[str, float] = {}
        buckets = {h for d in prior for h in hist[d]}
        for h in buckets:
            vs = [hist[d][h] for d in prior if h in hist[d]]
            if len(vs) >= 5:
                seasonal[h] = statistics.median(vs)

        bars = build_bars(prints)
        if not bars:
            dropped.append((key, "no continuous-session bars"))
            continue

        # bucket-alignment check: tape-derived volume vs the m5 store, same date
        same = hist.get(date, {})
        if same:
            for h, b in bars.items():
                if h in same and same[h] > 0:
                    align_tot += 1
                    if abs(b["volume"] / same[h] - 1) < 0.10:
                        align_hits += 1

        rows = classify(bars, seasonal)
        for r in rows:
            if r["narrow_lit"] and r["narrow_tick"]:
                both += 1
            elif r["narrow_lit"]:
                lit_only += 1
            elif r["narrow_tick"]:
                tick_only += 1
            if r["rvol_c"] > TRIG_RVOL and r["freq"] >= TRIG_MIN_FREQ:
                (trig if r["narrow_tick"] else c1).append(r)
            elif r["rvol_c"] <= C2_RVOL_MAX and r["freq"] >= TRIG_MIN_FREQ:
                c2.append(r)
        kept.append(key)

    log(f"tapes kept: {len(kept)}   dropped: {len(dropped)}")
    for k, why in dropped:
        log(f"   DROP  {k:<22} {why}")

    align = (align_hits / align_tot) if align_tot else 0.0
    log(f"\nbucket alignment vs m5 store: {align:.1%} of {align_tot} shared buckets within 10%")
    log(f"narrowness definitions: both {both}, tick-only {tick_only}, literal-only {lit_only}")

    s_trig = summarise(trig, "TRIGGER  rvol_c>3 & tick-narrow")
    s_c1 = summarise(c1, "C1       rvol_c>3 & WIDE")
    s_c2 = summarise(c2, "C2       rvol_c<=1.5 ordinary")

    log("\n" + "-" * 78)
    log(f"{'population':<34} {'n':>6} {'joint%':>8} {'dom%':>7} {'pass%':>7} {'blk%':>7}")
    log("-" * 78)
    for s in (s_trig, s_c1, s_c2):
        if not s["n"]:
            log(f"{s['label']:<34} {0:>6}   (empty)")
            continue
        log(f"{s['label']:<34} {s['n']:>6} {s['joint_pct']:>7.1f}% {s['dom_pct']:>6.1f}% "
            f"{s['passive_pct']:>6.1f}% {s['block_pct']:>6.1f}%")
    log("")
    for s in (s_trig, s_c1, s_c2):
        if s["n"]:
            log(f"  {s['label'][:8]:<9} medians  dom {s['med_dom']:.3f}  "
                f"passive {s['med_passive']:.3f}  slice_z {s['med_slice_z']:+.3f}")

    over_c1 = (s_trig.get("joint_pct", 0) - s_c1.get("joint_pct", 0)) if s_c1["n"] else None
    checks = [
        (f"1 joint >= {BAR_JOINT_PCT:.0%} of trigger bars",
         s_trig["n"] > 0 and s_trig["joint_pct"] >= BAR_JOINT_PCT * 100),
        (f"2 exceeds C1 by >= {BAR_OVER_C1_PP}pp", over_c1 is not None and over_c1 >= BAR_OVER_C1_PP),
        ("3 every leg above C2",
         all(s_trig.get(k, 0) > s_c2.get(k, 0) for k in ("dom_pct", "passive_pct", "block_pct"))
         if (s_trig["n"] and s_c2["n"]) else False),
        (f"4 alignment >= {BAR_ALIGN:.0%} and >= {BAR_MIN_TAPES} tapes",
         align >= BAR_ALIGN and len(kept) >= BAR_MIN_TAPES),
    ]
    log("\n" + "=" * 78)
    for label, ok in checks:
        log(f"  [{'PASS' if ok else 'FAIL'}]  {label}")
    if over_c1 is not None:
        log(f"\n  trigger - C1 on the joint condition: {over_c1:+.1f}pp")
    verdict = "PASS" if all(ok for _, ok in checks) else "FAIL"
    log(f"\nVERDICT: {verdict}")
    if verdict == "FAIL" and s_trig["n"] and s_trig["dom_pct"] > 50 and s_trig["passive_pct"] < 50:
        log("NOTE: dominance holds but PASSIVITY does not -- these are SWEEPS, not absorption.")
        log("      The thesis has the participant right and the direction backwards.")
    log("=" * 78)

    Path(args.out).write_text(json.dumps({
        "study": "H4 iceberg visibility (measurement)",
        "preregistered": "reference/vsa-tape.md",
        "panel_fingerprint": panel_fingerprint(),
        "sample": "SELECTED (case-study tapes) -- descriptive only",
        "tapes_kept": kept, "tapes_dropped": dropped,
        "bucket_alignment": align, "alignment_n": align_tot,
        "narrowness_overlap": {"both": both, "tick_only": tick_only, "literal_only": lit_only},
        "trigger": s_trig, "control_wide": s_c1, "control_ordinary": s_c2,
        "trigger_minus_c1_pp": over_c1,
        "checks": {k: v for k, v in checks}, "verdict": verdict,
    }, indent=2, default=float), encoding="utf-8")
    log(f"\nwrote {args.out}")
    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
