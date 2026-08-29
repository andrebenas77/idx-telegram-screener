#!/usr/bin/env python3
"""Why is a given name NOT on the momentum board? And which names nearly are?

The board prints what qualified. It never prints what did not, so "why isn't MDKA there?" has
until now been answered by reading `build_momentum_board.build()` and re-deriving the arithmetic
by hand. That is slow and it is where mistakes get made, because the gate is TWO INDEPENDENT
LEGS and the board output shows neither of them failing:

  LEG 1  broker accumulation -- some broker's net >= Rp500m AND >= 10% of ADTV AND net > 0 on
         at least 2 of the last 3 sessions
  LEG 2  is_momentum -- rvol5 in [1.5, 3.0) AND dd60 >= -0.10 AND rsi >= 55

Both must pass. Most names fail exactly one, and WHICH one is the whole answer: a name failing
leg 1 has no evidence anyone is buying it, while a name failing leg 2 on rvol5 alone may have a
large, genuine accumulation event and simply lack participation. Those are opposite situations
and the board renders them identically, as absence.

The gate is IMPORTED from `build_momentum_board` and `momentum_setup`, never copied. A second
copy of a threshold in this repo is a copy that will silently disagree with the board after the
next tuning, and then this script would confidently explain a rule that is not the one running.

`--near-miss` is the mode worth having. It lists names that clear the accumulation leg and miss
`is_momentum` by ONE condition, with the distance to the threshold -- the set that could qualify
on the next session without anything unusual happening. Read it as a watch list, not a signal:
nothing here has been validated except the gate itself, and a name that has not passed the gate
has not earned the gate's evidence.

Usage:
    py scripts/momentum_why.py MDKA
    py scripts/momentum_why.py MDKA ICBP ESSA --trajectory 8
    py scripts/momentum_why.py --near-miss
    py scripts/momentum_why.py --near-miss --date 2026-08-24
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_momentum_board as B  # noqa: E402  (the live gate constants, not a copy)
from alpha_lib import Panel  # noqa: E402
from momentum_setup import is_momentum  # noqa: E402
from overlay_test import features  # noqa: E402

MIN_COVERAGE = 0.50


def coverage(p: Panel, i: int) -> float | None:
    """Share of panel symbols carrying a bar on session i.

    Checked because a thin session makes every name look like it failed the gate when it in fact
    has no data. `momentum_board.json` once reported no candidates for days on a session holding
    2 bars of 161, and an absence of data reads exactly like an absence of opportunity.
    """
    if not p.close:
        return None
    return sum(1 for s in p.close if i in p.close[s]) / len(p.close)


def leg2(f: dict) -> tuple[bool, list, str]:
    """(passes, failing conditions with their values, status word).

    EXHAUSTION is reported separately from a plain failure. Above rvol5 3.0 the board treats
    the setup as an avoid rather than a candidate, and calling it "just misses" would invite
    exactly the wrong action.

    The magnitude is unsettled and the report says so rather than picking a number. The README
    quotes -1.85% (3d) / -3.82% (5d) with a 19% 5d hit rate; re-measured on the rebuilt
    159-name panel on 2026-08-29 the same cut gives a POSITIVE +0.81% (5d) on n=197 with a
    39% hit rate. The direction of the advice survives either way -- a 39% hit rate against
    ~50% inside the band is a distribution carried by a few outliers -- but the published
    figures predate the 112 -> 159 panel rebuild and have not been re-derived.
    """
    fails = []
    if f["rvol5"] < B.RVOL_MIN:
        fails.append(("rvol5", f["rvol5"], B.RVOL_MIN, B.RVOL_MIN - f["rvol5"]))
    elif f["rvol5"] >= B.RVOL_MAX:
        fails.append(("rvol5", f["rvol5"], B.RVOL_MAX, f["rvol5"] - B.RVOL_MAX))
    if f["dd60"] < B.DD_MIN:
        fails.append(("dd60", f["dd60"], B.DD_MIN, B.DD_MIN - f["dd60"]))
    if f["rsi"] < B.RSI_MIN:
        fails.append(("rsi", f["rsi"], B.RSI_MIN, B.RSI_MIN - f["rsi"]))
    if not fails:
        return True, [], "PASS"
    if f["rvol5"] >= B.EXHAUST_RVOL:
        return False, fails, "EXHAUSTION - avoid, the edge inverts above RVOL 3.0"
    return False, fails, "fail"


def leg1(p: Panel, sym: str, i: int):
    """(best qualifying broker or None, every broker net-buying today, sorted by size)."""
    a = (p.adtv.get(sym) or {}).get(i)
    rows, best = [], None
    for (s2, broker), series in p.flows.items():
        if s2 != sym:
            continue
        by = dict(series)
        net = by.get(i)
        if net is None or net <= 0:
            continue
        persist = sum(1 for j in range(i - 2, i + 1) if by.get(j, 0) > 0)
        pct = (100.0 * net / a) if a else 0.0
        ok = bool(net >= B.MIN_VALUE and a and net >= (B.MIN_ADTV_PCT / 100.0) * a
                  and persist >= 2)
        rows.append({"broker": broker, "net": net, "pct": pct, "persist": persist, "ok": ok})
        if ok and (best is None or net > best["net"]):
            best = rows[-1]
    rows.sort(key=lambda r: -r["net"])
    return best, rows


def why_missing(best, rows) -> str:
    """Name the binding constraint on the accumulation leg, rather than just saying it failed."""
    if not rows:
        return "no broker was a net buyer this session"
    top = rows[0]
    bits = []
    if top["net"] < B.MIN_VALUE:
        bits.append("largest net Rp%.0fm is below the Rp%.0fm floor"
                    % (top["net"] / 1e6, B.MIN_VALUE / 1e6))
    if top["pct"] < B.MIN_ADTV_PCT:
        bits.append("%.1f%% of ADTV is below %.0f%%" % (top["pct"], B.MIN_ADTV_PCT))
    if top["persist"] < 2:
        bits.append("net positive on only %d of the last 3 sessions" % top["persist"])
    return "%s: %s" % (top["broker"], "; ".join(bits) or "no single broker cleared every test")


def report(p: Panel, sym: str, i: int, session: str, traj: int) -> None:
    f = features(p, sym, i)
    print("=" * 78)
    print("%s   session %s" % (sym, session))
    print("=" * 78)
    if not f or f.get("rvol5") is None:
        print("  no features -- fewer than 120 sessions of history, or no bar on this date.")
        print("  That is a DATA gap, not a failed gate.")
        return

    ok2, fails, status = leg2(f)
    best, rows = leg1(p, sym, i)

    print("  LEG 1  broker accumulation           %s" % ("PASS" if best else "FAIL"))
    if best:
        print("           %s net Rp%.2fb = %.0f%% of ADTV, positive %d of last 3"
              % (best["broker"], best["net"] / 1e9, best["pct"], best["persist"]))
    else:
        print("           %s" % why_missing(best, rows))
    for r in rows[:4]:
        print("             %-4s Rp%9.2fb  %6.1f%% ADTV  %d/3  %s"
              % (r["broker"], r["net"] / 1e9, r["pct"], r["persist"],
                 "qualifies" if r["ok"] else ""))

    print("  LEG 2  is_momentum                   %s" % status)
    print("           rvol5 %6.3f  (needs %.1f to %.1f)" % (f["rvol5"], B.RVOL_MIN, B.RVOL_MAX))
    print("           dd60  %+6.3f  (needs >= %+.2f)" % (f["dd60"], B.DD_MIN))
    print("           rsi   %6.1f  (needs >= %.0f)" % (f["rsi"], B.RSI_MIN))
    for name, val, need, gap in fails:
        print("           -> %s misses by %.3f" % (name, gap))

    # Exhaustion is hoisted above every other reading. A name at RVOL >= 3.0 returned
    # -1.85% (3d) / -3.82% (5d) with a 19% 5d hit rate on this rule's own history, so the
    # actionable fact about it is "avoid", not "fails two legs". Reporting it as an ordinary
    # miss buries a warning underneath a technicality.
    if f["rvol5"] >= B.EXHAUST_RVOL:
        print("  VERDICT  EXHAUSTION -- avoid. RVOL %.2f is above the %.1f ceiling. The board "
              "puts these on the" % (f["rvol5"], B.EXHAUST_RVOL))
        print("           avoid list, not the candidate list. Accumulation %s and does not "
              "help here." % ("is present" if best else "is absent"))
        print("           Figures: README quotes -1.85% 3d / -3.82% 5d / 19% hit. Re-measured "
              "on the")
        print("           current 159-name panel 2026-08-29 the mean is POSITIVE (+0.81% 5d, "
              "n=197)")
        print("           but the hit rate falls to 39% against ~50% inside the band -- a few "
              "large")
        print("           winners over a mostly-losing distribution. The avoid stands; the "
              "quoted")
        print("           numbers need re-deriving on the rebuilt panel.")
    elif best and ok2:
        print("  VERDICT  ON THE BOARD")
    elif best and not ok2:
        print("  VERDICT  not on the board -- accumulation is present; what is missing is %s"
              % ", ".join(x[0] for x in fails))
    elif ok2 and not best:
        print("  VERDICT  not on the board -- the price setup qualifies but nobody is "
              "accumulating it")
    else:
        print("  VERDICT  not on the board -- both legs fail (%s; accumulation absent)"
              % ", ".join(x[0] for x in fails))

    if traj:
        print("\n  trajectory into the session:")
        for j in range(max(0, i - traj + 1), i + 1):
            ff = features(p, sym, j)
            if not ff or ff.get("rvol5") is None:
                continue
            b2, _ = leg1(p, sym, j)
            o2, _, _ = leg2(ff)
            print("     %s  rvol5 %6.3f  rsi %3.0f  dd60 %+6.3f   %s%s"
                  % (p.dates[j], ff["rvol5"], ff["rsi"], ff["dd60"],
                     "accum " if b2 else "      ", "momentum" if o2 else ""))
    print()


def near_miss(p: Panel, i: int, session: str, limit: int) -> None:
    """Names clearing the accumulation leg and missing is_momentum by exactly one condition."""
    out = []
    for sym in sorted(p.close):
        f = features(p, sym, i)
        if not f or f.get("rvol5") is None:
            continue
        best, _ = leg1(p, sym, i)
        if not best:
            continue
        ok2, fails, status = leg2(f)
        if ok2 or len(fails) != 1:
            continue
        name, val, need, gap = fails[0]
        # Exhaustion is not a near miss. It is the other side of the band, where the measured
        # edge is negative, and listing it beside "almost qualifies" would be actively wrong.
        if f["rvol5"] >= B.EXHAUST_RVOL:
            continue
        out.append((gap, sym, name, val, need, f, best))
    out.sort(key=lambda r: r[0])

    print("=" * 92)
    print("NEAR MISSES -- accumulation leg PASSES, is_momentum misses by one condition")
    print("session %s" % session)
    print("=" * 92)
    if not out:
        print("  none this session")
        return
    print("%-6s %-22s %8s %8s %6s  %s" % ("sym", "missing", "rvol5", "dd60", "rsi", "accumulator"))
    print("-" * 92)
    for gap, sym, name, val, need, f, best in out[:limit]:
        print("%-6s %-22s %8.3f %+8.3f %6.0f  %s Rp%.2fb %.0f%% ADTV"
              % (sym, "%s %.3f, needs %.2f" % (name, val, need), f["rvol5"], f["dd60"],
                 f["rsi"], best["broker"], best["net"] / 1e9, best["pct"]))
    print()
    print("  These have NOT passed the gate and carry none of its evidence. The validated")
    print("  result is +0.93pp (3d) / +1.37pp (5d) for names that DID pass; a near miss is a")
    print("  reason to look tomorrow, not a discounted version of the signal.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", help="tickers to explain")
    ap.add_argument("--date", default=None, help="session to score (default: last in the panel)")
    ap.add_argument("--near-miss", action="store_true",
                    help="list names clearing accumulation and missing is_momentum by one")
    ap.add_argument("--trajectory", type=int, default=0,
                    help="also show the last N sessions for each named symbol")
    ap.add_argument("--limit", type=int, default=25)
    a = ap.parse_args()
    if not a.symbols and not a.near_miss:
        ap.error("give at least one symbol, or --near-miss")

    p = Panel()
    p.load_prices()
    p.load_flows()

    session = a.date or p.dates[-1]
    i = p.didx.get(session)
    if i is None:
        print("no such session in the panel: %s (panel runs %s .. %s)"
              % (session, p.dates[0], p.dates[-1]))
        return 2
    cov = coverage(p, i)
    if cov is not None and cov < MIN_COVERAGE:
        print("[!!] session %s carries only %.0f%% of panel symbols. Below the %.0f%% floor "
              "every answer below is about missing DATA, not a failed gate."
              % (session, 100 * cov, 100 * MIN_COVERAGE))

    print("gate: LEG 1 net >= Rp%.0fm AND >= %.0f%% of ADTV AND net > 0 on >= 2 of last 3"
          % (B.MIN_VALUE / 1e6, B.MIN_ADTV_PCT))
    print("      LEG 2 rvol5 in [%.1f, %.1f)  dd60 >= %+.2f  rsi >= %.0f   (exhaustion >= %.1f)"
          % (B.RVOL_MIN, B.RVOL_MAX, B.DD_MIN, B.RSI_MIN, B.EXHAUST_RVOL))
    print("panel %s .. %s, session %s, coverage %.0f%%\n"
          % (p.dates[0], p.dates[-1], session, 100 * (cov or 0)))

    for sym in a.symbols:
        sym = sym.strip().upper().removesuffix(".JK")
        if sym not in p.close:
            print("=" * 78)
            print("%s is not in the panel universe (%d names). Nothing can be said about it."
                  % (sym, len(p.close)))
            print()
            continue
        report(p, sym, i, session, a.trajectory)

    if a.near_miss:
        near_miss(p, i, session, a.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
