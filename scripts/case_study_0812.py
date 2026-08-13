#!/usr/bin/env python3
"""Forensic on the 2026-08-12 IDX runners — BREN, PTRO, CUAN, DSSA.

Two jobs at once:

  1. Answer the question that started this: what did the accumulation actually look
     like, when could it have been entered, and how much did the accumulators hand to
     retail on the markup day.
  2. Produce the calibration set for reference/accumulation.md. The thresholds in that
     document were declared BEFORE this ran, on purpose. This script reports what they
     would have done; it does not tune them. If a threshold is wrong, that is a finding
     to write down, not a number to quietly move.

Everything here is read-only against closed sessions. Broker codes are unmasked only
after the close, so this can never be run intraday and expected to name anyone.

Usage:
    py case_study_0812.py                        # full run, writes build/case-study-*.json
    py case_study_0812.py --no-tape              # skip the expensive tape pull
    py case_study_0812.py --symbols BREN         # one name
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tape_lib  # noqa: E402
from invezgo_client import InvezgoClient, decode_sankey_node  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
WIB = timezone(timedelta(hours=7))

SYMBOLS = ["BREN", "PTRO", "CUAN", "DSSA"]
SURGE = "2026-08-12"
LOOKBACK_DAYS = 21

BN = 1_000_000_000


# ------------------------------------------------------------------ small helpers

def rupiah(v) -> str:
    """Compact IDR. Board convention: bn to one decimal, tn above 1000bn."""
    if v is None:
        return "-"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1000 * BN:
        return f"{sign}Rp{a / (1000 * BN):.2f}tn"
    if a >= BN:
        return f"{sign}Rp{a / BN:.1f}bn"
    if a >= 1_000_000:
        return f"{sign}Rp{a / 1_000_000:.0f}m"
    return f"{sign}Rp{a:,.0f}"


def pct(v, dp=1) -> str:
    return "-" if v is None else f"{v * 100:.{dp}f}%"


def f(v) -> float | None:
    """Invezgo returns every numeric as a string. A silent str/float mix here would
    concatenate instead of adding and produce nonsense that still looks like a number."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def rows_of(payload, key=None):
    """Unwrap the three shapes this API uses: bare list, {"data": [...]}, {key: [...]}."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if key and isinstance(payload.get(key), list):
            return payload[key]
        if isinstance(payload.get("data"), list):
            return payload["data"]
    return []


def prev_session(px_dates: list[str], d: str) -> str | None:
    before = [x for x in px_dates if x < d]
    return before[-1] if before else None


# ------------------------------------------------------------------ pulls

def pull_daily(c: InvezgoClient, sym: str, start: str, end: str) -> dict:
    """inventory_chart: price series + per-broker CUMULATIVE daily net, one request.

    The first day of the window is a REAL FLOW, not a baseline — verified against
    summary-stock window aggregates. Treating it as a baseline and dropping it silently
    deletes a session of flow from every window.
    """
    payload = c.inventory_chart(sym, start=start, end=end, scope="val", limit=25)
    if not payload:
        return {"prices": [], "brokers": {}}

    prices = []
    for r in rows_of(payload, "price"):
        prices.append({
            "date": str(r.get("date"))[:10],
            "open": f(r.get("open")), "high": f(r.get("high")),
            "low": f(r.get("low")), "close": f(r.get("close")),
            "volume": f(r.get("volume")),
        })
    prices.sort(key=lambda r: r["date"])

    brokers: dict[str, dict] = {}
    for b in rows_of(payload, "broker"):
        code = str(b.get("broker") or b.get("code") or "").strip().upper()
        if not code:
            continue
        cum = {str(p.get("date"))[:10]: f(p.get("value")) or 0.0
               for p in rows_of(b, "data")}
        dates = sorted(cum)
        daily, prev = {}, 0.0
        for d in dates:
            daily[d] = cum[d] - prev      # day 1: prev=0, so daily == cum. Correct.
            prev = cum[d]
        brokers[code] = {"name": str(b.get("name") or "").strip(),
                         "cum": cum, "daily": daily}
    return {"prices": prices, "brokers": brokers}


def pull_gross(c: InvezgoClient, sym: str, start: str, end: str,
               market: str = "RG") -> dict[str, dict]:
    """summary-stock: per-broker gross buy/sell, freq and weighted avg price.

    A from/to range is AGGREGATED with no daily dimension, so windows are built by
    calling this with from == to and summing. Net value alone cannot separate an
    accumulator from a market maker (accumulation.md 2) — the gross sides are the point.
    """
    payload = c.broker_summary_stock(sym, start=start, end=end, market=market)
    out: dict[str, dict] = {}
    for r in rows_of(payload):
        code = str(r.get("code") or "").strip().upper()
        if not code:
            continue
        bv, sv = f(r.get("buy_value")) or 0.0, f(r.get("sell_value")) or 0.0
        gross = bv + sv
        out[code] = {
            "name": str(r.get("name") or "").strip(),
            "buy_value": bv, "sell_value": sv, "net_value": bv - sv, "gross": gross,
            "buy_volume": f(r.get("buy_volume")) or 0.0,
            "sell_volume": f(r.get("sell_volume")) or 0.0,
            "buy_freq": f(r.get("buy_freq")) or 0.0,
            "sell_freq": f(r.get("sell_freq")) or 0.0,
            "buy_avg": f(r.get("buy_avg")), "sell_avg": f(r.get("sell_avg")),
            "osr": (bv / gross) if gross else None,
            "ats_buy": (bv / f(r.get("buy_freq"))) if f(r.get("buy_freq")) else None,
        }
    return out


def pull_intraday(c: InvezgoClient, sym: str, d: str) -> dict:
    """intraday-inventory: each top broker's cumulative net through one past session."""
    payload = c.intraday_inventory(sym, date=d, range_=5, type_="value", total=5)
    if not payload:
        return {}
    out = {}
    for b in rows_of(payload, "broker"):
        code = str(b.get("code") or b.get("broker") or "").strip().upper()
        if not code:
            continue
        series = [(str(p.get("x")), f(p.get("y")) or 0.0) for p in rows_of(b, "data")]
        if series:
            out[code] = series
    return out


# ------------------------------------------------------------------ features

def turn_ratio(series: list[tuple[str, float]]) -> tuple[float | None, str | None]:
    """How much of a broker's intraday peak position was given back by the close.

    `turn >= 0.30` on a green day is distribution wearing accumulation's daily net —
    the broker's end-of-day figure still reads as a net buy, but a third of what it
    accumulated was sold back into the markup. No daily-bar feature can see this.

    DEFINED ONLY FOR NET BUYERS. Applied to a net seller the ratio is meaningless and
    explosive: XL on BREN 2026-08-12 ticked to +Rp587m early and closed at -Rp34.2bn,
    which reads as "turn 5925%" — an arithmetic artifact of a near-zero denominator, not
    a broker giving back 59x its position. A broker that ends the day short of where it
    started distributed all day; that is what net_value already says, and it needs no
    ratio.
    """
    if not series:
        return None, None
    close_v = series[-1][1]
    peak_v, peak_x = max(((v, x) for x, v in series), key=lambda t: t[0])
    if close_v <= 0 or peak_v <= 0 or peak_v < close_v:
        return None, None
    return (peak_v - close_v) / peak_v, peak_x


def run_length(daily: dict[str, float], upto: str) -> int:
    ds = [d for d in sorted(daily) if d <= upto]
    n = 0
    for d in reversed(ds):
        if daily[d] > 0:
            n += 1
        else:
            break
    return n


def adtv(prices: list[dict], upto: str, n: int = 20) -> float | None:
    """Trailing-n mean of close*volume, STRICTLY BEFORE `upto` — matches Panel.adtv,
    so the two boards normalise size the same way and cannot disagree."""
    hist = [p for p in prices if p["date"] < upto][-n:]
    vals = [p["close"] * p["volume"] for p in hist
            if p["close"] and p["volume"]]
    return sum(vals) / len(vals) if vals else None


# ------------------------------------------------------------------ report

def analyse(c: InvezgoClient, sym: str, surge: str, args) -> dict:
    start = (date.fromisoformat(surge) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    print(f"\n{'=' * 78}\n{sym}  —  accumulation into {surge}\n{'=' * 78}")

    daily = pull_daily(c, sym, start, surge)
    prices = daily["prices"]
    if not prices:
        print(f"  no price data returned for {sym} — skipping")
        return {"symbol": sym, "error": "no price data"}

    px_dates = [p["date"] for p in prices]
    d_1 = prev_session(px_dates, surge)
    px = {p["date"]: p for p in prices}
    adtv20 = adtv(prices, surge)

    # ---- price context
    print("\n  PRICE")
    for p in prices[-6:]:
        prev = prev_session(px_dates, p["date"])
        chg = ((p["close"] / px[prev]["close"] - 1) if prev and px[prev]["close"]
               else None)
        mark = "  <- surge" if p["date"] == surge else (
            "  <- entry day" if p["date"] == d_1 else "")
        print(f"    {p['date']}  close {p['close']:>8,.0f}  {pct(chg, 2):>8}"
              f"  vol {p['volume'] / 1e6:>8.1f}m{mark}")

    # ---- multi-window one-sidedness (accumulation.md 4.2: never a single window)
    # Every window ENDS AT D-1, not at the surge day. Letting D into the window that
    # selects the accumulators would make the trap metric circular and guarantee a large
    # answer — accumulation.md 5 calls this out as structural, not a preference.
    windows = {}
    pre_dates = [d for d in px_dates if d <= (d_1 or surge)]
    for w, label in ((5, "5d"), (20, "20d")):
        hist = pre_dates[-w:]
        if hist:
            windows[label] = pull_gross(c, sym, hist[0], hist[-1])

    book_d1 = pull_gross(c, sym, d_1, d_1) if d_1 else {}   # the entry-day book
    book_d = pull_gross(c, sym, surge, surge)               # the surge-day book

    w5, w20 = windows.get("5d", {}), windows.get("20d", {})

    def net_over(code: str, w: int) -> float:
        """Broker's net across the last `w` sessions ending D-1, from the daily series."""
        dd = daily["brokers"].get(code, {}).get("daily", {})
        return sum(v for k, v in dd.items() if k in set(pre_dates[-w:]))

    accum = []
    for code, r in w20.items():
        if r["osr"] is None or adtv20 is None:
            continue
        if r["gross"] < max(5 * BN, 0.5 * adtv20):   # definedness guard, md 3.1
            continue
        net20 = net_over(code, 20)
        if r["osr"] >= 0.80 and net20 >= max(10 * BN, 0.20 * adtv20):
            accum.append((code, r, net20))
    accum.sort(key=lambda t: -t[2])

    print(f"\n  ACCUMULATORS — frozen at {d_1}; osr20>=0.80 and "
          f"net20>=max(Rp10bn, 20% ADTV).   ADTV20 {rupiah(adtv20)}")
    if not accum:
        print("    none qualified")
    print(f"    {'brk':<5}{'name':<26}{'net 20d':>11}{'buy 20d':>11}{'sell 20d':>11}"
          f"{'osr20':>7}{'osr5':>7}{'run':>5}{'avg tkt':>9}")
    for code, r, net20 in accum[:8]:
        run = run_length(daily["brokers"].get(code, {}).get("daily", {}), d_1 or surge)
        print(f"    {code:<5}{r['name'][:25]:<26}{rupiah(net20):>11}"
              f"{rupiah(r['buy_value']):>11}{rupiah(r['sell_value']):>11}"
              f"{pct(r['osr'], 0):>7}{pct(w5.get(code, {}).get('osr'), 0):>7}"
              f"{run:>5}{rupiah(r['ats_buy']):>9}")

    # ---- the entry day
    if d_1 and book_d1:
        prev = prev_session(px_dates, d_1)
        chg = ((px[d_1]["close"] / px[prev]["close"] - 1)
               if prev and px[prev]["close"] else None)
        print(f"\n  ENTRY DAY {d_1}  (close {px[d_1]['close']:,.0f}, {pct(chg, 2)})"
              f"  — absorption on weakness?")
        ranked = sorted(book_d1.values(), key=lambda r: -r["net_value"])
        print(f"    {'brk':<5}{'net':>12}{'buy':>11}{'sell':>11}{'osr':>7}"
              f"{'bfreq':>8}{'avg tkt':>10}{'buy avg':>10}")
        for r in ranked[:4] + ranked[-3:]:
            code = next((k for k, v in book_d1.items() if v is r), "?")
            print(f"    {code:<5}{rupiah(r['net_value']):>12}"
                  f"{rupiah(r['buy_value']):>11}{rupiah(r['sell_value']):>11}"
                  f"{pct(r['osr'], 0):>7}{r['buy_freq']:>8,.0f}"
                  f"{rupiah(r['ats_buy']):>10}"
                  f"{(f'{r['buy_avg']:,.0f}' if r['buy_avg'] else '-'):>10}")
        absorbed = sorted((r for r in book_d1.values() if r["net_value"] < 0),
                          key=lambda r: r["net_value"])
        if accum and absorbed:
            soaked = sum(r["net_value"] for r in absorbed)
            got = sum(book_d1.get(cd, {}).get("net_value", 0) for cd, _, _ in accum)
            print(f"    absorb_pair: accumulators took {rupiah(got)} against "
                  f"{rupiah(abs(soaked))} of net distribution "
                  f"({pct(got / abs(soaked) if soaked else None, 0)})")

    # ---- the surge day / retail trap
    print(f"\n  SURGE DAY {surge}  — distribution?")
    trap = {}
    if accum and book_d:
        distributed = sum(book_d.get(cd, {}).get("sell_value", 0) for cd, _, _ in accum)
        # position_value per accumulation.md 5: net over the broker's own run, floored
        # at 5 sessions, ending D-1. Using a fixed window instead would understate a
        # long campaign and overstate a short one.
        position = 0.0
        for cd, _, _ in accum:
            dd = daily["brokers"].get(cd, {}).get("daily", {})
            w = max(5, run_length(dd, d_1 or surge))
            position += sum(v for k, v in dd.items() if k in set(pre_dates[-w:]))
        trap_rate = distributed / position if position else None
        net_sold = sum(r["net_value"] for r in book_d.values() if r["net_value"] < 0)
        still_buying = sum(1 for cd, _, _ in accum
                           if (book_d.get(cd, {}).get("net_value") or 0) > 0)
        trap = {"distributed": distributed, "position_d1": position,
                "trap_rate": trap_rate, "net_distribution": abs(net_sold),
                "accumulators_still_buying": still_buying, "n_accumulators": len(accum)}
        print(f"    position built through {d_1} (run-length window): {rupiah(position)}")
        print(f"    accumulators' SELL value on {surge}:              {rupiah(distributed)}")
        print(f"    trap_rate (sold / position built):                {pct(trap_rate, 1)}")
        print(f"    total net distribution by everyone on {surge}:    "
              f"{rupiah(abs(net_sold))}")
        for cd, _, _ in accum[:6]:
            r = book_d.get(cd)
            if not r:
                continue
            print(f"      {cd:<4} {pct(r['osr'], 0):>5} osr on D   "
                  f"net {rupiah(r['net_value']):>11}  sold {rupiah(r['sell_value']):>11}"
                  f"  bfreq {r['buy_freq']:>7,.0f}  tkt {rupiah(r['ats_buy'])}")
        # Tag per accumulation.md 5. retail_absorb needs the Sectors retail cohort and
        # is added in the board; here the two computable legs are reported plainly.
        if trap_rate is not None:
            tag = ("RETAIL TRAP (confirmed)" if trap_rate >= 0.35
                   else "PARTIAL DISTRIBUTION" if trap_rate >= 0.15
                   else f"MARKUP, WHALE STILL LONG "
                        f"({still_buying}/{len(accum)} still net buying on D)")
            trap["tag"] = tag
            print(f"    -> {tag}")
        # Who actually sold into the markup, if not the accumulators.
        sellers = sorted(book_d.items(), key=lambda kv: kv[1]["net_value"])[:3]
        print("    biggest distributors on D: " + ", ".join(
            f"{cd} {rupiah(r['net_value'])} (osr {pct(r['osr'], 0)})"
            for cd, r in sellers))

    # ---- intraday hand-over
    intr = pull_intraday(c, sym, surge) if not args.no_intraday else {}
    if intr:
        print(f"\n  INTRADAY {surge}  — when did they load, and did they turn?")
        for code, series in sorted(intr.items(),
                                   key=lambda kv: -abs(kv[1][-1][1]))[:6]:
            t, peak_x = turn_ratio(series)
            tag = "  <- TURNED" if (t or 0) >= 0.30 else ""
            print(f"    {code:<5} close-net {rupiah(series[-1][1]):>12}"
                  f"  peak {rupiah(max(v for _, v in series)):>12} at {peak_x}"
                  f"  turn {pct(t, 0):>6}{tag}")

    # ---- aggressor imbalance
    #
    # The design called for frag = freq_imbalance - val_imbalance, pairing scope="freq"
    # against scope="val". That is NOT COMPUTABLE: momentum-chart's scope enum is
    # value|volume only, and `freq` 422s (see invezgo_client.momentum_chart). Trade
    # counts exist per broker-day and per print, never in an intraday bucket. The tape
    # below covers it at second resolution instead, which is finer than the 5-minute
    # bucket would have been.
    aggr = None
    if not args.no_intraday:
        mv = rows_of(c.momentum_chart(sym, date=surge, range_=5, scope="value"))
        if mv:
            b = f(mv[-1].get("buy")) or 0.0
            s = f(mv[-1].get("sell")) or 0.0
            if b + s:
                aggr = (b - s) / (b + s)
                print(f"\n  AGGRESSOR SPLIT {surge}: buy-initiated {rupiah(b)}, "
                      f"sell-initiated {rupiah(s)}, imbalance {aggr:+.3f}")

    # ---- tape microstructure
    tape_out = {}
    if not args.no_tape:
        for d in [x for x in (d_1, surge) if x][-args.tape_days:]:
            raw = c.running_trade_all(sym, date=d, max_pages=args.tape_pages,
                                      limit=150, market="RG",
                                      cached_only=args.tape_cached_only)
            prints = tape_lib.parse_prints(raw)
            if not prints:
                continue
            agg = tape_lib.aggregate_by_broker(prints)
            sweeps = tape_lib.sweep_events(prints)
            dom = tape_lib.dom_flow(prints)
            print(f"\n  TAPE {d}  — {len(prints):,} prints, VWAP "
                  f"{tape_lib.session_vwap(prints):,.1f}")
            print(f"    client domicile: F net {rupiah(dom['F']['net'])}   "
                  f"D net {rupiah(dom['D']['net'])}")
            print(f"    {'brk':<5}{'side':<6}{'prints':>8}{'1s max':>7}"
                  f"{'passive':>9}{'clip unif':>11}{'pin':>7}{'slice_z':>9}"
                  f"  behaviour")
            focus = [cd for cd, _, _ in accum[:3]] + [
                r[0] for r in sorted(agg.items(),
                                     key=lambda kv: kv[1]["sell_value"],
                                     reverse=True)[:2]]
            seen = set()
            for code in focus:
                if code in seen or code not in agg:
                    continue
                seen.add(code)
                for side in ("buy", "sell"):
                    bs = tape_lib.burst_stats(prints, code, side)
                    if bs["n_prints"] < 20:
                        continue
                    cs = tape_lib.clip_stats(prints, code, side)
                    sz = tape_lib.slice_z(agg, code, side)
                    ag = tape_lib.aggression_stats(prints, code, side)
                    beh = tape_lib.classify_behaviour(prints, code, side, agg)
                    print(f"    {code:<5}{side:<6}{bs['n_prints']:>8,}"
                          f"{bs['burst_max_1s']:>7}"
                          f"{pct(ag['passive_pct'], 0):>9}"
                          f"{pct(cs['clip_uniformity'], 0):>11}"
                          f"{pct(cs['price_pin'], 0):>7}"
                          f"{(f'{sz:+.2f}' if sz is not None else '-'):>9}"
                          f"  {beh}")
            if sweeps:
                print(f"    biggest one-second sweeps:")
                for e in sweeps[:4]:
                    xs = f", {e['self_cross']} self-cross" if e["self_cross"] else ""
                    print(f"      {e['time']}  {e['broker']} {e['side']:<4} "
                          f"{e['n_prints']:>3} prints  {rupiah(e['value']):>10}  "
                          f"{e['n_counterparties']} counterparties, "
                          f"{e['n_prices']} price(s){xs}")
            tape_out[d] = {
                "n_prints": len(prints),
                "vwap": tape_lib.session_vwap(prints),
                "dom_flow": dom,
                "top_sweeps": sweeps[:5],
                "by_broker": {k: v for k, v in agg.items()
                              if k in seen},
            }

    return {
        "symbol": sym, "surge_date": surge, "entry_day": d_1,
        "adtv20": adtv20,
        "prices": prices[-8:],
        "accumulators": [{"code": cd, "name": r["name"], "net_20d": n20,
                          "osr20": r["osr"], "osr5": w5.get(cd, {}).get("osr"),
                          "buy_value": r["buy_value"], "sell_value": r["sell_value"],
                          "ats_buy": r["ats_buy"]}
                         for cd, r, n20 in accum[:8]],
        "book_entry_day": book_d1, "book_surge_day": book_d,
        "retail_trap": trap, "aggressor_imbalance": aggr,
        "intraday_turn": {k: turn_ratio(v)[0] for k, v in intr.items()},
        "tape": tape_out,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--surge-date", default=SURGE)
    ap.add_argument("--no-tape", action="store_true",
                    help="skip running-trade (the expensive pull)")
    ap.add_argument("--no-intraday", action="store_true")
    ap.add_argument("--tape-days", type=int, default=2,
                    help="how many of (entry day, surge day) to pull tape for")
    ap.add_argument("--tape-pages", type=int, default=200,
                    help="hard per-stock-day page ceiling for running_trade_all")
    ap.add_argument("--tape-cached-only", action="store_true",
                    help="use tape only where it is already on disk; never pull more")
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    c = InvezgoClient(date=args.surge_date)
    if not c.enabled:
        print("INVEZGO_API_KEY not set — nothing to do", file=sys.stderr)
        return 1

    print(f"case study: {', '.join(syms)}  surge={args.surge_date}"
          f"  tape={'off' if args.no_tape else f'on ({args.tape_days}d)'}")

    results = []
    for sym in syms:
        try:
            results.append(analyse(c, sym, args.surge_date, args))
        except Exception as e:  # one bad symbol must not lose the other three
            print(f"  {sym}: FAILED — {type(e).__name__}: {e}", file=sys.stderr)
            results.append({"symbol": sym, "error": f"{type(e).__name__}: {e}"})

    print(f"\n{'=' * 78}")
    c.report()

    BUILD.mkdir(exist_ok=True)
    out = BUILD / f"case-study-{args.surge_date}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now(WIB).isoformat(),
        "surge_date": args.surge_date,
        "symbols": syms,
        "invezgo_requests": c.requests_used,
        "results": results,
    }, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
