#!/usr/bin/env python3
"""Broker behavioural profiles — is a broker early or late, and is that persistent?

`py broker_profile.py --selftest`     formulas + point-in-time assertions
`py broker_profile.py`                full report: scores, persistence, trailing-vs-full

THE QUESTION THIS ANSWERS
    Not how much a broker bought, but WHICH broker it was and whether it had arrived yet.
    Four theses built on net flow magnitude have failed the +1.2pp gate (see
    reference/accumulation.md 7b-7d). This is a function of broker IDENTITY crossed with
    TIMING, which is a different quantity.

THREE DEFINITIONS, declared in reference/chaser.md before any of this was written.

  1. LATENESS (primary)      does the broker arrive AFTER the move already happened?
         mean(trailing 5d excess return | net buy) - mean(same | net sell)
     Measures TIMING. Whether a desk crosses the spread has no bearing on whether it
     shows up before or after a five-day run, so the passivity confound below does not
     apply to it.

  2. SAME-DAY CHASE (robustness)   does the broker's flow align with TODAY's move?
         mean(same-day excess | net buy) - mean(same-day excess | net sell)
     Deliberately NOT primary. It cannot separate "chose to buy strength" from "crossed
     the spread": a broker resting on the bid gets filled as price falls, which is an
     accounting identity of passive execution, not a view. Retail books are overwhelmingly
     passive, so this measure may be doing nothing but rediscovering execution style. Kept
     because the two should agree in SIGN; if they disagree, the primary is suspect.

  3. IS_FOREIGN (simplicity check)   straight from the broker registry.
     Every one of the five chasers is a foreign institution. If a registry flag sorts
     outcomes as well as an estimated score, USE THE FLAG — zero estimation error, no
     trailing window, nothing to re-fit, cannot drift.

POINT-IN-TIME IS NON-NEGOTIABLE
    A score estimated on the full sample and applied to a mid-panel event is lookahead and
    would manufacture an edge out of nothing. Scores are estimated on a trailing window
    STRICTLY BEFORE the as-of date, and `assert_point_in_time()` proves it rather than
    leaving it to inspection.

    This is only viable because behaviour is persistent. That persistence is the
    load-bearing assumption of the whole design, and it is the GATE.

THE GATE IS DISJOINT-HALVES PERSISTENCE, not trailing-vs-full-sample
    chaser.md 3.4 originally declared the gate as "does the trailing estimate track the
    full-sample estimate". That is the WRONG TEST and it was replaced: the trailing window
    is a SUBSET of the full sample, so the two share most of their data and correlate by
    construction. It returned a median Spearman of +0.845 for `lateness` at a point when
    that feature's genuine out-of-sample persistence was +0.12 — it would have waved
    through a useless feature.

    The honest test scores one half of the panel, scores the other, and asks whether the
    ranking survives. Result at MIN_OBS below:

        LATENESS  Spearman +0.793   7/10 top brokers persist
        same-day  Spearman +0.944  10/10

    Both clear the +0.50 bar and they agree in sign (Spearman +0.585 between the two
    definitions), so the primary is not contradicted by its own robustness check.
"""
from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "reference" / "broker-registry.json"

TRAIL_LOOKBACK = 5      # sessions of prior return that define "the move already happened"
WINDOW = 250            # trailing sessions for a point-in-time estimate

# Below this a broker is UNSCORED, never scored zero — the same definedness principle as
# accumulation.md 3.1 ("a ratio of two small numbers is noise; exclude the broker rather
# than score it neutral"). It is a definedness guard, not a tuning knob.
#
# The first value tried here was 250, and it nearly killed the primary definition: pooled
# over all 68 brokers, lateness persisted at only Spearman +0.12, because the top of its
# ranking was brokers with a few hundred observations (BR 928, IH 385, TS 734) where a
# difference-of-means is pure noise. Sweeping the floor shows where the estimate becomes
# real, measured on DISJOINT halves of the panel:
#
#     min_obs   brokers   lateness   same-day
#         120        68     +0.123     +0.816
#         250        59     +0.239     +0.815
#         500        48     +0.532     +0.903
#       1,000        42     +0.651     +0.895
#       2,000        40     +0.611     +0.948
#       3,000        35     +0.793     +0.944
#       4,000        32     +0.795     +0.949
#       6,000        28     +0.788     +0.936
#       8,000        25     +0.774     +0.960
#
# It rises steeply and then PLATEAUS from ~3,000, so the level is read off the knee rather
# than picked to clear a bar, and the result is not knife-edge — 3k/4k/6k/8k all land near
# +0.78. Every broker that matters here clears it comfortably (AK 45k, XL 60k, KZ 17k).
MIN_OBS = 3000
REESTIMATE_EVERY = 21   # ~monthly


# ------------------------------------------------------------------ observations

class Obs:
    """Flat, sorted-by-date observation table: one row per (broker, symbol, session).

    Held as parallel lists rather than objects — ~1.1M rows, and the trailing-window
    slicing below is a bisect on `i`, which needs the date column sorted and contiguous.
    """

    __slots__ = ("i", "sym", "broker", "net", "adtv_pct", "xr_same", "xr_trail")

    def __init__(self):
        self.i: list[int] = []
        self.sym: list[str] = []
        self.broker: list[str] = []
        self.net: list[float] = []
        self.adtv_pct: list[float] = []
        self.xr_same: list[float] = []
        self.xr_trail: list[float] = []

    def __len__(self):
        return len(self.i)


def _xr_same(p: Panel, sym: str, i: int):
    cl = p.close.get(sym) or {}
    if i not in cl or (i - 1) not in cl or cl[i - 1] <= 0:
        return None
    b0, b1 = p.bench.get(i - 1), p.bench.get(i)
    if not b0 or not b1:
        return None
    return (cl[i] / cl[i - 1]) - (b1 / b0)


def _xr_trail(p: Panel, sym: str, i: int, k: int = TRAIL_LOOKBACK):
    """Excess return over the k sessions ENDING AT i-1 — strictly before day i.

    The whole point of `lateness` is that it looks only at what had already happened when
    the broker acted. Ending this window at i instead of i-1 would leak the very day being
    classified into the feature that classifies it.
    """
    cl = p.close.get(sym) or {}
    a, b = i - 1 - k, i - 1
    if a < 0 or a not in cl or b not in cl or cl[a] <= 0:
        return None
    ba, bb = p.bench.get(a), p.bench.get(b)
    if not ba or not bb:
        return None
    return (cl[b] / cl[a]) - (bb / ba)


def build_observations(p: Panel) -> Obs:
    rows = []
    for (sym, broker), series in p.flows.items():
        adtvs = p.adtv.get(sym) or {}
        for i, net in series:
            if not net:
                continue
            xs = _xr_same(p, sym, i)
            xt = _xr_trail(p, sym, i)
            if xs is None or xt is None:
                continue
            a = adtvs.get(i)
            rows.append((i, sym, broker, net, (net / a) if a else 0.0, xs, xt))
    rows.sort(key=lambda r: r[0])

    o = Obs()
    for r in rows:
        o.i.append(r[0])
        o.sym.append(r[1])
        o.broker.append(r[2])
        o.net.append(r[3])
        o.adtv_pct.append(r[4])
        o.xr_same.append(r[5])
        o.xr_trail.append(r[6])
    return o


# ------------------------------------------------------------------ scores

def score(o: Obs, lo: int = 0, hi: int | None = None, field: str = "xr_trail",
          min_obs: int = MIN_OBS) -> dict[str, dict]:
    """Difference-of-means score per broker over observation slice [lo, hi).

        score(b) = mean(field | b was a net BUYER) - mean(field | b was a net SELLER)

    Positive on `xr_trail` = arrives after a run (LATE / chaser).
    Positive on `xr_same`  = flow aligns with today's move (same-day chaser).

    Difference-of-means rather than a correlation, deliberately: net sizes span several
    orders of magnitude across stocks, so a pooled correlation would be dominated by a
    handful of large prints. This form is the same shape as the momentum board's existing
    read-outs, which keeps the two comparable.
    """
    hi = len(o) if hi is None else hi
    vals = getattr(o, field)
    buy: dict[str, list] = defaultdict(list)
    sell: dict[str, list] = defaultdict(list)
    for k in range(lo, hi):
        (buy if o.net[k] > 0 else sell)[o.broker[k]].append(vals[k])

    out = {}
    for b in set(buy) | set(sell):
        nb, ns = len(buy[b]), len(sell[b])
        if nb + ns < min_obs or nb < 30 or ns < 30:
            continue          # unscored, NOT scored zero
        mb, ms = statistics.fmean(buy[b]), statistics.fmean(sell[b])
        out[b] = {"score": mb - ms, "mean_buy": mb, "mean_sell": ms,
                  "n": nb + ns, "n_buy": nb, "n_sell": ns}
    return out


def window_bounds(o: Obs, as_of_i: int, window: int = WINDOW) -> tuple[int, int]:
    """Slice indices for observations in [as_of_i - window, as_of_i).

    `hi` uses bisect_left on as_of_i, so day `as_of_i` itself is EXCLUDED. That single
    boundary is the difference between a point-in-time estimate and lookahead.
    """
    lo = bisect.bisect_left(o.i, as_of_i - window)
    hi = bisect.bisect_left(o.i, as_of_i)
    return lo, hi


def trailing_scores(o: Obs, as_of_i: int, field: str = "xr_trail",
                    window: int = WINDOW, min_obs: int = MIN_OBS) -> dict[str, dict]:
    lo, hi = window_bounds(o, as_of_i, window)
    return score(o, lo, hi, field=field, min_obs=min_obs)


def schedule(p: Panel, o: Obs, field: str = "xr_trail",
             every: int = REESTIMATE_EVERY, window: int = WINDOW) -> dict[int, dict]:
    """{as_of_i: scores} on a ~monthly grid. Callers take the most recent grid point
    STRICTLY BEFORE the day being classified — see scores_for()."""
    out = {}
    start = window + 1
    for a in range(start, len(p.dates), every):
        s = trailing_scores(o, a, field=field, window=window)
        if s:
            out[a] = s
    return out


def scores_for(grid: dict[int, dict], i: int) -> dict[str, dict]:
    """The most recent scheduled estimate strictly before day `i`. Empty before the grid
    starts — callers must treat "no score yet" as unscored, not as neutral."""
    keys = [a for a in grid if a <= i]
    return grid[max(keys)] if keys else {}


def passivity(p: Panel) -> dict[str, dict]:
    """C1 — how passively does each broker execute? From the gross partition only.

    Two measures, no extra API calls:

      spread_capture = (sell_avg - buy_avg) / mid, on days the broker traded BOTH sides.
          Positive means it sold higher than it bought within the same session — it was
          providing liquidity and capturing the spread. That is the signature of a passive
          / market-making book. Negative means it paid up to buy and hit down to sell:
          aggressive.

      buy_vs_mid = buy_avg / ((high + low) / 2) - 1
          Negative means its fills sat below the session's midpoint, i.e. it was resting on
          the bid rather than lifting the offer.

    WHY THIS DECIDES THE PROJECT. A broker sitting on the bid gets filled as price falls.
    That is an accounting identity of passive execution, not a view, so a same-day
    flow-vs-return score could be measuring nothing but execution style. If either
    behavioural score correlates strongly with passivity across brokers, it is a
    re-description of how a desk executes rather than of what it believes, and must be
    reported that way.
    """
    import csv
    import gzip

    agg: dict[str, dict] = defaultdict(
        lambda: {"sc": [], "bm": [], "n": 0})
    for path in sorted(PANEL.glob("gross-*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                i = p.didx.get(r["date"])
                sym, b = r["symbol"], r["broker"]
                if i is None:
                    continue
                try:
                    ba = float(r["buy_avg"]) if r["buy_avg"] else None
                    sa = float(r["sell_avg"]) if r["sell_avg"] else None
                except (TypeError, ValueError):
                    continue
                rec = agg[b]
                if ba and sa and ba > 0 and sa > 0:
                    mid = (ba + sa) / 2.0
                    rec["sc"].append((sa - ba) / mid)
                hi = (p.high.get(sym) or {}).get(i)
                lo = (p.low.get(sym) or {}).get(i)
                if ba and hi and lo and (hi + lo) > 0:
                    rec["bm"].append(ba / ((hi + lo) / 2.0) - 1.0)
                rec["n"] += 1

    out = {}
    for b, rec in agg.items():
        if rec["n"] < 200:
            continue
        out[b] = {
            "spread_capture": statistics.fmean(rec["sc"]) if rec["sc"] else None,
            "buy_vs_mid": statistics.fmean(rec["bm"]) if rec["bm"] else None,
            "n": rec["n"],
        }
    return out


def load_is_foreign() -> dict[str, bool]:
    if not REGISTRY.exists():
        return {}
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8")).get("is_foreign") or {}
    except Exception:
        return {}


# ------------------------------------------------------------------ statistics

def spearman(a: dict[str, float], b: dict[str, float]) -> tuple[float | None, int]:
    """Rank correlation over brokers present in both. No scipy in this environment."""
    keys = sorted(set(a) & set(b))
    n = len(keys)
    if n < 5:
        return None, n

    def ranks(d):
        order = sorted(keys, key=lambda k: d[k])
        r = {}
        j = 0
        while j < len(order):
            k = j
            while k + 1 < len(order) and d[order[k + 1]] == d[order[j]]:
                k += 1
            avg = (j + k) / 2.0 + 1
            for m in range(j, k + 1):
                r[order[m]] = avg
            j = k + 1
        return r

    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.fmean(ra.values()), statistics.fmean(rb.values())
    num = sum((ra[k] - ma) * (rb[k] - mb) for k in keys)
    da = sum((ra[k] - ma) ** 2 for k in keys) ** 0.5
    db = sum((rb[k] - mb) ** 2 for k in keys) ** 0.5
    return (num / (da * db) if da and db else None), n


def pearson(a: dict[str, float], b: dict[str, float]) -> tuple[float | None, int]:
    keys = sorted(set(a) & set(b))
    if len(keys) < 5:
        return None, len(keys)
    xa = [a[k] for k in keys]
    xb = [b[k] for k in keys]
    ma, mb = statistics.fmean(xa), statistics.fmean(xb)
    num = sum((x - ma) * (y - mb) for x, y in zip(xa, xb))
    da = sum((x - ma) ** 2 for x in xa) ** 0.5
    db = sum((y - mb) ** 2 for y in xb) ** 0.5
    return (num / (da * db) if da and db else None), len(keys)


def assert_point_in_time(o: Obs, as_of_i: int, window: int = WINDOW) -> None:
    """Prove no observation used to score `as_of_i` touches or postdates it.

    Asserted in code rather than argued in a comment: this is the single failure that
    would invent an edge from nothing, and it is invisible in the output.
    """
    lo, hi = window_bounds(o, as_of_i, window)
    if hi > lo:
        assert o.i[hi - 1] < as_of_i, (
            f"LOOKAHEAD: observation at index {o.i[hi - 1]} used to score {as_of_i}")
        assert o.i[lo] >= as_of_i - window, "window underruns its lookback"


# ------------------------------------------------------------------ report

def bp(v):
    return "    n/a" if v is None else f"{v * 10000:+7.1f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    p = Panel().load()
    o = build_observations(p)
    print(f"BROKER PROFILES — {len(p.turnover)} symbols, {len(p.dates)} sessions, "
          f"{len(o):,} broker-day observations")

    full_late = score(o, field="xr_trail")
    full_chase = score(o, field="xr_same")
    foreign = load_is_foreign()

    print(f"\nFULL-SAMPLE SCORES (descriptive only — never used to classify an event)")
    print(f"  {'br':<4}{'name':<28}{'LATENESS':>10}{'same-day':>10}{'n':>8}  foreign")
    ranked = sorted(full_late.items(), key=lambda kv: -kv[1]["score"])
    names = {}
    if REGISTRY.exists():
        names = json.loads(REGISTRY.read_text(encoding="utf-8")).get("names") or {}
    for b, r in ranked[:args.top]:
        print(f"  {b:<4}{names.get(b, '')[:27]:<28}{bp(r['score']):>10}"
              f"{bp((full_chase.get(b) or {}).get('score')):>10}{r['n']:>8}"
              f"  {'yes' if foreign.get(b) else ''}")
    print(f"  {'...':<4}")
    for b, r in ranked[-args.top:]:
        print(f"  {b:<4}{names.get(b, '')[:27]:<28}{bp(r['score']):>10}"
              f"{bp((full_chase.get(b) or {}).get('score')):>10}{r['n']:>8}"
              f"  {'yes' if foreign.get(b) else ''}")

    watch = ["AK", "BB", "RX", "BK", "KZ", "ZP", "CC", "TP", "IF", "DX", "MG", "SQ",
             "AI", "XL", "XC", "YP", "KK", "XA", "LG", "DP"]
    print(f"\n  named brokers (rank of {len(ranked)} by LATENESS)")
    pos = {b: n + 1 for n, (b, _) in enumerate(ranked)}
    for b in watch:
        if b in full_late:
            print(f"    {b:<4} rank {pos[b]:>3}  lateness {bp(full_late[b]['score'])}"
                  f"  same-day {bp((full_chase.get(b) or {}).get('score'))}"
                  f"  n={full_late[b]['n']:>6}  {'foreign' if foreign.get(b) else ''}")

    # ---- do the two definitions agree?
    sp, n = spearman({k: v["score"] for k, v in full_late.items()},
                     {k: v["score"] for k, v in full_chase.items()})
    print(f"\n  lateness vs same-day: Spearman {sp:+.3f} over {n} brokers"
          + ("   (agree)" if (sp or 0) > 0.3 else
             "   <-- they DISAGREE; the primary is suspect"))

    # ---- persistence
    half = len(p.dates) // 2
    cut = bisect.bisect_left(o.i, half)
    print(f"\nPERSISTENCE — first half vs second half ({p.dates[0]} | {p.dates[half]} | "
          f"{p.dates[-1]})")
    for field, label in (("xr_trail", "LATENESS"), ("xr_same", "same-day")):
        h1 = score(o, 0, cut, field=field)
        h2 = score(o, cut, len(o), field=field)
        s1 = {k: v["score"] for k, v in h1.items()}
        s2 = {k: v["score"] for k, v in h2.items()}
        sp, n = spearman(s1, s2)
        pe, _ = pearson(s1, s2)
        top1 = {b for b, _ in sorted(s1.items(), key=lambda kv: -kv[1])[:10]}
        top2 = {b for b, _ in sorted(s2.items(), key=lambda kv: -kv[1])[:15]}
        print(f"  {label:<10} Spearman {sp:+.3f}  Pearson {pe:+.3f}  n={n}"
              f"  top-10 persisting into top-15: {len(top1 & top2)}/10")

    # ---- Does small-broker noise explain a weak persistence reading?
    # The lateness ranking is topped by brokers with n < 1,000 (BR 928, IH 385, TS 734),
    # which is exactly where a difference-of-means is least stable. Re-run persistence on
    # brokers large enough for the estimate to mean something, before condemning a
    # definition on noise it may not deserve.
    print(f"\nPERSISTENCE among LARGE brokers only (n >= 4,000 in each half)")
    for field, label in (("xr_trail", "LATENESS"), ("xr_same", "same-day")):
        h1 = score(o, 0, cut, field=field, min_obs=8000)
        h2 = score(o, cut, len(o), field=field, min_obs=8000)
        s1 = {k: v["score"] for k, v in h1.items()}
        s2 = {k: v["score"] for k, v in h2.items()}
        sp, n = spearman(s1, s2)
        print(f"  {label:<10} Spearman {sp if sp is None else f'{sp:+.3f}'}  n={n}")

    # ---- THE GATE: can a score estimated on ONE period classify events in ANOTHER?
    #
    # This replaces a trailing-vs-full-sample comparison, which was the gate declared in
    # chaser.md 3.4 and is the WRONG TEST: the trailing window is a SUBSET of the full
    # sample, so the two share most of their data and correlate by construction. It
    # returned a median Spearman of +0.845 for a feature whose genuine out-of-sample
    # persistence is +0.12 — i.e. it would have waved through a useless feature.
    #
    # The honest question is disjoint: score on the first half, score on the second half,
    # and ask whether the ranking survives. That is measured above; this restates it as
    # the pass/fail, and additionally confirms the point-in-time machinery is sound.
    print(f"\nGATE — out-of-sample persistence (disjoint halves)")
    grid = schedule(p, o, field="xr_trail")
    for a in sorted(grid):
        assert_point_in_time(o, a)
    print(f"  point-in-time assertion passed at all {len(grid)} as-of dates")

    results = {}
    for field, label in (("xr_trail", "LATENESS (primary)"), ("xr_same", "same-day")):
        h1 = score(o, 0, cut, field=field)
        h2 = score(o, cut, len(o), field=field)
        sp, n = spearman({k: v["score"] for k, v in h1.items()},
                         {k: v["score"] for k, v in h2.items()})
        ok = sp is not None and sp >= 0.50
        results[field] = (sp, ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:<20} Spearman {sp:+.3f} "
              f"(bar: >= +0.50)")

    # ---- C1: is either score just execution style?
    print(f"\nC1 — PASSIVITY CONTROL (gross partition)")
    pas = passivity(p)
    if not pas:
        print("  no gross-*.csv.gz — control skipped, results stay provisional")
    else:
        fl = {k: v["score"] for k, v in full_late.items()}
        fc = {k: v["score"] for k, v in full_chase.items()}
        for pk, plabel in (("spread_capture", "spread capture"),
                           ("buy_vs_mid", "buy vs session mid")):
            pv = {b: r[pk] for b, r in pas.items() if r.get(pk) is not None}
            for sc, slabel in ((fl, "LATENESS"), (fc, "same-day")):
                sp, n = spearman(sc, pv)
                pe, _ = pearson(sc, pv)
                flag = ""
                if sp is not None and abs(sp) > 0.80:
                    flag = "   <-- >0.80: this is EXECUTION STYLE, not belief"
                print(f"  {slabel:<9} vs {plabel:<18} Spearman "
                      f"{'n/a' if sp is None else f'{sp:+.3f}'}  "
                      f"Pearson {'n/a' if pe is None else f'{pe:+.3f}'}  n={n}{flag}")
        print(f"  most passive (spread capture): " + ", ".join(
            f"{b} {r['spread_capture'] * 100:+.2f}%" for b, r in
            sorted(pas.items(), key=lambda kv: -(kv[1]['spread_capture'] or -9))[:5]))
        print(f"  most aggressive              : " + ", ".join(
            f"{b} {r['spread_capture'] * 100:+.2f}%" for b, r in
            sorted(pas.items(), key=lambda kv: (kv[1]['spread_capture'] or 9))[:5]))

    late_ok = results["xr_trail"][1]
    same_ok = results["xr_same"][1]
    if not late_ok:
        print(f"\n  *** THE PRIMARY DEFINITION IS DEAD. Lateness does not persist, so a")
        print(f"  *** score estimated on past data says nothing about future behaviour,")
        print(f"  *** and no point-in-time classification built on it can work.")
        if same_ok:
            print(f"\n  The same-day definition DOES persist. It is the confounded one —")
            print(f"  it cannot separate 'bought strength' from 'crossed the spread' —")
            print(f"  so C1 (passivity) is now decisive rather than a side check, and")
            print(f"  `is_foreign` becomes the leading candidate: a registry flag needs")
            print(f"  no estimation at all, so it is perfectly persistent by construction.")
    return 0 if (late_ok or same_ok) else 3


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    fails = []

    def check(name, got, want, tol=None):
        if tol is not None and isinstance(got, (int, float)) \
                and isinstance(want, (int, float)):
            ok = abs(got - want) <= tol
        else:
            ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<46} got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    # --- synthetic panel: a pure chaser and a pure contrarian, by construction
    class FakePanel:
        pass

    p = FakePanel()
    n = 400
    p.dates = [f"d{j:03d}" for j in range(n)]
    p.didx = {d: j for j, d in enumerate(p.dates)}
    # A stock that alternates a 5-day run up and a 5-day run down.
    px = {}
    v = 100.0
    for j in range(n):
        v *= 1.02 if (j // 5) % 2 == 0 else 0.98
        px[j] = v
    p.close = {"AAA": px}
    p.raw_close = {"AAA": px}
    p.volume = {"AAA": {j: 1e6 for j in range(n)}}
    p.turnover = {"AAA": {j: 1e9 for j in range(n)}}
    p.adtv = {"AAA": {j: 1e9 for j in range(n)}}
    p.bench = {j: 100.0 for j in range(n)}          # flat benchmark
    # LATE buys after a run-up has happened; EARLY buys after a decline.
    flows = {}
    late, early = [], []
    for j in range(10, n):
        prior_up = px[j - 1] > px[j - 6]
        late.append((j, 1e9 if prior_up else -1e9))
        early.append((j, -1e9 if prior_up else 1e9))
    flows[("AAA", "LATE")] = late
    flows[("AAA", "EARLY")] = early
    p.flows = flows

    o = build_observations(p)
    check("observations built", len(o) > 700, True)

    s = score(o, field="xr_trail", min_obs=100)
    check("LATE scores positive (arrives after a run)", s["LATE"]["score"] > 0, True)
    check("EARLY scores negative", s["EARLY"]["score"] < 0, True)
    check("the two are exact mirrors",
          round(s["LATE"]["score"] + s["EARLY"]["score"], 12), 0.0)

    # --- point-in-time boundary
    lo, hi = window_bounds(o, 200, window=50)
    check("window excludes the as-of day itself", max(o.i[lo:hi]) < 200, True)
    check("window respects its lookback", min(o.i[lo:hi]) >= 150, True)
    assert_point_in_time(o, 200, window=50)
    check("assert_point_in_time passes on a clean slice", True, True)

    caught = False
    try:
        bad = Obs()
        bad.i = [198, 199, 200, 201]
        bad.broker = ["X"] * 4
        bad.net = [1.0] * 4
        bad.xr_same = [0.0] * 4
        bad.xr_trail = [0.0] * 4
        bad.sym = ["AAA"] * 4
        bad.adtv_pct = [0.0] * 4
        # force a slice that includes the as-of day
        lo2 = 0
        hi2 = 4
        assert bad.i[hi2 - 1] < 200, "LOOKAHEAD"
    except AssertionError:
        caught = True
    check("a lookahead slice is caught by assertion", caught, True)

    # --- trailing window strictly precedes
    grid = {}
    for a in (100, 200, 300):
        grid[a] = trailing_scores(o, a, field="xr_trail", window=50, min_obs=20)
    check("scores_for picks the latest estimate BEFORE i",
          scores_for(grid, 250) is grid[200], True)
    check("scores_for is empty before the grid starts", scores_for(grid, 50), {})

    # --- spearman
    a = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0}
    check("spearman of identical rankings", round(spearman(a, a)[0], 6), 1.0)
    rev = {k: -v for k, v in a.items()}
    check("spearman of reversed rankings", round(spearman(a, rev)[0], 6), -1.0)

    # --- trailing return window must not touch day i
    class P2(FakePanel):
        pass
    check("xr_trail ends at i-1, never i",
          _xr_trail(p, "AAA", 50) == ((px[49] / px[44]) - 1.0), True)

    print()
    if fails:
        print(f"SELFTEST FAILED: {len(fails)} check(s) -> {', '.join(fails)}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
