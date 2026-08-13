#!/usr/bin/env python3
"""Second-resolution tape microstructure from Invezgo `running-trade`.

Pure functions, no network, no I/O. `python tape_lib.py --selftest` checks every
statistic against a hand-built fixture plus the real BREN 2026-08-12 10:30:08 sweep.

WHAT THIS MODULE IS FOR
    Detecting ORDER SLICING — one large order broken into many small ones to disguise
    size. That leaves its fingerprint in EXECUTED trades and is therefore fully
    historical and fully validatable.

WHAT IT IS NOT FOR
    Spoofing, in the strict sense of displaying size with intent to cancel. That needs
    order placements and cancellations, which no feed on this plan carries historically.
    See reference/accumulation.md 8. Nothing in this module should be described as
    detecting spoofing, because it cannot.

THREE FACTS ABOUT THE FEED THAT SHAPE EVERYTHING BELOW

1.  `volume` is in SHARES, not lots. IDX lots are 100 shares. Every rupiah figure here
    is `price * volume` with no lot factor; applying one inflates value 100x and makes
    every broker look like a whale.

2.  `buyer_dom` / `seller_dom` is the CLIENT's domicile, NOT the broker's. Verified on
    BREN 2026-08-12, where consecutive prints read `ZP,DP,D,F` then `ZP,DP,F,F` — same
    buying broker, same selling broker, opposite flag. So the tape carries a
    foreign/domestic label on every individual print, which is a direct measurement of
    institutional-vs-retail participation rather than the broker-level proxy Sectors'
    `cohort` field provides. Treating it as a broker attribute would produce a broker
    that is 60% foreign and 40% domestic and no way to explain it.

3.  Timestamps are "HH:MM:SS" WIB strings with real seconds populated. A single second
    routinely holds a dozen prints — that is the sweep signature, not a rounding
    artifact. The opening auction genuinely lands on :00.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict

SHARES_PER_LOT = 100

# A "one-second cluster" needs at least this many prints from the same broker on the
# same side before it is called a burst. Four is a guess, tuned in accumulation.md 3.8;
# two is ordinary, and ten is unmistakable.
BURST_MIN_PRINTS = 4

# A sweep is one second in which a single broker is on the SAME side of at least this
# many prints against multiple counterparties.
SWEEP_MIN_PRINTS = 5

# Distinct counterparties in a single second before a cluster is called a sweep rather
# than ordinary trading. In a normal second a broker faces one to three names; reaching
# six means it consumed a whole price level of resting orders in one order. Six is a
# GUESS, and it is deliberately well below the 33 counterparties AK faced in the
# 2026-08-11 BREN sweep so the label is not fitted to that one observation.
SWEEP_MIN_COUNTERPARTIES = 6


def _secs(hhmmss: str) -> int | None:
    """"10:30:08" -> 37808. None on anything unparseable, so one malformed row cannot
    take down a whole session's statistics."""
    parts = str(hhmmss or "").strip().split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def parse_prints(rows) -> list[dict]:
    """Normalise raw running-trade rows into typed prints, dropping unusable ones.

    Output keys: t (seconds since midnight WIB), time, price, volume (shares),
    value (IDR), lots, buyer, seller, buyer_dom, seller_dom, aggressor, board.

    Rows are returned in feed order, NOT re-sorted. The feed's order within a single
    second is the execution sequence — `avg_price` decreases monotonically through the
    BREN 10:30:08 sweep, which confirms it — and re-sorting would destroy the only
    sequencing information a one-second cluster contains.
    """
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t = _secs(r.get("time"))
        if t is None:
            continue
        try:
            price = float(r.get("price") or 0)
            volume = float(r.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or volume <= 0:
            continue
        buyer = str(r.get("buyer") or "").strip().upper()
        seller = str(r.get("seller") or "").strip().upper()
        # IDX masks codes to "--" during the live session. A masked tape produces
        # per-broker statistics that are silently about nobody.
        if buyer in ("", "--") or seller in ("", "--"):
            continue
        out.append({
            "t": t,
            "time": str(r.get("time")),
            "price": price,
            "volume": volume,
            "value": price * volume,
            "lots": volume / SHARES_PER_LOT,
            "buyer": buyer,
            "seller": seller,
            "buyer_dom": str(r.get("buyer_dom") or "").strip().upper(),
            "seller_dom": str(r.get("seller_dom") or "").strip().upper(),
            "aggressor": str(r.get("type") or "").strip().upper(),
            "board": str(r.get("board") or "").strip().upper(),
        })
    return out


def side_prints(prints: list[dict], broker: str, side: str) -> list[dict]:
    """Prints where `broker` is the buyer (side="buy") or the seller (side="sell")."""
    key = "buyer" if side == "buy" else "seller"
    b = broker.strip().upper()
    return [p for p in prints if p[key] == b]


# ---------------------------------------------------------------- aggregation

def aggregate_by_broker(prints: list[dict]) -> dict[str, dict]:
    """Per-broker buy/sell value, share volume, print count and VWAP, from the tape.

    This is the reconciliation hook: these figures should reproduce `summary-stock`'s
    buy_value / buy_volume / buy_freq / buy_avg for the same stock-day. They will not
    match exactly — the tape is one board segment at a time and summary-stock aggregates
    differently around auctions — but a gap beyond a few percent means the two sources
    disagree about what happened, and everything downstream is then measuring the
    disagreement rather than the market.
    """
    agg: dict[str, dict] = defaultdict(lambda: {
        "buy_value": 0.0, "buy_volume": 0.0, "buy_freq": 0,
        "sell_value": 0.0, "sell_volume": 0.0, "sell_freq": 0})
    for p in prints:
        b = agg[p["buyer"]]
        b["buy_value"] += p["value"]
        b["buy_volume"] += p["volume"]
        b["buy_freq"] += 1
        s = agg[p["seller"]]
        s["sell_value"] += p["value"]
        s["sell_volume"] += p["volume"]
        s["sell_freq"] += 1
    for rec in agg.values():
        rec["buy_avg"] = (rec["buy_value"] / rec["buy_volume"]
                          if rec["buy_volume"] else None)
        rec["sell_avg"] = (rec["sell_value"] / rec["sell_volume"]
                           if rec["sell_volume"] else None)
        rec["net_value"] = rec["buy_value"] - rec["sell_value"]
        gross = rec["buy_value"] + rec["sell_value"]
        rec["osr"] = rec["buy_value"] / gross if gross else None
        rec["ats_buy"] = (rec["buy_value"] / rec["buy_freq"]
                          if rec["buy_freq"] else None)
        rec["ats_sell"] = (rec["sell_value"] / rec["sell_freq"]
                           if rec["sell_freq"] else None)
    return dict(agg)


# ---------------------------------------------------------------- burst / slicing

def burst_stats(prints: list[dict], broker: str, side: str) -> dict:
    """How much of a broker's activity arrives in one-second clusters.

    `burst_max_1s`  most prints in any single second
    `burst_max_10s` most prints in any 10-second window (sliding, not bucketed —
                    bucketing would split a burst straddling a boundary and halve it)
    `p_burst`       share of prints landing in a second holding >= BURST_MIN_PRINTS
    `ipi_median`    median seconds between consecutive prints
    `ipi_sub1s`     share of consecutive gaps that are 0 seconds, i.e. same-second
    """
    ps = side_prints(prints, broker, side)
    n = len(ps)
    base = {"n_prints": n, "burst_max_1s": 0, "burst_max_10s": 0,
            "p_burst": None, "ipi_median": None, "ipi_sub1s": None}
    if not n:
        return base

    per_sec = Counter(p["t"] for p in ps)
    base["burst_max_1s"] = max(per_sec.values())

    # Sliding 10s window over distinct occupied seconds.
    secs = sorted(per_sec)
    best, lo = 0, 0
    running = 0
    for hi in range(len(secs)):
        running += per_sec[secs[hi]]
        while secs[hi] - secs[lo] >= 10:
            running -= per_sec[secs[lo]]
            lo += 1
        best = max(best, running)
    base["burst_max_10s"] = best

    in_burst = sum(c for c in per_sec.values() if c >= BURST_MIN_PRINTS)
    base["p_burst"] = in_burst / n

    if n > 1:
        ts = [p["t"] for p in ps]
        gaps = sorted(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
        mid = len(gaps) // 2
        base["ipi_median"] = float(
            gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2)
        base["ipi_sub1s"] = sum(1 for g in gaps if g < 1) / len(gaps)
    return base


def clip_stats(prints: list[dict], broker: str, side: str) -> dict:
    """Print-size fingerprint. Algorithmic slicers repeat identical clips.

    `modal_lots`       the most common lot size
    `clip_uniformity`  share of prints at exactly that size
    `clip_entropy`     Shannon entropy of the lot-size distribution in bits, normalised
                       by log2(n_distinct) so it is comparable across brokers with
                       different print counts. Near 0 = one repeated clip; near 1 = the
                       ragged distribution a book of genuine client orders produces.
    `price_pin`        share of prints at the single most-used price
    """
    ps = side_prints(prints, broker, side)
    n = len(ps)
    out = {"modal_lots": None, "clip_uniformity": None,
           "clip_entropy": None, "price_pin": None}
    if not n:
        return out

    lots = Counter(round(p["lots"]) for p in ps)
    modal, modal_n = lots.most_common(1)[0]
    out["modal_lots"] = modal
    out["clip_uniformity"] = modal_n / n

    if len(lots) > 1:
        h = -sum((c / n) * math.log2(c / n) for c in lots.values())
        out["clip_entropy"] = h / math.log2(len(lots))
    else:
        out["clip_entropy"] = 0.0

    prices = Counter(p["price"] for p in ps)
    out["price_pin"] = prices.most_common(1)[0][1] / n
    return out


def slice_z(agg: dict[str, dict], broker: str, side: str = "buy") -> float | None:
    """ln(freq_share / value_share) — positive means the broker is spending more TRADES
    than its rupiah share implies, i.e. deliberate fragmentation.

    Relative rather than absolute on purpose: the same rupiah ticket is 128x the lots on
    a Rp50 stock as on a Rp6,400 one, so an absolute clip-size threshold would flag every
    penny stock and no blue chip. Same reasoning as reference/scoring.md.
    """
    vk, fk = f"{side}_value", f"{side}_freq"
    tot_v = sum(r[vk] for r in agg.values())
    tot_f = sum(r[fk] for r in agg.values())
    rec = agg.get(broker.strip().upper())
    if not rec or not tot_v or not tot_f or not rec[fk] or not rec[vk]:
        return None
    return math.log((rec[fk] / tot_f) / (rec[vk] / tot_v))


# ---------------------------------------------------------------- sweeps & crossing

def sweep_events(prints: list[dict], min_prints: int = SWEEP_MIN_PRINTS) -> list[dict]:
    """One-second clusters where a single broker is on the same side of many prints.

    The BREN 2026-08-12 10:30:08 cluster is the reference case: 16 prints in one second,
    every one at 3520 with AK as the seller, against ZP, NI, XL, XC, YP, CC, TP and AK
    itself. That is one desk clearing a price level in a single tick.

    Returned per event: t, time, broker, side, n_prints, value, lots, n_counterparties,
    n_prices, self_cross (prints where the broker was on both sides).
    """
    by_sec: dict[int, list[dict]] = defaultdict(list)
    for p in prints:
        by_sec[p["t"]].append(p)

    events: list[dict] = []
    for t, group in by_sec.items():
        if len(group) < min_prints:
            continue
        for side, key, other in (("sell", "seller", "buyer"), ("buy", "buyer", "seller")):
            counts = Counter(p[key] for p in group)
            for broker, n in counts.items():
                if n < min_prints:
                    continue
                mine = [p for p in group if p[key] == broker]
                events.append({
                    "t": t,
                    "time": mine[0]["time"],
                    "broker": broker,
                    "side": side,
                    "n_prints": n,
                    "value": sum(p["value"] for p in mine),
                    "lots": sum(p["lots"] for p in mine),
                    "n_counterparties": len({p[other] for p in mine}),
                    "n_prices": len({p["price"] for p in mine}),
                    "self_cross": sum(1 for p in mine if p["buyer"] == p["seller"]),
                })
    events.sort(key=lambda e: (-e["value"], e["t"]))
    return events


def self_cross_stats(prints: list[dict], broker: str | None = None) -> dict:
    """Prints where the same broker is both buyer and seller — an internal cross.

    Not inherently improper: a broker matching two client orders in-house is ordinary.
    It matters here because it inflates a broker's gross on BOTH sides, which drags
    one-sidedness towards 0.5 and can make a real accumulator read as churn. Any desk
    with a material self-cross share needs its `osr` read with that in mind.
    """
    pool = prints if broker is None else [
        p for p in prints
        if broker.strip().upper() in (p["buyer"], p["seller"])]
    n = len(pool)
    if not n:
        return {"n": 0, "self_cross_prints": 0, "self_cross_pct": None,
                "self_cross_value": 0.0}
    xs = [p for p in pool if p["buyer"] == p["seller"]]
    return {"n": n, "self_cross_prints": len(xs), "self_cross_pct": len(xs) / n,
            "self_cross_value": sum(p["value"] for p in xs)}


def aggression_stats(prints: list[dict], broker: str, side: str) -> dict:
    """Passive vs aggressive fills — the dimension that decides what a burst MEANS.

    `aggressor` names the side that crossed the spread. So for a broker's BUY prints:
        aggressor == "BUY"   the broker lifted the offer   -> AGGRESSIVE, paying up
        aggressor == "SELL"  someone hit the broker's bid  -> PASSIVE, absorbing

    Without this, burst statistics are ambiguous in a way that inverts their reading.
    On BREN 2026-08-11, TP shows 1,332 buy prints with p_burst 72% — which looks like
    frantic activity until you see the prints are PASSIVE: TP sat on the bid at 3,310
    and was hit 1,332 times. That is absorption, the exact opposite of chasing.

    And it separates the two behaviours this module exists to tell apart:

      SWEEP  one second, many prints, MANY COUNTERPARTIES, aggressive.
             One large market order consuming a whole level of resting orders. The
             prints are the victims' orders, not the sweeper slicing its own.
             AK's 327 prints in one second against 33 counterparties is this.

      SLICE  many prints spread ACROSS TIME, uniform clip size, pinned to one price,
             usually passive. One participant working a large order quietly.

    Reading a sweep as slicing would flag the most impatient participant on the tape as
    the most patient one.
    """
    ps = side_prints(prints, broker, side)
    want_aggressive = "BUY" if side == "buy" else "SELL"
    n = len(ps)
    if not n:
        return {"n": 0, "passive_pct": None, "passive_value_pct": None}
    agg_n = sum(1 for p in ps if p["aggressor"] == want_aggressive)
    tot_v = sum(p["value"] for p in ps)
    agg_v = sum(p["value"] for p in ps if p["aggressor"] == want_aggressive)
    return {
        "n": n,
        "passive_pct": (n - agg_n) / n,
        "passive_value_pct": ((tot_v - agg_v) / tot_v) if tot_v else None,
    }


def classify_behaviour(prints: list[dict], broker: str, side: str,
                       agg: dict[str, dict] | None = None,
                       min_prints: int = 20) -> str:
    """One label per (broker, side): sweep / slice / block / passive-absorb / mixed.

    Deliberately coarse. The point is to stop a board from calling a sweeper a slicer,
    not to build a taxonomy nobody can check.
    """
    bs = burst_stats(prints, broker, side)
    if bs["n_prints"] < min_prints:
        return "thin"
    cs = clip_stats(prints, broker, side)
    ag = aggression_stats(prints, broker, side)
    sweeps = [e for e in sweep_events(prints)
              if e["broker"] == broker.strip().upper() and e["side"] == side]
    z = slice_z(agg, broker, side) if agg else None
    passive = ag["passive_pct"] or 0.0

    big_sweep = any(e["n_counterparties"] >= SWEEP_MIN_COUNTERPARTIES for e in sweeps)
    if big_sweep and passive < 0.5:
        return "sweep"
    if z is not None and z >= 0.30 and (cs["clip_uniformity"] or 0) >= 0.15:
        return "slice"
    if z is not None and z <= -0.50:
        return "block" if passive < 0.6 else "passive-block"
    if passive >= 0.75:
        return "passive-absorb"
    return "mixed"


def client_dom_split(prints: list[dict], broker: str, side: str) -> dict:
    """Foreign/domestic split of the CLIENT behind a broker's prints.

    See the module docstring: the dom flag travels with the order, not the broker, so a
    single broker legitimately shows both. That is the point — it is the finest-grained
    institutional-vs-retail read available anywhere in this data set.
    """
    ps = side_prints(prints, broker, side)
    key = "buyer_dom" if side == "buy" else "seller_dom"
    tot = sum(p["value"] for p in ps)
    if not tot:
        return {"value": 0.0, "foreign_pct": None, "domestic_pct": None}
    f = sum(p["value"] for p in ps if p[key] == "F")
    d = sum(p["value"] for p in ps if p[key] == "D")
    return {"value": tot, "foreign_pct": f / tot, "domestic_pct": d / tot}


def dom_flow(prints: list[dict]) -> dict:
    """Session-wide net value by client domicile — a direct retail-vs-institution read
    that does not depend on any broker cohort label.

    Note this can disagree in SIGN with country-level foreign flow, and on BREN it did:
    national foreign flow was negative on both 2026-08-11 and 08-12 while foreign
    *brokers* were the accumulators. Country-level netting hides the transfer.
    """
    out = {"F": {"buy": 0.0, "sell": 0.0}, "D": {"buy": 0.0, "sell": 0.0}}
    for p in prints:
        if p["buyer_dom"] in out:
            out[p["buyer_dom"]]["buy"] += p["value"]
        if p["seller_dom"] in out:
            out[p["seller_dom"]]["sell"] += p["value"]
    for rec in out.values():
        rec["net"] = rec["buy"] - rec["sell"]
    return out


def session_vwap(prints: list[dict]) -> float | None:
    tot_v = sum(p["volume"] for p in prints)
    return (sum(p["value"] for p in prints) / tot_v) if tot_v else None


# ---------------------------------------------------------------- selftest

_SWEEP = [
    # The real BREN 2026-08-12 10:30:08 cluster, verbatim from running-trade.
    ("10:30:00", 3520, 4100, "TP", "XC", "F", "D"),
    ("10:30:02", 3520, 400, "TP", "XL", "F", "D"),
    ("10:30:02", 3520, 5000, "TP", "XC", "F", "D"),
    ("10:30:05", 3520, 1100, "TP", "XL", "F", "D"),
    ("10:30:08", 3520, 2300, "ZP", "AK", "D", "F"),
    ("10:30:08", 3520, 2300, "ZP", "AK", "D", "F"),
    ("10:30:08", 3520, 200, "NI", "AK", "D", "F"),
    ("10:30:08", 3520, 2300, "ZP", "AK", "D", "F"),
    ("10:30:08", 3520, 300, "XL", "AK", "D", "F"),
    ("10:30:08", 3520, 1900, "AK", "AK", "F", "F"),
    ("10:30:08", 3520, 200, "XC", "AK", "D", "F"),
    ("10:30:08", 3520, 2700, "ZP", "AK", "D", "F"),
    ("10:30:08", 3520, 8500, "AK", "AK", "F", "F"),
    ("10:30:08", 3520, 100, "YP", "AK", "D", "F"),
    ("10:30:08", 3520, 3100, "ZP", "AK", "D", "F"),
    ("10:30:08", 3520, 10500, "AK", "AK", "F", "F"),
    ("10:30:08", 3520, 7300, "CC", "AK", "F", "F"),
    ("10:30:08", 3520, 20500, "AK", "AK", "F", "F"),
    ("10:30:08", 3520, 2200, "ZP", "AK", "D", "F"),
    ("10:30:08", 3520, 142600, "TP", "AK", "F", "F"),
]


def _fixture() -> list[dict]:
    return [{"board": "RG", "time": t, "price": p, "volume": v,
             "buyer": b, "seller": s, "buyer_dom": bd, "seller_dom": sd,
             "type": "SELL", "avg_price": 0}
            for (t, p, v, b, s, bd, sd) in _SWEEP]


def _selftest() -> int:
    fails = []

    def check(name, got, want, tol=1e-9):
        ok = (abs(got - want) <= tol) if isinstance(want, (int, float)) and \
            isinstance(got, (int, float)) else got == want
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<38} got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    prints = parse_prints(_fixture())
    check("parse_prints keeps every row", len(prints), 20)
    check("masked codes dropped",
          len(parse_prints([{"time": "09:00:00", "price": 1, "volume": 1,
                             "buyer": "--", "seller": "XL"}])), 0)
    check("volume is shares, not lots", prints[0]["lots"], 41.0)
    check("value = price * shares", prints[0]["value"], 3520 * 4100)

    bs = burst_stats(prints, "AK", "sell")
    check("AK sell prints", bs["n_prints"], 16)
    check("AK burst_max_1s (the sweep)", bs["burst_max_1s"], 16)
    check("AK ipi_sub1s", round(bs["ipi_sub1s"], 6), 1.0)
    check("AK p_burst", bs["p_burst"], 1.0)

    tp = burst_stats(prints, "TP", "buy")
    check("TP buy prints", tp["n_prints"], 5)
    check("TP burst_max_1s", tp["burst_max_1s"], 2)
    check("TP p_burst (below threshold)", tp["p_burst"], 0.0)

    sweeps = sweep_events(prints)
    ak = [e for e in sweeps if e["broker"] == "AK" and e["side"] == "sell"]
    check("one AK sell sweep found", len(ak), 1)
    check("sweep n_prints", ak[0]["n_prints"], 16)
    check("sweep at one price", ak[0]["n_prices"], 1)
    check("sweep counterparties", ak[0]["n_counterparties"], 8)
    check("sweep self-crosses", ak[0]["self_cross"], 4)

    xs = self_cross_stats(prints, "AK")
    check("AK self-cross prints", xs["self_cross_prints"], 4)

    cs = clip_stats(prints, "ZP", "buy")
    check("ZP price_pin (one level)", cs["price_pin"], 1.0)
    check("ZP modal clip is 23 lots", cs["modal_lots"], 23)

    agg = aggregate_by_broker(prints)
    check("AK is one-sided seller", round(agg["AK"]["osr"], 4),
          round(agg["AK"]["buy_value"] /
                (agg["AK"]["buy_value"] + agg["AK"]["sell_value"]), 4))
    check("TP bought nothing back", agg["TP"]["sell_value"], 0.0)
    check("TP buy prints match", agg["TP"]["buy_freq"], 5)

    # Every print is at 3520, so VWAP must be exactly 3520 — catches any share/lot mixup
    # in the weighting.
    check("session VWAP", session_vwap(prints), 3520.0)

    dom = dom_flow(prints)
    check("domestic clients are net buyers here",
          dom["D"]["net"] > 0, True)

    # Aggression. Every fixture print is aggressor=SELL, so buyers are PASSIVE (their
    # bids were hit) and AK, the seller, is fully AGGRESSIVE. Getting this backwards
    # would report the desk that dumped 16 prints in a second as a patient accumulator.
    check("TP buys were passive (bid hit)",
          aggression_stats(prints, "TP", "buy")["passive_pct"], 1.0)
    check("AK sells were aggressive",
          aggression_stats(prints, "AK", "sell")["passive_pct"], 0.0)
    # The fixture holds 16 AK prints, under the production 20-print floor, so the floor
    # is lowered here to exercise the real branch rather than the "thin" guard.
    check("AK reads as a sweep, not a slice",
          classify_behaviour(prints, "AK", "sell", agg, min_prints=5), "sweep")
    check("thin flow is not classified at all",
          classify_behaviour(prints, "NI", "buy", agg), "thin")

    print()
    if fails:
        print(f"SELFTEST FAILED: {len(fails)} check(s) -> {', '.join(fails)}")
        return 1
    print("SELFTEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
