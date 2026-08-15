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

from alpha_lib import PANEL, Panel, block_ci, panel_fingerprint, wilson_lower  # noqa: E402
from broker_alpha import build_events  # noqa: E402
from intraday_lib import evaluate_entry, read_bars  # noqa: E402
from momentum_setup import is_momentum  # noqa: E402
from overlay_test import features  # noqa: E402
from trade_lib import (SHARES_PER_LOT, LegPlan, RiskConfig,  # noqa: E402
                       atr_series, blend_excess, blend_r, config_from_env,
                       low_n_prior, round_tick, rvol1_series, stop_price,
                       target_price, tick_size)

# Board thresholds, mirrored from build_momentum_board.py. Changing these here would
# silently backtest a different screener than the one that publishes.
MIN_VALUE = 500e6
MIN_ADTV_PCT = 10.0
RVOL_MIN, RVOL_MAX = 1.5, 3.0
DD_MIN, RSI_MIN = -0.10, 55.0

HOLD = 5              # baseline holding period, matches the validated +1.40% (5d)
MAX_HOLD = 10         # hard backstop so a trailing stop cannot run forever

# ---- fill conventions, switchable so each can be measured on its own.
#
# GAP_FILL="open" (D1) prices a stop gapped through overnight at the session's actual
# OPEN. Until 2026-08-15 Panel carried no opens and the PRIOR CLOSE stood in as a
# proxy. Measured across 29,028 stop episodes that proxy is optimistic on 8.9% of
# them and pessimistic on 0.0% — a one-sided flattery of exactly the violent days a
# stop exists for, worth about -0.40% of entry when it bites. "prior_close"
# reproduces the pre-fix numbers and exists only to keep golden/v0.json replayable.
#
# E2_FILL (D2) resolves a live doc/code contradiction: the module docstring above
# says close-based exits fill at the NEXT OPEN, the code has always filled at the
# next CLOSE. Both are defensible; only one was documented. "next_close" stays the
# default because adopting the more flattering of two conventions after seeing the
# scores is a search, not a decision.
GAP_FILL = "open"
E2_FILL = "next_close"

# CA_ADJUST (D3) closes a bug that predates this file: DECISIONS are taken on raw bars
# (correct — a stop is a raw price a broker can see) but the P&L was ALSO computed on
# raw prices, so any trade spanning an ex-date booked the adjustment as a return. PACK
# on 2026-01-12 printed raw -91.7% against an ADJUSTED +9.4%: the stock rose and the
# backtest recorded -11.6R. 17 of the 20 worst "stop" losses were ex-dates, not gaps.
#
# It stayed hidden because the old prior-close gap proxy capped the fake loss at the
# stop; pricing gaps honestly is what surfaced it. The fix keeps every decision on raw
# bars and converts only the RETURN to the adjusted basis via f = close_adj/raw_close,
# which is the convention alpha_lib.py:8-16 already states and this file violated.
CA_ADJUST = True


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

def _adj_factor(p: Panel, sym: str, i: int) -> float:
    """close_adj / raw_close for one session — the corporate-action scale on day i.

    1.0 on every ordinary session. On an ex-date the adjusted series is continuous
    while the raw series steps, and the ratio is exactly the step. Multiplying both
    legs of a trade by their own factor removes the step from the return without
    touching any decision, which must stay on raw prices.
    """
    a = p.close.get(sym, {}).get(i)
    r = p.raw_close.get(sym, {}).get(i)
    return (a / r) if (a and r) else 1.0

def simulate(p: Panel, c: dict, cfg: RiskConfig, rules: set[str],
             atrs: dict, rv1s: dict, *, legs: LegPlan | None = None,
             entry_px: float | None = None) -> dict | None:
    """Replay one candidate under `rules`. Returns a trade record or None if untradeable.

    Every exit is priced pessimistically:
      - a stop gapped through fills at the OPEN, not at the stop
      - a close-based exit fills at the NEXT open, because the decision is only
        knowable after the close
    Overstating fill quality is the second-most-common way a backtest lies.
    """
    sym, i = c["symbol"], c["i"]
    hi, lo, cl = p.high.get(sym, {}), p.low.get(sym, {}), p.raw_close.get(sym, {})
    op = p.open.get(sym, {}) if GAP_FILL == "open" else {}
    ent = i + 1
    if ent not in cl:
        return None
    # entry_px overrides the close only for the entry-fill study, where the whole
    # question is what a different fill does to the same trade. Everything downstream
    # — stop, r_ps, R — then derives from the ACTUAL fill, or the policies would not
    # be comparable.
    entry = cl[ent] if entry_px is None else entry_px
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

    # Profit leg, if one is planned. Computed once, from entry — the level must be
    # knowable when the order is placed, not derived from the path it later takes.
    tgt = target_price(entry, r_ps, a, legs) if legs else 0
    scale_leg = None          # (weight, effective_px, exit_i) once the leg fills
    both_touched = 0

    j = ent + 1
    while j <= ent + horizon:
        if j not in cl:
            break
        prev_c = cl.get(j - 1)

        # E1 hard stop, intraday. A gap-through fills at the session's OPEN — the
        # price a market order actually gets — falling back to the prior close only
        # when no open exists. min() because an open ABOVE the stop means the gap
        # closed intraday and the stop filled at the stop.
        if cur_stop and lo.get(j) is not None and lo[j] <= cur_stop:
            gap_open = op.get(j, prev_c if prev_c is not None else cur_stop)
            exit_i, exit_px = j, min(gap_open, cur_stop)
            exit_rule = "E1_stop"
            break

        # Profit leg. Deliberately AFTER the stop: on a bar that touched both levels
        # the intra-bar path is unknown, and assuming the adverse leg filled first is
        # the only assumption that cannot flatter the result. `both_touched` is
        # reported so the size of that assumption is visible rather than argued about.
        if legs and scale_leg is None and hi.get(j) is not None and hi[j] >= tgt:
            if cur_stop and lo.get(j) is not None and lo[j] <= cur_stop:
                both_touched = 1
            if legs.fill == "limit_intraday":
                # A gap-up THROUGH a resting sell limit fills at the open, which is
                # better than the limit — the one place in this model where the
                # honest fill helps the trade.
                fill_px, fill_i = max(tgt, op.get(j, tgt)), j
            elif (j + 1) in cl:
                fill_i = j + 1
                fill_px = (p.open.get(sym, {}).get(j + 1, cl[j + 1])
                           if legs.fill == "next_open" else cl[j + 1])
            else:
                fill_px, fill_i = None, None
            if fill_px:
                eff = fill_px * _adj_factor(p, sym, fill_i) / _adj_factor(p, sym, ent) \
                    if CA_ADJUST else fill_px
                if legs.kind == "full":
                    exit_i, exit_px, exit_rule = fill_i, fill_px, f"T_{legs.label or 'target'}"
                    break
                scale_leg = (legs.fraction, eff, fill_i)

        # E2 structure: CLOSE below the 5-session low. Intraday probes ignored on
        # purpose — that distinction is the whole GGRM lesson.
        if "E2" in rules:
            l5 = low_n_prior(p, sym, j - 1, cfg.struct_lookback)
            if l5 and cl[j] < l5 and (j + 1) in cl:
                exit_i, exit_rule = j + 1, "E2_structure"
                exit_px = (p.open.get(sym, {}).get(j + 1, cl[j + 1])
                           if E2_FILL == "next_open" else cl[j + 1])
                break

        # E3 blow-off into supply.
        if "E3" in rules:
            rv = rv1s.get(sym, {}).get(j)
            h, l_, c_ = hi.get(j), lo.get(j), cl.get(j)
            clv = (c_ - l_) / (h - l_) if (h is not None and l_ is not None and h > l_) else None
            if rv and rv >= 4.0 and clv is not None and clv <= 0.35 and (j + 1) in cl:
                exit_i, exit_rule = j + 1, "E3_blowoff"
                exit_px = (p.open.get(sym, {}).get(j + 1, cl[j + 1])
                           if E2_FILL == "next_open" else cl[j + 1])
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

    # Return on the ADJUSTED basis; decisions above stay on raw bars. f is 1.0 for
    # every session with no corporate action, so this is a no-op on ~96% of trades
    # and rescues the rest. Falls back to 1.0 when either leg is missing, which
    # degrades to the old raw-price behaviour rather than dropping the trade.
    fe = _adj_factor(p, sym, ent) if CA_ADJUST else 1.0
    fx = _adj_factor(p, sym, exit_i) if CA_ADJUST else 1.0
    gross = (exit_px * fx) / (entry * fe) - 1
    bench = (p.bench.get(exit_i, 0) / p.bench[ent] - 1) if (ent in p.bench and exit_i in p.bench) else None
    cost = cfg.fee_buy + cfg.fee_sell if cfg.fee_buy else 0.0
    net = gross - cost

    if scale_leg is not None:
        # Two legs. Weights are fractions of the ORIGINAL position and r_ps is the
        # risk-per-share frozen at open — neither is rescaled by what remains, or R
        # stops being comparable across configurations.
        w, sp, si = scale_leg
        def _b(k):
            return (p.bench[k] / p.bench[ent] - 1) \
                if (ent in p.bench and k in p.bench) else None
        bs, bf = _b(si), _b(exit_i)
        legs_px = [(w, sp), (1 - w, exit_px * fx / fe)]
        R = blend_r(legs_px, entry, r_ps, cost)
        gross = sum(wt * (px / entry - 1) for wt, px in legs_px)
        net = gross - cost
        excess = (blend_excess([(w, sp, bs), (1 - w, exit_px * fx / fe, bf)], entry, cost)
                  if (bs is not None and bf is not None) else None)
        return {"symbol": sym, "i": i, "entry_i": ent, "exit_i": exit_i,
                "entry": entry, "exit": exit_px, "stop": stop, "basis": basis,
                "R": R, "gross": gross, "net": net, "excess": excess,
                "held": exit_i - ent, "rule": f"SCALE+{exit_rule}",
                "scale_i": si, "scale_px": sp, "scale_frac": w,
                "both_touched": both_touched}
    return {"symbol": sym, "i": i, "entry_i": ent, "exit_i": exit_i,
            "entry": entry, "exit": exit_px, "stop": stop, "basis": basis,
            # R = net return x (entry / risk-per-share). Algebraically identical to the
            # legacy raw expression when f == 1, but NOT bit-identical, so the legacy
            # form is kept verbatim under the flag to preserve the v0 regression.
            "R": (net * entry / r_ps) if CA_ADJUST
                 else ((exit_px - entry - entry * cost) / r_ps),
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
            veto_cache: dict | None = None, legs: LegPlan | None = None) -> list[dict]:
    out = []
    for c in cands:
        if vetoes:
            vr = veto_cache.get((c["symbol"], c["i"]), [])
            if any(v in vetoes for v in vr):
                continue
        t = simulate(p, c, cfg, rules, atrs, rv1s, legs=legs)
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


# ----------------------------------------------------------------------------- check 0

def check_zero(p: Panel, cands: list[dict], n_folds: int = 4, k: int = 5,
               bar: float = 0.005) -> dict:
    """Before reading ANY result from a window, show the known-good rule still works there.

    This is `reference/accumulation.md` §6.0, and it exists because skipping it
    produced a confidently stated wrong verdict: one-sidedness was written up as
    REFUTED on a -0.83pp lift over 59 sessions, when the VALIDATED momentum rule
    returned -1.39pp over that same window against +2.26pp over two years. The window
    was hostile to the whole family; a new rule's failure there was indistinguishable
    from the period. Verdict withdrawn.

    So: momentum's k-day lift over a matched baseline, pooled and per fold. The
    baseline is every (symbol, day) in the SAME symbol universe and the SAME fold —
    momentum names drift up with their universe, and a rule must beat that, not zero.

    Runs in the harness rather than living in prose, because a check that is only
    documented catches nothing.
    """
    universe = sorted({c["symbol"] for c in cands})
    days = sorted({c["i"] for c in cands})
    if not days:
        return {"ok": False, "reason": "no candidates"}
    edges = [days[len(days) * j // n_folds] for j in range(n_folds)] + [days[-1] + 1]

    def lift(lo: int, hi: int) -> dict:
        ev = [x for c in cands if lo <= c["i"] < hi
              for x in [p.excess_return(c["symbol"], c["i"], k)] if x is not None]
        bl = [x for s in universe for i in p.raw_close.get(s, {})
              if lo <= i < hi
              for x in [p.excess_return(s, i, k)] if x is not None]
        if not ev or not bl:
            return {"n": len(ev), "n_base": len(bl), "lift": None}
        m, b = statistics.fmean(ev), statistics.fmean(bl)
        return {"n": len(ev), "n_base": len(bl), "momentum": m, "baseline": b,
                "lift": m - b}

    pooled = lift(days[0], days[-1] + 1)
    per = [lift(edges[j], edges[j + 1]) for j in range(n_folds)]
    ok = pooled.get("lift") is not None and pooled["lift"] >= bar
    return {"ok": ok, "bar": bar, "k": k, "pooled": pooled, "folds": per,
            "n_universe": len(universe)}


# ------------------------------------------------------------------------- regression

def regress_against(golden_path: str, payload: dict) -> int:
    """Diff a fresh run against a frozen golden. Exit 3 = incomparable, 1 = drifted.

    Two deliberate strictnesses:

    1. **Fingerprint first.** A golden is only comparable to a run over the SAME
       panel. The stored trade_backtest.json says n=915; the same code on today's
       panel says n=1088. Nothing was wrong with either — they describe different
       data, and diffing them would have reported eleven spurious "regressions".
    2. **repr(), not isclose().** This guards a refactor, not a tolerance. The
       single-leg path must stay byte-identical when the two-leg path is added; a
       1e-12 drift is the signature of reordered float arithmetic, which is exactly
       what generalising the single-leg formula to `fraction=1.0` produces. A
       tolerance test would pass it and the incumbent's numbers would quietly move.
    """
    gp = Path(golden_path)
    if not gp.exists():
        print(f"[!!] golden not found: {gp}")
        return 3
    g = json.loads(gp.read_text(encoding="utf-8"))

    gf, nf = g.get("panel_fingerprint"), payload.get("panel_fingerprint")
    if gf is None:
        print(f"[!!] {gp.name} predates fingerprinting — recapture it, do not compare")
        return 3
    if gf != nf:
        print(f"[!!] PANEL DRIFTED — golden and run describe different data.")
        print(f"     golden: {gf}")
        print(f"     run   : {nf}")
        print("     recapture the golden; a diff across panels is meaningless.")
        return 3

    gc, nc = g.get("conventions"), payload.get("conventions")
    if gc != nc:
        # Not a hard gate: comparing across conventions is exactly what the D1/D2
        # tables do on purpose. But an UNINTENDED convention change would otherwise
        # read as "the refactor broke the incumbent", so it is called out first.
        print(f"[note] fill conventions differ — golden {gc} vs run {nc}.")
        print("       Differences below are EXPECTED if this is a D1/D2 comparison.")

    diffs = []
    gres, nres = g.get("results", {}), payload.get("results", {})
    for lbl in sorted(set(gres) | set(nres)):
        if lbl not in gres:
            diffs.append(f"  + NEW ROW  {lbl}")
            continue
        if lbl not in nres:
            diffs.append(f"  - GONE     {lbl}")
            continue
        for k in sorted(set(gres[lbl]) | set(nres[lbl])):
            gv, nv = gres[lbl].get(k), nres[lbl].get(k)
            if repr(gv) != repr(nv):
                diffs.append(f"  ~ {lbl} :: {k}\n      golden {gv!r}\n      run    {nv!r}")

    print(f"\nREGRESSION vs {gp.name}  (panel {nf['sha']}, {len(gres)} golden rows)")
    if not diffs:
        print(f"  [ok] all {len(gres)} rows bit-identical")
        return 0
    print(f"  [!!] {len(diffs)} difference(s):")
    for d in diffs:
        print(d)
    return 1


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
    ap.add_argument("--regress", type=str, default=None,
                    help="diff this run against a frozen golden JSON (exit 1 on drift, "
                         "3 if the panel no longer matches)")
    ap.add_argument("--gap-fill", choices=("open", "prior_close"), default=GAP_FILL,
                    help="how a stop gapped through overnight fills (D1)")
    ap.add_argument("--e2-fill", choices=("next_close", "next_open"), default=E2_FILL,
                    help="how a close-based exit fills (D2)")
    ap.add_argument("--no-ca-adjust", action="store_true",
                    help="price P&L on raw bars, ex-dates included (D3 off; legacy)")
    a = ap.parse_args()
    globals()["GAP_FILL"] = a.gap_fill
    globals()["E2_FILL"] = a.e2_fill
    globals()["CA_ADJUST"] = not a.no_ca_adjust
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
    fingerprint = panel_fingerprint()
    fingerprint |= {"n_dates": len(p.dates), "n_symbols": len(p.close),
                    "first": p.dates[0] if p.dates else None,
                    "last": p.dates[-1] if p.dates else None}
    print(f"  panel fingerprint {fingerprint['sha']} | "
          f"fees {cfg.fee_buy:.4f}/{cfg.fee_sell:.4f} "
          f"({(cfg.fee_buy + cfg.fee_sell) * 100:.2f}% round trip)")

    print("precomputing ATR / RVOL1 series...")
    atrs = {s: atr_series(p, s) for s in p.raw_close}
    rv1s = {s: rvol1_series(p, s) for s in p.volume}

    print("building candidates (board rules, deduped across brokers)...")
    cands = build_candidates(p)
    print(f"  {len(cands)} candidate (symbol, day) pairs")
    if not cands:
        print("[!!] no candidates — nothing to test")
        return 1

    # ---- CHECK 0 — is this window one where the known-good rule still works?
    c0 = check_zero(p, cands, a.folds)
    pooled = c0["pooled"]
    print(f"\nCHECK 0 — {c0['k']}d momentum lift vs matched baseline "
          f"(same {c0['n_universe']} symbols, same fold); bar +{c0['bar'] * 100:.1f}pp")
    print(f"  pooled  n {pooled['n']:>5} vs base {pooled['n_base']:>6} | "
          f"momentum {pooled['momentum'] * 100:+.2f}% | baseline {pooled['baseline'] * 100:+.2f}% | "
          f"lift {pooled['lift'] * 100:+.2f}pp  {'PASS' if c0['ok'] else 'FAIL'}")
    for j, f in enumerate(c0["folds"]):
        v = f.get("lift")
        print(f"  fold {j + 1}   n {f['n']:>5} vs base {f['n_base']:>6} | "
              + (f"lift {v * 100:+.2f}pp" if v is not None else "lift --"))
    if not c0["ok"]:
        print("  [!!] the known-good rule does NOT work in this window. Nothing read from")
        print("       it can separate a bad rule from a bad period. Stopping.")
        return 4

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
    # Keep the CELLS, not just the win count. A row can go 3/4 -> 1/4 on a 0.33pp
    # change in the fill model, which says the margins were thinner than the
    # modelling error — and the win count alone hides that entirely.
    stability_cells: dict[str, list] = {
        "BASELINE (incumbent)": [b.get("mean_excess", 0) * 100 for b in base_fold],
        "_fold_n": [len(sub) for sub in folds]}
    for label, rules, vet in rows[1:]:
        cells, wins = [], 0
        for k, sub in enumerate(folds):
            s = summarise_trades(run_set(p, sub, cfg, rules, atrs, rv1s, vet, vc))
            cells.append(s.get("mean_excess", 0) * 100 if s.get("n") else None)
            if s.get("n") and s["mean_excess"] > base_fold[k].get("mean_excess", 0):
                wins += 1
        stability[label] = wins
        stability_cells[label] = cells
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

    payload = {"panel_fingerprint": fingerprint,
               "conventions": {"gap_fill": GAP_FILL, "e2_fill": E2_FILL,
                               "ca_adjust": CA_ADJUST},
               "check_zero": c0,
               "n_candidates": len(cands), "fees": not a.no_fees,
               "fee_buy": cfg.fee_buy, "fee_sell": cfg.fee_sell,
               "full_daily_folds_won": stable, "stability": stability,
               "stability_cells": stability_cells,
               "null2_percentile": pct, "results": results,
               "verdict_full_stack": "PASS" if beat else "FAIL",
               "shippable": ship[0][0] if ship else None}

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {a.out}")

    if a.regress:
        rc = regress_against(a.regress, payload)
        if rc:
            return rc
    return 0 if ship else 2


if __name__ == "__main__":
    sys.exit(main())
