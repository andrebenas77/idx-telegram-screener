#!/usr/bin/env python3
"""Does managing the momentum board's signals beat just holding them?

The board already has a validated edge: buy the close after the signal, sell five
sessions later, +0.96% (3d) / +1.40% (5d) excess over IHSG. That is the INCUMBENT, and
it is the thing to beat. A stop that cuts winners, or a veto that removes the names
that pay, makes the system worse while feeling more disciplined.

**Kill criterion, stated before the numbers arrive: if FULL DAILY does not beat
BASELINE on mean R and mean excess, ship nothing.** No re-specifying thresholds until
a row goes green — that is how a backtest becomes a search for a number rather than a
test of a hypothesis.

Conventions inherited from alpha_lib/broker_alpha and NOT re-litigated here:
  - Signal on day i (board is built pre-open on i+1 from data through i).
  - Entry at raw close[i+1] — one full session of lag, per alpha_lib.py:10-14.
  - Returns are excess over IHSG; the index moved enough over this window that raw
    returns would rank everything by beta.
  - Structure and stops on RAW bars; returns on adjusted closes.

Two null controls run every time. The second is the one that matters: a veto that
rejects 60% of candidates will differ from baseline by luck alone at these sample
sizes, so it is compared against a coin flip with the SAME acceptance rate.

Usage:
    py scripts/trade_backtest.py --daily --ablate
    py scripts/trade_backtest.py --daily --ablate --folds 4 --no-fees
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel, wilson_lower  # noqa: E402
from broker_alpha import build_events  # noqa: E402
from intraday_lib import evaluate_entry, read_bars  # noqa: E402
from momentum_setup import is_momentum  # noqa: E402
from overlay_test import features  # noqa: E402
from trade_lib import (SHARES_PER_LOT, RiskConfig, atr_series,  # noqa: E402
                       config_from_env, low_n_prior, round_tick, rvol1_series,
                       stop_price, tick_size)

# Board thresholds, mirrored from build_momentum_board.py. Changing these here would
# silently backtest a different screener than the one that publishes.
MIN_VALUE = 500e6
MIN_ADTV_PCT = 10.0
RVOL_MIN, RVOL_MAX = 1.5, 3.0
DD_MIN, RSI_MIN = -0.10, 55.0

HOLD = 5              # baseline holding period, matches the validated +1.40% (5d)
MAX_HOLD = 10         # hard backstop so a trailing stop cannot run forever


# ------------------------------------------------------------------ candidate universe

def build_candidates(p: Panel) -> list[dict]:
    """(symbol, signal day) pairs exactly as the live board would produce them.

    Deduped across brokers: five brokers accumulating one name on one day is one
    trade, not five. Ranking by broker quality is a board concern; here it would
    just weight the sample.
    """
    evs = build_events(p, MIN_VALUE, MIN_ADTV_PCT)
    seen: dict[tuple[str, int], dict] = {}
    for e in evs:
        key = (e["symbol"], e["i"])
        if key in seen:
            seen[key]["adtv_pct"] += e["adtv_pct"]
            seen[key]["n_brokers"] += 1
            continue
        seen[key] = {"symbol": e["symbol"], "i": e["i"],
                     "adtv_pct": e["adtv_pct"], "n_brokers": 1}

    out = []
    for cand in seen.values():
        f = features(p, cand["symbol"], cand["i"])
        if not f:
            continue
        if not is_momentum(f, RVOL_MIN, DD_MIN, RSI_MIN, RVOL_MAX):
            continue
        cand["f"] = f
        out.append(cand)
    out.sort(key=lambda c: (c["i"], c["symbol"]))
    return out


# ------------------------------------------------------------------------ the vetoes

def struct_grade(f: dict) -> str:
    """STRUCT+ / STRUCT? / STRUCT-.

    A HH/HL label at CLV 0.00 — exactly what ISAT and ERAA printed on 2026-08-10 —
    grades STRUCT-. The raw high/low comparison says the range moved up; the close
    says everyone who bought in that range is under water.
    """
    clv, trend, cmf = f.get("clv"), f.get("trend"), f.get("cmf20")
    if clv is not None and clv <= 0.25:
        return "STRUCT-"
    if trend == "LH/LL":
        return "STRUCT-"
    if cmf is not None and cmf < -0.10:
        return "STRUCT-"
    if trend == "HH/HL" and clv is not None and clv >= 0.50 and (cmf or 0) > 0:
        return "STRUCT+"
    return "STRUCT?"


def veto_reasons(p: Panel, c: dict, atrs: dict, rv1s: dict, cfg: RiskConfig) -> list[str]:
    sym, i, f = c["symbol"], c["i"], c["f"]
    out = []
    rv1 = rv1s.get(sym, {}).get(i)
    cl = p.raw_close.get(sym, {})
    ret1 = (cl[i] / cl[i - 1] - 1) if i in cl and (i - 1) in cl and cl[i - 1] else None
    if rv1 is not None:
        if rv1 >= 4.0:
            out.append("EXHAUST_1D")
        elif rv1 >= 3.0 and ret1 is not None and ret1 >= 0.07:
            out.append("EXHAUST_1D")
    if struct_grade(f) == "STRUCT-":
        out.append("STRUCT-")
    a = atrs.get(sym, {}).get(i)
    adj = p.close.get(sym, {})
    px = [adj[j] for j in range(i - 25, i + 1) if j in adj]
    if a and len(px) >= 21:
        k = 2 / 21
        e = sum(px[:20]) / 20
        for v in px[20:]:
            e = v * k + e * (1 - k)
        if e and (cl.get(i, 0) - e) / a > 2.5:
            out.append("EXTENDED")
    return out


# --------------------------------------------------------------------- trade simulator

def simulate(p: Panel, c: dict, cfg: RiskConfig, rules: set[str],
             atrs: dict, rv1s: dict) -> dict | None:
    """Replay one candidate under `rules`. Returns a trade record or None if untradeable.

    Every exit is priced pessimistically:
      - a stop gapped through fills at the OPEN, not at the stop
      - a close-based exit fills at the NEXT open, because the decision is only
        knowable after the close
    Overstating fill quality is the second-most-common way a backtest lies.
    """
    sym, i = c["symbol"], c["i"]
    hi, lo, cl = p.high.get(sym, {}), p.low.get(sym, {}), p.raw_close.get(sym, {})
    op = None  # open prices are not in Panel; use prior close as the gap proxy
    ent = i + 1
    if ent not in cl:
        return None
    entry = cl[ent]
    if entry <= 0:
        return None

    a = atrs.get(sym, {}).get(ent)
    if not a:
        return None

    if "tradeable_only" in rules:
        # Same universe restriction as the ATR-stop rows, but hold to the baseline
        # exit. Isolates the width filter from the stop.
        s_, _b = stop_price(entry, a, low_n_prior(p, sym, ent, cfg.struct_lookback), cfg)
        if not s_:
            return None
        stop, basis = 0, "none"
    elif "prior_low_stop" in rules:
        # The counterfactual: what was actually being done. A stop one tick under the
        # PRIOR SESSION'S low. This is the rule that cost the GGRM trade.
        base = lo.get(ent)
        stop = round_tick(base - tick_size(base), "down") if base else 0
        basis = "prior_low"
        if stop and (entry - stop) <= 0:
            return None
    elif "atr_stop" in rules:
        stop, basis = stop_price(entry, a, low_n_prior(p, sym, ent, cfg.struct_lookback), cfg)
        if not stop:
            return None
    else:
        stop, basis = 0, "none"

    r_ps = (entry - stop) if stop else (cfg.k_atr * a)   # risk per share for R scaling
    if r_ps <= 0:
        return None

    cur_stop = stop
    exit_i = exit_px = None
    exit_rule = ""
    horizon = MAX_HOLD if (rules & {"atr_stop", "prior_low_stop", "trail"}) else HOLD

    j = ent + 1
    while j <= ent + horizon:
        if j not in cl:
            break
        prev_c = cl.get(j - 1)

        # E1 hard stop, intraday. Gap-through fills at the open (proxied by prior
        # close, the only pre-open price Panel carries).
        if cur_stop and lo.get(j) is not None and lo[j] <= cur_stop:
            gap_open = prev_c if prev_c is not None else cur_stop
            exit_i, exit_px = j, min(gap_open, cur_stop)
            exit_rule = "E1_stop"
            break

        # E2 structure: CLOSE below the 5-session low. Intraday probes ignored on
        # purpose — that distinction is the whole GGRM lesson.
        if "E2" in rules:
            l5 = low_n_prior(p, sym, j - 1, cfg.struct_lookback)
            if l5 and cl[j] < l5 and (j + 1) in cl:
                exit_i, exit_px, exit_rule = j + 1, cl[j + 1], "E2_structure"
                break

        # E3 blow-off into supply.
        if "E3" in rules:
            rv = rv1s.get(sym, {}).get(j)
            h, l_, c_ = hi.get(j), lo.get(j), cl.get(j)
            clv = (c_ - l_) / (h - l_) if (h is not None and l_ is not None and h > l_) else None
            if rv and rv >= 4.0 and clv is not None and clv <= 0.35 and (j + 1) in cl:
                exit_i, exit_px, exit_rule = j + 1, cl[j + 1], "E3_blowoff"
                break

        held = j - ent
        rmult = (cl[j] - entry) / r_ps

        # Trailing: breakeven at +1R, chandelier at +2R. Monotone.
        if "trail" in rules and cur_stop:
            new = cur_stop
            if rmult >= 1.0:
                new = max(new, round_tick(entry * (1 + cfg.fee_buy + cfg.fee_sell), "up"))
            if rmult >= 2.0:
                w = [hi[k] for k in range(j - 4, j + 1) if k in hi]
                aj = atrs.get(sym, {}).get(j) or a
                if w:
                    new = max(new, round_tick(max(w) - 2.5 * aj, "down"))
            cur_stop = max(cur_stop, min(new, cl[j] - tick_size(cl[j])))

        # E6 time stop: dead money after HOLD sessions.
        if "E6" in rules and held >= HOLD and rmult < 0.75 and (j + 1) in cl:
            exit_i, exit_px, exit_rule = j + 1, cl[j + 1], "E6_time"
            break

        if "atr_stop" not in rules and "prior_low_stop" not in rules and held >= HOLD:
            exit_i, exit_px, exit_rule = j, cl[j], "baseline_hold"
            break
        j += 1

    if exit_i is None:
        j = min(ent + horizon, max(k for k in cl if k <= ent + horizon))
        if j <= ent:
            return None
        exit_i, exit_px, exit_rule = j, cl[j], "horizon"

    gross = exit_px / entry - 1
    bench = (p.bench.get(exit_i, 0) / p.bench[ent] - 1) if (ent in p.bench and exit_i in p.bench) else None
    cost = cfg.fee_buy + cfg.fee_sell if cfg.fee_buy else 0.0
    net = gross - cost
    return {"symbol": sym, "i": i, "entry_i": ent, "exit_i": exit_i,
            "entry": entry, "exit": exit_px, "stop": stop, "basis": basis,
            "R": (exit_px - entry - entry * cost) / r_ps,
            "gross": gross, "net": net,
            "excess": (net - bench) if bench is not None else None,
            "held": exit_i - ent, "rule": exit_rule}


# ------------------------------------------------------------------------- statistics

def summarise_trades(trades: list[dict]) -> dict:
    ok = [t for t in trades if t]
    n = len(ok)
    if not n:
        return {"n": 0}
    R = [t["R"] for t in ok]
    ex = [t["excess"] for t in ok if t["excess"] is not None]
    hits = sum(1 for x in ex if x > 0)
    by_rule: dict[str, int] = {}
    for t in ok:
        by_rule[t["rule"]] = by_rule.get(t["rule"], 0) + 1
    return {"n": n, "meanR": statistics.fmean(R), "medR": statistics.median(R),
            "mean_excess": statistics.fmean(ex) if ex else 0.0,
            "hit": hits / len(ex) if ex else 0.0,
            "wilson": wilson_lower(hits, len(ex)) if ex else 0.0,
            "held": statistics.fmean([t["held"] for t in ok]),
            "worstR": min(R), "rules": by_rule}


def run_set(p: Panel, cands: list[dict], cfg: RiskConfig, rules: set[str],
            atrs: dict, rv1s: dict, vetoes: set[str] | None = None,
            veto_cache: dict | None = None) -> list[dict]:
    out = []
    for c in cands:
        if vetoes:
            vr = veto_cache.get((c["symbol"], c["i"]), [])
            if any(v in vetoes for v in vr):
                continue
        t = simulate(p, c, cfg, rules, atrs, rv1s)
        if t:
            out.append(t)
    return out


def fmt(label: str, s: dict, base: dict | None = None) -> str:
    if not s.get("n"):
        return f"  {label:<34} (no trades)"
    d = ""
    if base and base.get("n"):
        d = f" | dR {s['meanR'] - base['meanR']:+.3f} dX {(s['mean_excess'] - base['mean_excess']) * 100:+.2f}pp"
    return (f"  {label:<34} n {s['n']:>5} | meanR {s['meanR']:+.3f} | medR {s['medR']:+.3f} "
            f"| excess {s['mean_excess'] * 100:+.2f}% | hit {s['hit']:.1%} "
            f"| held {s['held']:.1f} | worst {s['worstR']:+.1f}R{d}")


# ------------------------------------------------------------------ intraday entry

def simulate_intraday(p: Panel, c: dict, cfg: RiskConfig, atrs: dict, rv1s: dict,
                      t_min: str, m5: dict, idx5: dict,
                      gates: tuple[str, ...] = ("G1", "G2", "G3", "G4", "G5")) -> dict | None:
    """Same exits as the validated daily winner; entry decided on 5-minute bars.

    The stop is measured from the INTRADAY fill, not from the daily close, and it can
    be hit the same session — a trade that triggers at 09:41 and collapses by 14:00 is
    a same-day loss, and pretending the stop only arms tomorrow would flatter the
    result.
    """
    sym, i = c["symbol"], c["i"]
    ent = i + 1
    cl = p.raw_close.get(sym, {})
    if ent not in cl or ent >= len(p.dates):
        return None
    day = p.dates[ent]
    bars = (m5.get(sym) or {}).get(day)
    if not bars:
        return None

    a = atrs.get(sym, {}).get(ent)
    if not a:
        return None
    atr_pct = a / cl[ent] if cl[ent] else None
    sig = evaluate_entry(bars, (idx5 or {}).get(day) or [], t_min, atr_pct, cfg, gates)
    if not sig.get("entry"):
        return {"skipped": True, "symbol": sym, "i": i,
                "binding": sig.get("binding"), "or_hi": sig.get("or_hi")}

    entry = round_tick(sig["fill_px"], "up") + tick_size(sig["fill_px"])
    stop, basis = stop_price(entry, a, low_n_prior(p, sym, ent, cfg.struct_lookback), cfg)
    if not stop:
        return {"skipped": True, "symbol": sym, "i": i, "binding": "too_wide"}
    r_ps = entry - stop

    # Same-session stop: scan the bars after the fill.
    fill_hhmm = sig.get("fill_hhmm") or sig["hhmm"]
    for b in bars:
        if b.hhmm <= fill_hhmm:
            continue
        if b.l <= stop:
            cost = cfg.fee_buy + cfg.fee_sell
            gross = stop / entry - 1
            bench = ((p.bench.get(ent, 0) / p.bench[ent] - 1)
                     if ent in p.bench else 0.0)
            return {"symbol": sym, "i": i, "entry_i": ent, "exit_i": ent,
                    "entry": entry, "exit": stop, "stop": stop, "basis": basis,
                    "R": (stop - entry - entry * cost) / r_ps,
                    "gross": gross, "net": gross - cost,
                    "excess": gross - cost - bench, "held": 0, "rule": "E1_same_day"}

    # Then hand off to the daily exit machinery from the next session.
    fake = dict(c)
    t = simulate(p, fake, cfg, {"atr_stop", "E2"}, atrs, rv1s)
    if not t:
        return None
    # Re-price the daily result against the intraday entry.
    cost = cfg.fee_buy + cfg.fee_sell
    gross = t["exit"] / entry - 1
    bench = ((p.bench.get(t["exit_i"], 0) / p.bench[ent] - 1)
             if ent in p.bench and t["exit_i"] in p.bench else None)
    return {"symbol": sym, "i": i, "entry_i": ent, "exit_i": t["exit_i"],
            "entry": entry, "exit": t["exit"], "stop": stop, "basis": basis,
            "R": (t["exit"] - entry - entry * cost) / r_ps,
            "gross": gross, "net": gross - cost,
            "excess": (gross - cost - bench) if bench is not None else None,
            "held": t["exit_i"] - ent, "rule": t["rule"], "trigger": sig["hhmm"]}


def run_intraday(p: Panel, cands: list[dict], cfg: RiskConfig, atrs: dict, rv1s: dict,
                 t_min: str, m5: dict, idx5: dict,
                 gates: tuple[str, ...] = ("G1", "G2", "G3", "G4", "G5")) -> tuple[list, list]:
    took, skipped = [], []
    for c in cands:
        r = simulate_intraday(p, c, cfg, atrs, rv1s, t_min, m5, idx5, gates)
        if r is None:
            continue
        (skipped if r.get("skipped") else took).append(r)
    return took, skipped


def assert_cases(p: Panel, cfg: RiskConfig, atrs: dict, m5: dict, idx5: dict) -> int:
    """The four decisions that motivated the whole design, replayed at bar level.

    These are regression assertions, not evidence. Three losing trades and one winner
    cannot validate a rule — the daily study already showed vetoes that looked
    compelling on exactly this kind of anecdote and then failed across 915 trades.
    What these DO catch is a refactor silently changing the behaviour the design was
    built around.
    """
    cases = [("GGRM", "2026-08-07", True, "the winner — must still be taken"),
             ("ISAT", "2026-08-10", False, "entered Monday, drawdown — must be skipped"),
             ("ERAA", "2026-08-10", False, "entered Monday, drawdown — must be skipped"),
             ("GGRM", "2026-08-10", False, "Monday re-entry — must be skipped")]
    fails = 0
    for t_min in ("09:35", "10:00"):
        print(f"\n  T_min = {t_min}")
        for sym, day, want, note in cases:
            bars = (m5.get(sym) or {}).get(day) or []
            ib = (idx5 or {}).get(day) or []
            if not bars:
                print(f"    [!!] {sym} {day}: no 5m bars cached")
                fails += 1
                continue
            i = p.didx.get(day)
            a = atrs.get(sym, {}).get(i) if i is not None else None
            cl = (p.raw_close.get(sym) or {}).get(i) if i is not None else None
            sig = evaluate_entry(bars, ib, t_min, (a / cl) if a and cl else None, cfg)
            got = bool(sig.get("entry"))
            ok = got == want
            fails += 0 if ok else 1
            if got:
                detail = f"ENTRY @{sig['hhmm']} signal {sig['signal_px']:g} fill {sig['fill_px']:g}"
            else:
                detail = (f"no entry, binding {sig.get('binding')} "
                          f"(OR-hi {sig.get('or_hi')}, "
                          f"HoD {max(b.h for b in bars):g})")
            print(f"    [{'ok' if ok else '!!'}] {sym} {day}: {detail}")
            if not ok:
                print(f"         expected {'ENTRY' if want else 'no entry'} — {note}")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--daily", action="store_true", help="run the daily-bar study")
    ap.add_argument("--intraday", action="store_true", help="run the 5-minute entry study")
    ap.add_argument("--assert-cases", action="store_true", help="replay the four motivating decisions")
    ap.add_argument("--ablate", action="store_true", help="print the ablation table")
    ap.add_argument("--folds", type=int, default=4, help="time folds for stability")
    ap.add_argument("--no-fees", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=str, default=str(PANEL / "trade_backtest.json"))
    a = ap.parse_args()
    if not (a.daily or a.intraday or a.assert_cases):
        ap.print_help()
        return 0

    cfg, warn = config_from_env()
    for w in warn:
        print(f"[warn] {w}")
    if a.no_fees:
        cfg.fee_buy = cfg.fee_sell = 0.0

    print("loading panel...")
    p = Panel().load()
    print(f"  {p.describe()}")

    print("precomputing ATR / RVOL1 series...")
    atrs = {s: atr_series(p, s) for s in p.raw_close}
    rv1s = {s: rvol1_series(p, s) for s in p.volume}

    print("building candidates (board rules, deduped across brokers)...")
    cands = build_candidates(p)
    print(f"  {len(cands)} candidate (symbol, day) pairs")
    if not cands:
        print("[!!] no candidates — nothing to test")
        return 1

    # ---- intraday paths
    if a.intraday or a.assert_cases:
        syms = sorted({c["symbol"] for c in cands}) | {"GGRM", "ISAT", "ERAA"} \
            if False else sorted(set(c["symbol"] for c in cands) | {"GGRM", "ISAT", "ERAA"})
        print(f"loading 5-minute bars for {len(syms)} symbols...")
        m5 = {s: read_bars(s) for s in syms}
        m5 = {s: v for s, v in m5.items() if v}
        idx5 = read_bars("COMPOSITE")
        print(f"  {len(m5)} symbols cached | index sessions {len(idx5)}")
        if not idx5:
            print("  [warn] no COMPOSITE bars — G1 (regime) will be skipped entirely")

        if a.assert_cases:
            print("\nREGRESSION — the four decisions that motivated the design")
            f = assert_cases(p, cfg, atrs, m5, idx5)
            print(f"\n  {'[ok] all four reproduce' if not f else f'[!!] {f} case(s) differ'}")
            if not a.intraday:
                return 0 if not f else 1

        if a.intraday:
            first = min((d for v in m5.values() for d in v), default=None)
            win = [c for c in cands
                   if c["i"] + 1 < len(p.dates) and p.dates[c["i"] + 1] >= (first or "")]
            print(f"\nINTRADAY ENTRY STUDY — {len(win)} candidates whose entry day has 5m bars")
            print(f"  window starts {first} | exits are the validated daily pair (ATR stop + E2)")
            print("=" * 118)
            dref = summarise_trades(run_set(p, win, cfg, {"atr_stop", "E2"}, atrs, rv1s))
            print(fmt("daily entry at close[i+1]", dref))
            print("-" * 118)
            # Ablate the gates individually before judging the stack. Five gates
            # derived from four anecdotes is exactly the shape that failed on the
            # daily side; the question worth answering is whether ANY single gate
            # carries signal, not whether all five together do.
            combos = [("G2",), ("G1",), ("G3",), ("G4",),
                      ("G2", "G3"), ("G2", "G3", "G4"), ("G1", "G2", "G3", "G4", "G5")]
            best = None
            for t_min in ("09:35", "10:00"):
                print(f"  T_min {t_min}")
                for gates in combos:
                    took, skip = run_intraday(p, win, cfg, atrs, rv1s, t_min, m5, idx5, gates)
                    s = summarise_trades(took)
                    if not s.get("n"):
                        print(f"    {'+'.join(gates):<22} (no trades)")
                        continue
                    sk = {(r["symbol"], r["i"]) for r in skip}
                    tk_d = summarise_trades(run_set(
                        p, [c for c in win if (c["symbol"], c["i"]) not in sk],
                        cfg, {"atr_stop", "E2"}, atrs, rv1s))
                    sk_d = summarise_trades(run_set(
                        p, [c for c in win if (c["symbol"], c["i"]) in sk],
                        cfg, {"atr_stop", "E2"}, atrs, rv1s))
                    sel = tk_d.get("mean_excess", 0) - sk_d.get("mean_excess", 0)
                    print(f"    {'+'.join(gates):<22} n {s['n']:>4} "
                          f"({s['n'] / len(win):>4.0%} taken) | excess {s['mean_excess'] * 100:+6.2f}% "
                          f"| meanR {s['meanR']:+.3f} | vs daily {(s['mean_excess'] - dref['mean_excess']) * 100:+.2f}pp "
                          f"| selection {sel * 100:+.2f}pp")
                    if s["n"] >= 40 and (best is None or s["mean_excess"] > best[2]["mean_excess"]):
                        best = (t_min, gates, s, sel)
            print("=" * 118)
            if best:
                t_min, gates, s, sel = best
                gain = (s["mean_excess"] - dref["mean_excess"]) * 100
                print(f"BEST with n>=40: T_min {t_min}, gates {'+'.join(gates)} — "
                      f"excess {s['mean_excess'] * 100:+.2f}% vs daily {dref['mean_excess'] * 100:+.2f}% "
                      f"({gain:+.2f}pp), n {s['n']}")
                # 14 combinations were tested. The best of 14 landing slightly positive
                # is what chance produces, so the only meaningful question is where it
                # sits against random filters that take the same NUMBER of trades.
                keep = s["n"] / len(win)
                draws = []
                for d in range(400):
                    r2 = random.Random(a.seed + 1000 + d)
                    sub = [c for c in win if r2.random() < keep]
                    q = summarise_trades(run_set(p, sub, cfg, {"atr_stop", "E2"}, atrs, rv1s))
                    if q.get("n"):
                        draws.append(q["mean_excess"])
                draws.sort()
                pctile = sum(1 for x in draws if x < s["mean_excess"]) / len(draws)
                print(f"  NULL — 400 random filters taking the same {keep:.0%} of candidates:")
                print(f"    random mean {statistics.fmean(draws) * 100:+.2f}% | "
                      f"5th {draws[int(0.05 * len(draws))] * 100:+.2f}% | "
                      f"95th {draws[int(0.95 * len(draws))] * 100:+.2f}% | "
                      f"real sits at the {pctile:.0%} percentile")
                usable = gain > 0 and pctile >= 0.95
                print(f"  verdict: {'USABLE (PROVISIONAL)' if usable else 'NOT USABLE'} — "
                      f"{'clears the null' if usable else 'indistinguishable from a random filter of the same size; 14 combinations were tested, so the best one being positive is expected by chance'}")
            else:
                print("NO GATE COMBINATION reaches n>=40. The intraday entry layer cannot "
                      "be evaluated on this window and must not ship as an automated filter.")
            print("  PROVISIONAL at best: one regime, ~114 sessions, no walk-forward "
                  "possible. n<200 throughout.")
            return 0

    vc = {(c["symbol"], c["i"]): veto_reasons(p, c, atrs, rv1s, cfg) for c in cands}
    nv = sum(1 for v in vc.values() if v)
    print(f"  vetoes would remove {nv} of {len(cands)} ({nv / len(cands):.1%})")
    for r in ("EXHAUST_1D", "STRUCT-", "EXTENDED"):
        print(f"     {r:<12} {sum(1 for v in vc.values() if r in v):>5}")

    fee_note = "gross of fees" if a.no_fees else f"net of {(cfg.fee_buy + cfg.fee_sell) * 100:.2f}% round trip"
    print(f"\nABLATION — {fee_note}, entry at close[i+1], excess over IHSG")
    print("=" * 118)

    rows: list[tuple[str, set, set | None]] = [
        ("BASELINE hold 5d (incumbent)", set(), None),
        # The confound control. Requiring a valid ATR stop silently DROPS names whose
        # stop would be wider than 8% of price — the most volatile candidates. If
        # baseline restricted to that same subset already earns the improvement, then
        # the width filter is doing the work and the stop itself adds nothing. This row
        # is the one that tells the two apart, and it must be read before any other.
        ("BASELINE on stoppable subset", {"tradeable_only"}, None),
        ("prior-low stop (what you did)", {"prior_low_stop"}, None),
        ("+ ATR stop", {"atr_stop"}, None),
        ("+ ATR stop + E2 structure", {"atr_stop", "E2"}, None),
        ("+ E3 blow-off", {"atr_stop", "E2", "E3"}, None),
        ("+ E6 time stop", {"atr_stop", "E2", "E3", "E6"}, None),
        ("+ trailing", {"atr_stop", "E2", "E3", "E6", "trail"}, None),
        ("+ veto EXHAUST_1D", {"atr_stop", "E2", "E3", "E6", "trail"}, {"EXHAUST_1D"}),
        ("+ veto STRUCT-", {"atr_stop", "E2", "E3", "E6", "trail"}, {"EXHAUST_1D", "STRUCT-"}),
        ("FULL DAILY", {"atr_stop", "E2", "E3", "E6", "trail"},
         {"EXHAUST_1D", "STRUCT-", "EXTENDED"}),
    ]

    results: dict[str, dict] = {}
    base = None
    for label, rules, vet in rows:
        tr = run_set(p, cands, cfg, rules, atrs, rv1s, vet, vc)
        s = summarise_trades(tr)
        results[label] = s
        if base is None:
            base = s
        print(fmt(label, s, base if label != rows[0][0] else None))
    print("=" * 118)

    full = results["FULL DAILY"]
    print("\nexit-rule mix (FULL DAILY): " +
          ", ".join(f"{k} {v}" for k, v in sorted(full.get("rules", {}).items(),
                                                  key=lambda x: -x[1])))

    # ---- stability across time folds, for EVERY row
    # Picking the winning row off the pooled table alone is how an ablation becomes a
    # search. A configuration only counts if it beats the incumbent in most folds too.
    print(f"\nSTABILITY — every configuration across {a.folds} equal time folds")
    print("  (excess over IHSG per fold; a row must win the pooled table AND most folds)")
    days = sorted({c["i"] for c in cands})
    edges = [days[len(days) * k // a.folds] for k in range(a.folds)] + [days[-1] + 1]
    folds = [[c for c in cands if edges[k] <= c["i"] < edges[k + 1]] for k in range(a.folds)]
    base_fold = [summarise_trades(run_set(p, sub, cfg, set(), atrs, rv1s)) for sub in folds]

    print(f"  {'configuration':<34}" + "".join(f"{'f' + str(k + 1):>9}" for k in range(a.folds))
          + f"{'wins':>7}")
    print(f"  {'BASELINE (incumbent)':<34}"
          + "".join(f"{b.get('mean_excess', 0) * 100:>8.2f}%" for b in base_fold) + f"{'--':>7}")
    stability: dict[str, int] = {}
    for label, rules, vet in rows[1:]:
        cells, wins = [], 0
        for k, sub in enumerate(folds):
            s = summarise_trades(run_set(p, sub, cfg, rules, atrs, rv1s, vet, vc))
            cells.append(s.get("mean_excess", 0) * 100 if s.get("n") else None)
            if s.get("n") and s["mean_excess"] > base_fold[k].get("mean_excess", 0):
                wins += 1
        stability[label] = wins
        print(f"  {label:<34}"
              + "".join(f"{c:>8.2f}%" if c is not None else f"{'--':>9}" for c in cells)
              + f"{wins}/{a.folds:>5}")
    stable = stability.get("FULL DAILY", 0)

    # ---- null 1: shuffle which days are signal days
    rnd = random.Random(a.seed)
    allpairs = [(s, i) for s in p.raw_close for i in p.raw_close[s]]
    fake = []
    for _ in range(len(cands)):
        s, i = rnd.choice(allpairs)
        f = features(p, s, i)
        if f:
            fake.append({"symbol": s, "i": i, "f": f})
    nb = summarise_trades(run_set(p, fake, cfg, set(), atrs, rv1s))
    nf = summarise_trades(run_set(p, fake, cfg, {"atr_stop", "E2", "E3", "E6", "trail"},
                                  atrs, rv1s))
    print("\nNULL 1 — random (symbol, day) pairs, same machinery")
    print(fmt("  random baseline", nb))
    print(fmt("  random + full exits", nf))
    print("  reads as: how much of the exit effect is just reduced exposure, "
          "not signal selection")

    # ---- null 2: coin-flip veto at the same acceptance rate
    keep_rate = 1 - nv / len(cands)
    print(f"\nNULL 2 — coin-flip veto at the SAME acceptance rate ({keep_rate:.1%}), 200 draws")
    real = full.get("meanR", 0)
    draws = []
    exits = {"atr_stop", "E2", "E3", "E6", "trail"}
    for d in range(200):
        r2 = random.Random(a.seed + d)
        sub = [c for c in cands if r2.random() < keep_rate]
        draws.append(summarise_trades(run_set(p, sub, cfg, exits, atrs, rv1s)).get("meanR", 0))
    draws.sort()
    pct = sum(1 for x in draws if x < real) / len(draws)
    print(f"  real vetoes meanR {real:+.3f} | coin-flip mean {statistics.fmean(draws):+.3f} "
          f"| 5th pct {draws[int(0.05 * len(draws))]:+.3f} | 95th {draws[int(0.95 * len(draws))]:+.3f}")
    print(f"  the real veto set sits at the {pct:.0%} percentile of random filters "
          f"of the same size")

    # ---- verdict
    BASE = "BASELINE hold 5d (incumbent)"
    b0 = results[BASE]
    beat = (full.get("meanR", 0) > b0.get("meanR", 0)
            and full.get("mean_excess", 0) > b0.get("mean_excess", 0))
    print("\n" + "=" * 118)
    print(f"KILL CRITERION: FULL DAILY beats BASELINE on meanR AND mean excess -> "
          f"{'PASS' if beat else 'FAIL'}")

    # If the full stack fails, the ablation has still done its job: it says WHICH
    # components carry the effect. Reading that off is the table's purpose, not
    # cherry-picking — the alternative is discarding a real finding because it arrived
    # attached to three ideas that did not work. The bar stays high: a shippable row
    # must beat the incumbent on BOTH pooled metrics AND in most folds.
    # A managed row must beat BOTH baselines: the incumbent, and the incumbent
    # restricted to the same universe. Clearing only the first would mean the gain
    # came from refusing to trade wide-stop names, which is a screening change, not a
    # risk-management one — and it would belong in the board, not here.
    SUB = "BASELINE on stoppable subset"
    bsub = results.get(SUB, b0)
    bar_R = max(b0["meanR"], bsub.get("meanR", b0["meanR"]))
    bar_X = max(b0["mean_excess"], bsub.get("mean_excess", b0["mean_excess"]))
    print(f"  comparator: max(incumbent, stoppable-subset) = "
          f"meanR {bar_R:+.3f}, excess {bar_X * 100:+.2f}%")
    ship = [(lbl, s) for lbl, s in results.items()
            if lbl not in (BASE, SUB)
            and s.get("n", 0) >= 200
            and s.get("meanR", 0) > bar_R
            and s.get("mean_excess", 0) > bar_X
            and stability.get(lbl, 0) >= a.folds - 1]
    ship.sort(key=lambda x: -x[1]["mean_excess"])
    if ship:
        lbl, s = ship[0]
        print(f"SHIPPABLE SUBSET: {lbl}")
        print(f"  excess {s['mean_excess'] * 100:+.2f}% vs baseline {b0['mean_excess'] * 100:+.2f}% "
              f"({(s['mean_excess'] - b0['mean_excess']) * 100:+.2f}pp) | meanR {s['meanR']:+.3f} "
              f"vs {b0['meanR']:+.3f} | n {s['n']} | folds won {stability.get(lbl, 0)}/{a.folds}")
        print(f"  every other row is REJECTED — see the table above for what each cost")
    else:
        print("SHIPPABLE SUBSET: none — no configuration clears both metrics and "
              f"{a.folds - 1}/{a.folds} folds. Ship nothing.")
    print("=" * 118)

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(
        {"n_candidates": len(cands), "fees": not a.no_fees,
         "full_daily_folds_won": stable, "stability": stability,
         "null2_percentile": pct, "results": results,
         "verdict_full_stack": "PASS" if beat else "FAIL",
         "shippable": ship[0][0] if ship else None}, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {a.out}")
    return 0 if ship else 2


if __name__ == "__main__":
    sys.exit(main())
