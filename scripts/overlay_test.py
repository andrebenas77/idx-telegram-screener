#!/usr/bin/env python3
"""Does the 'buy them while they're quiet' overlay actually add anything?

The Quiet Accumulation board rests on a premise nobody has measured: that broker
accumulation is a BETTER entry when the stock is coiled (a tight, low-volatility base)
or oversold (washed out on drying volume) than when it is not. If the overlay adds
nothing, the board would be filtering on noise — and it would look perfectly sensible
on screen while doing so.

Every feature is computed from data up to and including the signal day only, and
returns still use the one-session entry lag, so nothing here can see the future.

Reported primarily on MEAN EXCESS, not hit rate: the payoff is right-skewed (48% hit
rate but positive mean), and ranking on hit rate was already shown to invert out of
sample. Hit rate is shown alongside for context.

Usage:
    py scripts/overlay_test.py [--min-adtv-pct 10] [--sweep]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel, summarise  # noqa: E402
from broker_alpha import build_events  # noqa: E402


# ------------------------------------------------------------------ setup features

def features(p: Panel, sym: str, i: int) -> dict | None:
    """Price-setup features on the signal day, using only data up to day i."""
    cl = p.close.get(sym) or {}
    vo = p.volume.get(sym) or {}
    need = [i - n for n in range(0, 120)]
    if not all(j in cl for j in need[:60]):
        return None

    px = [cl[j] for j in range(i - 59, i + 1)]          # last 60 sessions
    last = px[-1]
    if last <= 0:
        return None

    w20 = px[-20:]
    hi20, lo20 = max(w20), min(w20)
    range_pct = (hi20 - lo20) / last

    rets = [px[k] / px[k - 1] - 1 for k in range(1, len(px)) if px[k - 1] > 0]
    vol20 = statistics.pstdev(rets[-20:]) if len(rets) >= 20 else None

    # Volatility percentile against this stock's OWN history — an absolute vol
    # threshold would just select the sleepiest large caps every time.
    hist = []
    for j in range(i - 119, i + 1):
        seg = [cl[k] / cl[k - 1] - 1
               for k in range(j - 19, j + 1) if k in cl and (k - 1) in cl and cl[k - 1] > 0]
        if len(seg) >= 15:
            hist.append(statistics.pstdev(seg))
    vol_pctile = (sum(1 for h in hist if h <= vol20) / len(hist)
                  if hist and vol20 is not None else None)

    dd60 = last / max(px) - 1

    gains = [max(0.0, r) for r in rets[-14:]]
    losses = [max(0.0, -r) for r in rets[-14:]]
    ag, al = sum(gains) / 14, sum(losses) / 14
    rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

    v5 = [vo[j] for j in range(i - 4, i + 1) if j in vo]
    v20 = [vo[j] for j in range(i - 19, i + 1) if j in vo]
    rvol5 = (sum(v5) / len(v5)) / (sum(v20) / len(v20)) if v5 and v20 and sum(v20) else None

    out = {"range_pct": range_pct, "vol_pctile": vol_pctile, "dd60": dd60,
           "rsi": rsi, "rvol5": rvol5}
    out.update(structure(p, sym, i))
    return out


def structure(p: Panel, sym: str, i: int) -> dict:
    """Price structure and accumulation/distribution, from RAW daily bars.

    Structure is judged on printed levels, never on the back-adjusted series: a
    corporate action rescales adjusted history and would silently rewrite past highs.

    `clv` = (close - low) / (high - low): where in the session's range it closed.
    `cmf20` = Chaikin Money Flow: sum((2*clv - 1) * volume) / sum(volume) over 20
    sessions. This is the daily-bar answer to "did volume transact on strength or on
    weakness" — positive reads as accumulation, negative as distribution.

    NOTE this INFERS from the close's position in the range; it is not a true
    volume-at-price profile. A session that opened weak, rallied and closed on its high
    scores as accumulation even if most volume changed hands near the low. The literal
    measure needs intraday bars, which Invezgo only serves for the current day.
    """
    hi, lo, cl, vo = (p.high.get(sym) or {}), (p.low.get(sym) or {}), \
        (p.raw_close.get(sym) or {}), (p.volume.get(sym) or {})
    out: dict = {"hi_last": hi.get(i), "lo_last": lo.get(i),
                 "hi_prev": hi.get(i - 1), "lo_prev": lo.get(i - 1),
                 "hi_5d": None, "lo_5d": None, "trend": None,
                 "clv": None, "cmf20": None, "broke_5d_high": None}

    w = [j for j in range(i - 4, i + 1) if j in hi and j in lo]
    if w:
        out["hi_5d"] = max(hi[j] for j in w)
        out["lo_5d"] = min(lo[j] for j in w)

    # Higher-high AND higher-low = intact uptrend; both failing = broken.
    if all(v is not None for v in (out["hi_last"], out["lo_last"],
                                   out["hi_prev"], out["lo_prev"])):
        hh = out["hi_last"] > out["hi_prev"]
        hl = out["lo_last"] > out["lo_prev"]
        out["trend"] = "HH/HL" if (hh and hl) else ("LH/LL" if not (hh or hl) else "mixed")

    if out["hi_5d"] is not None and cl.get(i) is not None:
        # Compare against the 5-day high EXCLUDING today, otherwise today's own high
        # makes the test trivially true whenever it closes at its high.
        prior = [hi[j] for j in range(i - 4, i) if j in hi]
        if prior:
            out["broke_5d_high"] = cl[i] > max(prior)

    def clv_of(j: int):
        h, l, c = hi.get(j), lo.get(j), cl.get(j)
        if h is None or l is None or c is None:
            return None
        # A limit-locked ARA/ARB session has high == low. CLV is genuinely undefined
        # there — returning 0.5 would invent a neutral reading for the most decisive
        # sessions on IDX.
        if h == l:
            return None
        return (c - l) / (h - l)

    out["clv"] = clv_of(i)

    num = den = 0.0
    for j in range(i - 19, i + 1):
        c = clv_of(j)
        v = vo.get(j)
        if c is None or not v:
            continue
        num += (2 * c - 1) * v
        den += v
    out["cmf20"] = num / den if den else None
    return out


def classify(f: dict, range_max: float, vol_max: float, dd_max: float,
             rsi_max: float, rvol_max: float) -> set[str]:
    tags = set()
    if (f["vol_pctile"] is not None and f["range_pct"] <= range_max
            and f["vol_pctile"] <= vol_max):
        tags.add("coiled")
    if (f["rvol5"] is not None and f["dd60"] <= dd_max
            and f["rsi"] <= rsi_max and f["rvol5"] <= rvol_max):
        tags.add("oversold")
    return tags


def row(label: str, evs: list[dict]) -> str:
    if not evs:
        return f"  {label:<26}{'—':>8}"
    s3 = summarise([e["x3"] for e in evs])
    s5 = summarise([e["x5"] for e in evs])
    return (f"  {label:<26}{s3['n']:>8,}{s3['hit_rate']:>9.1%}{s5['hit_rate']:>9.1%}"
            f"{s3['mean_excess']:>+11.2%}{s5['mean_excess']:>+11.2%}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-adtv-pct", type=float, default=10.0)
    ap.add_argument("--min-value", type=float, default=500e6)
    ap.add_argument("--range-max", type=float, default=0.12)
    ap.add_argument("--vol-max", type=float, default=0.40)
    ap.add_argument("--dd-max", type=float, default=-0.20)
    ap.add_argument("--rsi-max", type=float, default=35.0)
    ap.add_argument("--rvol-max", type=float, default=1.0)
    ap.add_argument("--sweep", action="store_true",
                    help="also sweep the thresholds to check the answer is not a "
                         "knife-edge of one parameter choice")
    args = ap.parse_args()

    print("loading panel…")
    p = Panel().load()
    print(f"  {p.describe()}")

    events = build_events(p, args.min_value, args.min_adtv_pct)
    print(f"\nevents at >= {args.min_adtv_pct}% ADTV and >= Rp"
          f"{args.min_value/1e6:,.0f}m: {len(events):,}")

    # attach features once
    kept, skipped = [], 0
    for e in events:
        f = features(p, e["symbol"], e["i"])
        if f is None:
            skipped += 1
            continue
        e["f"] = f
        e["tags"] = classify(f, args.range_max, args.vol_max, args.dd_max,
                             args.rsi_max, args.rvol_max)
        kept.append(e)
    print(f"with full 120-session history: {len(kept):,} "
          f"({skipped:,} dropped near the window start)")

    hdr = (f"  {'group':<26}{'n':>8}{'hit3':>9}{'hit5':>9}"
           f"{'mean3':>11}{'mean5':>11}")
    print("\nDOES THE OVERLAY HELP?  (mean excess is the metric that matters)")
    print(hdr)
    print(row("ALL accumulation", kept))
    coiled = [e for e in kept if "coiled" in e["tags"]]
    over = [e for e in kept if "oversold" in e["tags"]]
    either = [e for e in kept if e["tags"]]
    neither = [e for e in kept if not e["tags"]]
    print(row("  coiled", coiled))
    print(row("  oversold", over))
    print(row("  either (the board)", either))
    print(row("  neither (excluded)", neither))

    base3 = summarise([e["x3"] for e in kept])["mean_excess"]
    base5 = summarise([e["x5"] for e in kept])["mean_excess"]
    verdict = {}
    if either:
        e3 = summarise([e["x3"] for e in either])["mean_excess"] - base3
        e5 = summarise([e["x5"] for e in either])["mean_excess"] - base5
        verdict = {"lift_3d": e3, "lift_5d": e5, "n": len(either)}
        print(f"\n  overlay lift vs all accumulation: "
              f"3d {e3:+.2%}, 5d {e5:+.2%}  (n={len(either):,})")
        if e3 <= 0 and e5 <= 0:
            print("  VERDICT: the overlay does NOT help. Filtering on it would make the")
            print("           board worse than ranking on accumulation alone.")
        elif e3 > 0 and e5 > 0:
            print("  VERDICT: the overlay helps on both horizons.")
        else:
            print("  VERDICT: mixed — helps on one horizon only. Treat as unproven.")

    # ---- continuous view: is there a gradient, or is the threshold arbitrary?
    print("\nGRADIENT CHECK — mean 3d excess by feature quintile")
    print("  (a real effect should trend monotonically, not appear only at one cut)")
    for name, key, rev in (("20d range % (tight->wide)", "range_pct", False),
                           ("drawdown from 60d high", "dd60", False),
                           ("RSI(14)", "rsi", False),
                           ("RVOL5", "rvol5", False)):
        vals = [(e["f"][key], e["x3"]) for e in kept if e["f"].get(key) is not None]
        if len(vals) < 500:
            continue
        vals.sort(reverse=rev)
        q = len(vals) // 5
        cells = []
        for k in range(5):
            seg = vals[k * q:(k + 1) * q] if k < 4 else vals[4 * q:]
            cells.append(sum(x for _, x in seg) / len(seg))
        print(f"  {name:<28}" + "".join(f"{c:>+10.2%}" for c in cells))

    if args.sweep:
        print("\nSWEEP — is the answer stable, or a knife-edge on one threshold?")
        print(hdr)
        for rmax in (0.08, 0.12, 0.18):
            for vmax in (0.25, 0.40, 0.60):
                sel = [e for e in kept
                       if e["f"]["vol_pctile"] is not None
                       and e["f"]["range_pct"] <= rmax
                       and e["f"]["vol_pctile"] <= vmax]
                print(row(f"  coiled r<={rmax} v<={vmax}", sel))
        for dd in (-0.15, -0.20, -0.30):
            for rs in (30.0, 35.0, 45.0):
                sel = [e for e in kept
                       if e["f"]["rvol5"] is not None
                       and e["f"]["dd60"] <= dd and e["f"]["rsi"] <= rs
                       and e["f"]["rvol5"] <= args.rvol_max]
                print(row(f"  oversold dd<={dd} rsi<={rs}", sel))

    out = {
        "params": vars(args),
        "n_events": len(kept),
        "groups": {
            name: summarise([e[f"x{k}"] for e in grp])
            for name, grp in (("all", kept), ("coiled", coiled), ("oversold", over),
                              ("either", either), ("neither", neither))
            for k in (3,)
        },
        "verdict": verdict,
    }
    path = PANEL / "overlay_test.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=float),
                    encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
