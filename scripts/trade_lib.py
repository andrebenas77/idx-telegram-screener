#!/usr/bin/env python3
"""Position mechanics for the IDX trade layer: ticks, lots, ATR, stops, sizing, R.

This is the only module that does money arithmetic, and it is deliberately the only
one that imports nothing over the network. `trade_backtest.py` runs the entire
two-year daily study against this file with no API key present; a rate-limited or
down vendor can never change what a lot is worth.

Four rules are load-bearing here. Each exists because its absence cost real money on
2026-08-07/10, and each is enforced structurally rather than by comment.

1. **FORBIDDEN: a stop at, or one tick under, the prior session's low.**
   GGRM closed 2026-08-07 at 21725 with ATR(14) around 700. Friday's low, 20725, sat
   only ~1.4 ATR below that close — inside the noise band of a stock that ranges 3.5%
   a day. Monday's low was 20700: the prior-day low was breached by Rp25, or 0.12%,
   and Monday *closed* at 20750, back above it. `stop_price()` cannot emit that stop —
   the structural leg is the FIVE-session low, and it is only used when it is WIDER
   than the ATR stop.

2. **Stops round DOWN, never to nearest.** Rounding a stop up tightens it, which
   shrinks the stop distance, which silently increases the lot count beyond the risk
   budget. The rounding direction is a sizing decision wearing a display costume.

3. **Volatility is measured on RAW bars.** `Panel` carries both series and
   `alpha_lib.py:88-95` explains why: adjustment rescales history on every corporate
   action and would rewrite past highs. Adjusted closes stay reserved for returns.
   A split still prints a ~50% raw gap, so `corporate_action_on()` detects the
   ex-date exactly (raw and adjusted one-day returns diverge there and nowhere else)
   and that day is dropped from the ATR window rather than poisoning it for 40
   sessions.

4. **One lot may be more risk than the budget allows.** At Rp2.08m per GGRM lot a
   small book cannot express a 1.5% risk in whole lots. There are exactly two honest
   answers — take it and flag OVERSIZED_MIN_LOT, or skip and say so. Quietly
   accepting a 2.5x-budget position is how a 1.5% system produces a 4% loss.

Units: 1 IDX lot = 100 shares. All money is IDR.
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alpha_lib import Panel  # noqa: E402

SHARES_PER_LOT = 100


# --------------------------------------------------------------------- tick mechanics

def tick_size(price: float) -> int:
    """IDX tick ladder (post-2023 regime).

    Copied deliberately from dewa-orderbook-lab/scripts/ob_core.py:51 rather than
    imported: that is a different repo, and a cross-repo sys.path hack is exactly how
    the VPS deploy breaks. If the ladder ever changes, both copies move together.
    """
    if price < 200:
        return 1
    if price < 500:
        return 2
    if price < 2000:
        return 5
    if price < 5000:
        return 10
    return 25


def round_tick(price: float, mode: str = "down") -> int:
    """Snap to a valid IDX tick.

    mode: 'down' for stops (never tightens), 'up' for entries and targets (never
    assumes a better fill than the ladder allows), 'near' for DISPLAY ONLY.

    On band crossings: every band boundary (200, 500, 2000, 5000) is an exact
    multiple of the tick in the band above it, so rounding down from just above a
    boundary lands ON the boundary and never below. The band is re-checked anyway and
    the loop is proven to terminate — a silent one-tick error in a stop is a silent
    sizing error, and this is cheap insurance.
    """
    if price <= 0:
        return 0
    for _ in range(4):
        t = tick_size(price)
        if mode == "down":
            snapped = math.floor(price / t) * t
        elif mode == "up":
            snapped = math.ceil(price / t) * t
        else:
            snapped = round(price / t) * t
        if tick_size(snapped) == t or snapped == price:
            return int(snapped)
        price = snapped        # landed in a different band; re-snap with its tick
    return int(snapped)


def ticks_between(a: float, b: float) -> float:
    """Approximate tick count between two prices, using the lower price's band."""
    t = tick_size(min(a, b))
    return abs(a - b) / t if t else 0.0


# ------------------------------------------------------------------------------- ATR

def corporate_action_on(p: Panel, sym: str, i: int, tol: float = 0.02) -> bool:
    """True when the RAW and ADJUSTED one-day returns disagree by more than `tol`.

    A split, bonus or dividend rescales close_adj but not close, so the two ratios
    diverge on exactly the ex-date and nowhere else. Cheaper and more reliable than
    parsing corporate_actions.json, and it uses data already in memory.
    """
    raw, adj = p.raw_close.get(sym), p.close.get(sym)
    if not raw or not adj:
        return False
    if i not in raw or i - 1 not in raw or i not in adj or i - 1 not in adj:
        return False
    if raw[i - 1] <= 0 or adj[i - 1] <= 0:
        return False
    return abs((raw[i] / raw[i - 1]) - (adj[i] / adj[i - 1])) > tol


def true_range(p: Panel, sym: str, i: int):
    """max(H-L, |H - C_prev|, |L - C_prev|) on RAW bars. None if any leg is missing."""
    hi, lo, cl = p.high.get(sym), p.low.get(sym), p.raw_close.get(sym)
    if not hi or not lo or not cl:
        return None
    if i not in hi or i not in lo or i - 1 not in cl:
        return None
    prev = cl[i - 1]
    return max(hi[i] - lo[i], abs(hi[i] - prev), abs(lo[i] - prev))


def atr(p: Panel, sym: str, i: int, n: int = 14, method: str = "wilder"):
    """ATR through day i inclusive, on raw bars, corporate-action days excluded.

    method='wilder' (default, the standard definition): seed with the arithmetic mean
    of the first n true ranges in the lookback, then ATR_j = (ATR_{j-1}*(n-1) + TR_j)/n.
    Wilder needs history for the seed to decay out, so we walk 4n sessions back.

    method='sma': plain mean of the last n true ranges. Kept because the original
    diagnostic of the GGRM trade used it; the two differ by a few percent on this data
    and the stop policy is unchanged either way. Having both lets --selftest state the
    difference rather than hide it.

    Returns None when fewer than n usable observations exist.
    """
    trs: list[float] = []
    for j in range(max(1, i - 4 * n + 1), i + 1):
        if corporate_action_on(p, sym, j):
            continue          # ex-date: a missing observation, not a real range
        tr = true_range(p, sym, j)
        if tr is not None:
            trs.append(tr)
    if len(trs) < n:
        return None
    if method == "sma":
        return sum(trs[-n:]) / n
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def atr_series(p: Panel, sym: str, n: int = 14, method: str = "wilder") -> dict[int, float]:
    """ATR for EVERY day of a symbol in one pass. {i: atr}.

    `atr()` walks 4n sessions per call, which is fine for one lookup and hopeless
    across 112 symbols x 476 days inside a backtest. Same definition, same
    corporate-action handling, O(n) instead of O(n*56).
    """
    cl = p.raw_close.get(sym) or {}
    if not cl:
        return {}
    out: dict[int, float] = {}
    trs: list[float] = []
    run = None
    for i in sorted(cl):
        if corporate_action_on(p, sym, i):
            continue
        tr = true_range(p, sym, i)
        if tr is None:
            continue
        trs.append(tr)
        if len(trs) < n:
            continue
        if run is None:
            run = sum(trs[:n]) / n            # Wilder seed
        else:
            run = (run * (n - 1) + tr) / n
        out[i] = (sum(trs[-n:]) / n) if method == "sma" else run
    return out


def rvol1_series(p: Panel, sym: str, base: int = 20) -> dict[int, float]:
    """volume[i] / mean(volume[i-base..i-1]). STRICTLY trailing.

    The denominator excludes day i, matching Panel.adtv (alpha_lib.py:138-148). This
    is the statistic the board's RVOL5 cannot see: ISAT on 2026-08-07 printed 6.06
    here while RVOL5 read 2.11, comfortably inside the [1.5, 3.0) buy band, because a
    five-day mean divides one blow-off session by five.
    """
    vo = p.volume.get(sym) or {}
    out: dict[int, float] = {}
    idxs = sorted(vo)
    window: list[float] = []
    for i in idxs:
        if len(window) == base and sum(window):
            out[i] = vo[i] / (sum(window) / base)
        window.append(vo[i])
        if len(window) > base:
            window.pop(0)
    return out


def ema(values: list[float], n: int):
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


# ------------------------------------------------------------------------ risk config

@dataclass
class RiskConfig:
    """Defaults reflect a concentrated book: 1.5% risk, 1-3 positions.

    At 1.5% risk the NOTIONAL cap binds before the risk budget on low-volatility
    names, which is why max_pos_pct is 0.30 rather than the 0.20 that suits a
    diversified book. That number is a preference, not a finding — it is the single
    parameter here most worth revisiting once the backtest has run.
    """
    equity_idr: float = 100_000_000.0
    risk_pct: float = 0.015
    # "notional" targets a rupiah POSITION SIZE and lets risk fall out of the stop
    # distance; "risk" targets a rupiah LOSS and lets position size fall out of it.
    # These are not the same knob and confusing them is expensive in both directions:
    # a Rp300m position with a 5% stop risks Rp15m, while a Rp45m RISK with the same
    # stop is a Rp900m position.
    sizing_mode: str = "risk"
    notional_min_idr: float = 100_000_000.0
    notional_max_idr: float = 300_000_000.0
    k_atr: float = 1.5
    k_atr_min: float = 1.2
    k_atr_max: float = 2.5
    min_stop_pct: float = 0.025
    max_stop_pct: float = 0.08
    max_pos_pct: float = 0.30
    adtv_cap_pct: float = 0.05
    # HEAT is the real constraint; max_open is a backstop, not a policy.
    # It was originally 3, on the assumption that every position carries a full 1.5%
    # risk — three of those is 4.5% and the two caps bind together. At a Rp3bn book
    # that assumption breaks: the 5%-of-ADTV liquidity cap holds a typical mid-cap to
    # ~0.5% risk, so three positions came to 2.56% heat and the SLOT count then blocked
    # further names while a third of the risk budget sat unused. Six slots lets
    # liquidity-capped positions add up to the intended risk; heat and beta-gross still
    # decide when to stop.
    max_open: int = 6
    heat_cap_pct: float = 0.045
    max_per_sector: int = 2
    beta_gross_cap: float = 0.60
    min_lot_tolerance: float = 1.5
    fee_buy: float = 0.0015
    fee_sell: float = 0.0025
    struct_lookback: int = 5
    # Profit-taking leg. NOT an edge — measured at -31.9bp of mean excess (95% CI
    # -13.0 to -52.1) across 1,088 trades, bought deliberately for regret control
    # inside a declared 40bp budget. See reference/exits.md §1. A full exit at the
    # same level costs -95.6bp, and holding to E2 is what the evidence prefers.
    scale_fraction: float = 1 / 3
    scale_level_r: float = 2.0
    notes: list[str] = field(default_factory=list)

    @property
    def risk_budget_idr(self) -> float:
        return self.equity_idr * self.risk_pct


ENVF = Path(__file__).resolve().parents[1] / "secrets" / "trade.env"


def load_trade_env() -> dict:
    """secrets/trade.env, with the ENVIRONMENT winning.

    Same precedence as notify_telegram.py:33-45, so systemd can inject values on the
    VPS without editing a file in the checkout.
    """
    import os
    env: dict[str, str] = {}
    if ENVF.exists():
        for line in ENVF.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("TRADE_EQUITY_IDR", "TRADE_RISK_PCT", "TRADE_FEE_BUY", "TRADE_FEE_SELL",
              "TRADE_SIZING_MODE", "TRADE_NOTIONAL_MIN_IDR", "TRADE_NOTIONAL_MAX_IDR"):
        if os.environ.get(k):
            env[k] = os.environ[k].strip()
    return env


def config_from_env() -> tuple["RiskConfig", list[str]]:
    """The single source of risk config, shared by the plan, the manager AND the
    backtest. Costs are not cosmetic here — the backtest measures net returns, so a
    backtest running on different fees than the live sizing is measuring a strategy
    nobody trades.
    """
    env, warn = load_trade_env(), []
    cfg = RiskConfig()
    if env.get("TRADE_EQUITY_IDR"):
        cfg.equity_idr = float(env["TRADE_EQUITY_IDR"])
    else:
        warn.append(f"TRADE_EQUITY_IDR not set — using placeholder "
                    f"Rp{cfg.equity_idr:,.0f}; lot counts are fiction until you set it")
    if env.get("TRADE_RISK_PCT"):
        cfg.risk_pct = float(env["TRADE_RISK_PCT"])
    if env.get("TRADE_FEE_BUY"):
        cfg.fee_buy = float(env["TRADE_FEE_BUY"])
    if env.get("TRADE_FEE_SELL"):
        cfg.fee_sell = float(env["TRADE_FEE_SELL"])
    if not (env.get("TRADE_FEE_BUY") and env.get("TRADE_FEE_SELL")):
        warn.append(f"broker fees not set — assuming {cfg.fee_buy:.2%} buy / "
                    f"{cfg.fee_sell:.2%} sell")
    if env.get("TRADE_SIZING_MODE"):
        cfg.sizing_mode = env["TRADE_SIZING_MODE"].strip().lower()
    if env.get("TRADE_NOTIONAL_MIN_IDR"):
        cfg.notional_min_idr = float(env["TRADE_NOTIONAL_MIN_IDR"])
    if env.get("TRADE_NOTIONAL_MAX_IDR"):
        cfg.notional_max_idr = float(env["TRADE_NOTIONAL_MAX_IDR"])
    # Slot and heat caps became env-settable on 2026-08-15: portfolio_sim showed that
    # at 0.5% risk the book runs ~3 names and the 5-slot cap binds on 37% of days,
    # where at 1.5% it averaged 2.3 names and bound on 7%. The cap only becomes a
    # live constraint at small size, so it stops being a constant nobody touches.
    if env.get("TRADE_MAX_OPEN"):
        cfg.max_open = int(env["TRADE_MAX_OPEN"])
    if env.get("TRADE_HEAT_CAP_PCT"):
        cfg.heat_cap_pct = float(env["TRADE_HEAT_CAP_PCT"])
    if env.get("TRADE_SCALE_FRACTION"):
        cfg.scale_fraction = float(env["TRADE_SCALE_FRACTION"])
    if env.get("TRADE_SCALE_LEVEL_R"):
        cfg.scale_level_r = float(env["TRADE_SCALE_LEVEL_R"])
    return cfg, warn


# ---------------------------------------------------------------------- stop policy

def low_n_prior(p: Panel, sym: str, i: int, n: int = 5):
    """Lowest low over the n sessions ENDING at i inclusive.

    n=5 by design. The one-session low is the forbidden stop (see module docstring):
    it is a noise level, not a structure level.
    """
    lo = p.low.get(sym) or {}
    vals = [lo[j] for j in range(i - n + 1, i + 1) if j in lo]
    return min(vals) if vals else None


def stop_price(entry_ref: float, atr14: float, lo_struct, cfg: RiskConfig):
    """Return (stop_px, basis). stop_px == 0 means 'cannot be traded', with a reason.

    ATR governs. The structural low may WIDEN the stop, but only within
    `k_atr_max` ATR of the entry.

    The widening cap is not decoration. On 2026-08-06 GGRM's 5-session low was 18100
    — 12.3% below the close, because the stock had just run 25% in a month and the
    window still contained the base it broke out of. Taking the wider leg
    unconditionally made a perfectly tradeable name untradeable: a 1.5-ATR stop sat
    at 5.2%, well inside the ceiling. The structural level is only informative when
    it is NEAR the volatility stop; when it is far below it is describing an older
    regime, not this trade's risk.

    The tighter leg is never taken. That is the GGRM failure and it stays forbidden.
    """
    if not atr14 or atr14 <= 0 or entry_ref <= 0:
        return 0, "no_atr"

    atr_stop = entry_ref - cfg.k_atr * atr14
    basis = f"atr{cfg.k_atr:g}"
    cand = atr_stop
    if lo_struct:
        struct_stop = lo_struct - tick_size(lo_struct)
        widen_ok = (entry_ref - struct_stop) <= cfg.k_atr_max * atr14
        if struct_stop < cand and widen_ok:
            cand, basis = struct_stop, f"struct{cfg.struct_lookback}d"

    floor_dist = max(cfg.k_atr_min * atr14,
                     3 * tick_size(entry_ref),
                     cfg.min_stop_pct * entry_ref)
    if entry_ref - cand < floor_dist:
        cand = entry_ref - floor_dist
        basis += "+floor"

    stop = round_tick(cand, "down")
    if stop <= 0:
        return 0, "no_stop"
    dist = entry_ref - stop
    if dist <= 0:
        return 0, "no_stop"

    # The structural leg WIDENS the stop, and widening can push an otherwise
    # tradeable name past the width ceiling. When that happens, fall back to the
    # plain ATR stop rather than refusing the name: the 5-session low is an
    # enhancement, not a veto. ISAT on 2026-08-10 is the case — its structural stop
    # came to 11.7% and triggered a refusal while the 1.5-ATR stop sat at 7.1%,
    # comfortably inside the 8% ceiling.
    if dist > cfg.max_stop_pct * entry_ref:
        fallback = round_tick(entry_ref - cfg.k_atr * atr14, "down")
        fdist = entry_ref - fallback
        if fallback > 0 and 0 < fdist <= cfg.max_stop_pct * entry_ref:
            return fallback, f"atr{cfg.k_atr:g}(struct too wide)"
        return 0, f"too_wide({fdist / entry_ref:.1%})"
    return stop, basis


# --------------------------------------------------------------------------- sizing

def size_position(entry_ref: float, stop_px: int, adtv_idr, cfg: RiskConfig) -> dict:
    """Lots, actual risk, and WHICH constraint bound. Never silently returns zero."""
    out = {"lots": 0, "risk_idr": 0.0, "risk_pct": 0.0, "notional_idr": 0.0,
           "lots_raw": 0.0, "granularity_drag": 0.0, "binding": "", "flags": [],
           "reason": ""}
    # stop_px == 0 is stop_price()'s "cannot be traded" sentinel, NOT a stop at zero.
    # Treating it as a price makes stop_dist the entire share price, which sizes a
    # position off a 100% risk-per-share and reports it as if it were fine.
    if stop_px <= 0:
        out["binding"] = "no_stop"
        out["reason"] = "no valid stop"
        return out
    stop_dist = entry_ref - stop_px
    if stop_dist <= 0 or entry_ref <= 0:
        out["reason"] = "bad stop"
        return out

    per_lot_risk = stop_dist * SHARES_PER_LOT
    per_lot_cost = entry_ref * SHARES_PER_LOT

    if cfg.sizing_mode == "notional":
        # Target a position SIZE, scaled by how much the name can actually absorb.
        # "depends on market volume and value" = the 5%-of-ADTV ceiling decides where
        # between the min and max this name lands. Risk becomes an OUTPUT and is
        # reported, never assumed.
        ceiling = cfg.adtv_cap_pct * adtv_idr if adtv_idr else cfg.notional_max_idr
        target = min(cfg.notional_max_idr, max(cfg.notional_min_idr, min(ceiling,
                                                                        cfg.notional_max_idr)))
        if ceiling < cfg.notional_min_idr:
            out["binding"] = "liquidity"
            out["reason"] = (f"too thin: 5% of ADTV is only "
                             f"Rp{ceiling / 1e6:,.0f}m, under the "
                             f"Rp{cfg.notional_min_idr / 1e6:,.0f}m minimum size")
            return out
        lots_raw = target / per_lot_cost
        lots = math.floor(lots_raw)
        binding, binding_raw = ("liquidity" if ceiling < cfg.notional_max_idr
                                else "notional_target"), lots_raw
        if lots < 1:
            out["binding"] = "min_lot"
            out["reason"] = (f"one lot is Rp{per_lot_cost / 1e6:,.1f}m, over the "
                             f"Rp{target / 1e6:,.0f}m target")
            return out
        out.update(lots=lots, lots_raw=lots_raw,
                   risk_idr=lots * per_lot_risk,
                   risk_pct=lots * per_lot_risk / cfg.equity_idr,
                   notional_idr=lots * per_lot_cost,
                   granularity_drag=(lots / lots_raw - 1) if lots_raw else 0.0,
                   risk_use=(lots * per_lot_risk / cfg.risk_budget_idr)
                            if cfg.risk_budget_idr else 0.0,
                   binding=binding)
        if lots < 2:
            out["flags"].append("NO_SCALE")
        return out

    lots_raw = cfg.risk_budget_idr / per_lot_risk
    lots = math.floor(lots_raw)
    binding, binding_raw = "risk", lots_raw

    cap_notional_raw = (cfg.max_pos_pct * cfg.equity_idr) / per_lot_cost
    if math.floor(cap_notional_raw) < lots:
        lots, binding, binding_raw = math.floor(cap_notional_raw), "notional", cap_notional_raw
    if adtv_idr:
        cap_liq_raw = (cfg.adtv_cap_pct * adtv_idr) / per_lot_cost
        if math.floor(cap_liq_raw) < lots:
            lots, binding, binding_raw = math.floor(cap_liq_raw), "liquidity", cap_liq_raw

    if lots < 1:
        # Minimum tradeable size is one lot. Two honest answers, no third.
        if per_lot_risk <= cfg.min_lot_tolerance * cfg.risk_budget_idr and cap_notional_raw >= 1:
            lots, binding = 1, "min_lot"
            out["flags"].append("OVERSIZED_MIN_LOT")
        else:
            out["lots_raw"] = lots_raw
            out["reason"] = (f"min lot risks {per_lot_risk / cfg.risk_budget_idr:.2f}R"
                             if cap_notional_raw >= 1 else "min lot exceeds notional cap")
            out["binding"] = "min_lot"
            return out

    # Two different things were previously conflated under one number.
    #   granularity_drag = pure lot-ROUNDING loss, measured against whichever cap
    #                      actually bound. Small by nature (never worse than one lot).
    #   risk_use         = how much of the risk budget the position actually carries.
    #                      At a Rp3bn book the 5%-of-ADTV cap routinely holds a mid-cap
    #                      to a third of target risk; reporting that as "-67% drag" made
    #                      a liquidity constraint look like a rounding error.
    out.update(lots=lots,
               lots_raw=lots_raw,
               risk_idr=lots * per_lot_risk,
               risk_pct=lots * per_lot_risk / cfg.equity_idr,
               notional_idr=lots * per_lot_cost,
               granularity_drag=(lots / binding_raw - 1) if binding_raw else 0.0,
               risk_use=(lots * per_lot_risk / cfg.risk_budget_idr)
                        if cfg.risk_budget_idr else 0.0,
               binding=binding)
    if lots < 2:
        out["flags"].append("NO_SCALE")      # cannot take a partial: one lot, one exit
    return out


def r_multiple(entry_px: float, mark: float, r_idr: float, lots: int) -> float:
    """P&L in R. r_idr is FROZEN at open and passed in — never recomputed here.

    Recomputing R from the CURRENT stop is the single most common way a trade journal
    lies to its owner: once the stop trails up, every past loss shrinks retroactively.
    """
    if r_idr <= 0 or lots <= 0:
        return 0.0
    return (mark - entry_px) * lots * SHARES_PER_LOT / r_idr


def round_trip_cost(entry_px: float, lots: int, cfg: RiskConfig) -> float:
    notional = entry_px * lots * SHARES_PER_LOT
    return notional * (cfg.fee_buy + cfg.fee_sell)


def breakeven_stop(entry_px: float, cfg: RiskConfig) -> int:
    """Entry plus round-trip costs, rounded UP to a tick: a stop that actually breaks
    even rather than one that breaks even before fees and loses after them."""
    return round_tick(entry_px * (1 + cfg.fee_buy + cfg.fee_sell), "up")


# ------------------------------------------------------------------- profit-taking legs

@dataclass(frozen=True)
class LegPlan:
    """A profit-taking leg: sell `fraction` when price reaches `level`.

    kind="scale" leaves the remainder running on E1/E2. kind="full" closes the line
    and requires fraction == 1.0 — the two are the same machine, and keeping them one
    dataclass is what makes "is a partial better than a full exit at the same level?"
    a comparison rather than two separate studies.

    `level` is read against `trigger`: "R" multiples of the frozen risk-per-share, or
    "ATR" multiples of ATR14 at entry. Both are measured from the ENTRY price.
    """
    kind: str = "scale"        # "scale" | "full"
    trigger: str = "R"         # "R" | "ATR"
    level: float = 2.0
    fraction: float = 0.5
    fill: str = "limit_intraday"   # "limit_intraday" | "next_open" | "next_close"
    label: str = ""

    def __post_init__(self):
        if self.kind == "full" and self.fraction != 1.0:
            raise ValueError("a 'full' leg must sell the whole position")
        if not 0 < self.fraction <= 1.0:
            raise ValueError(f"fraction must be in (0, 1]: {self.fraction}")


def target_price(entry: float, r_ps: float, atr14: float, plan: LegPlan) -> int:
    """The price the leg triggers at, snapped UP to a valid tick.

    UP is the pessimistic direction for a SELLER: it demands price reach a strictly
    higher level than the raw target, so the fill is never assumed on a bar that only
    grazed it. Same convention trade_plan.py uses for entry_hi.
    """
    raw = entry + plan.level * (r_ps if plan.trigger == "R" else atr14)
    return round_tick(raw, "up")


def split_lots(lots: int, fraction: float) -> tuple[int, int]:
    """(sold, remainder) respecting whole lots. (0, lots) means 'cannot be split'.

    The SOLD leg floors, so the remainder never rounds up: selling more than intended
    is a sizing error wearing a rounding costume. A 1-lot position cannot scale at
    all, which is why size_position flags NO_SCALE below 2 lots.
    """
    if lots < 2 or not 0 < fraction < 1:
        return 0, lots
    sold = math.floor(lots * fraction)
    if sold < 1 or lots - sold < 1:
        return 0, lots
    return sold, lots - sold


def blend_r(legs: list[tuple[float, float]], entry: float, r_ps: float,
            cost: float) -> float:
    """R of a multi-leg exit. legs = [(weight, exit_px), ...], weights summing to 1.

    Weights are fractions of the ORIGINAL position and r_ps is the risk-per-share
    FROZEN AT OPEN. Neither is ever rescaled by what remains — rescaling makes R
    incomparable across configurations, which is the same trap that made the
    prior-low stop's meanR look twice as good as the ATR stop's while earning less.

    Fees need no special case: fee_buy is proportional to the fraction bought, so the
    per-share form is already exact and collapses to the single-leg expression at
    weight 1.
    """
    if r_ps <= 0:
        return 0.0
    return sum(w * (px - entry - entry * cost) for w, px in legs) / r_ps


def blend_excess(legs: list[tuple[float, float, float]], entry: float,
                 cost: float) -> float:
    """Excess-over-benchmark of a multi-leg exit.

    legs = [(weight, exit_px, bench_return_for_THAT_leg), ...].

    The per-leg benchmark is the point. A scale leg exits earlier and therefore faces
    a SHORTER index window; charging it the full holding period's market move credits
    it with returns it never carried, and the error's sign follows the index rather
    than cancelling.
    """
    return sum(w * ((px / entry - 1) - cost - b) for w, px, b in legs)


# ---------------------------------------------------------------- portfolio exposure

def portfolio_heat(positions: list[dict], equity: float) -> float:
    """Open capital at risk / equity.

    max(0, ...) because once the stop is above entry the position carries no capital
    risk. Counting a locked-in gain as heat would block new entries exactly when the
    book is working.
    """
    if equity <= 0:
        return 0.0
    tot = sum(max(0.0, (pos["entry_px"] - pos["stop_px"]) * pos["lots"] * SHARES_PER_LOT)
              for pos in positions)
    return tot / equity


def _returns(series: dict, idxs: list[int]) -> list[float]:
    out = []
    for a, b in zip(idxs, idxs[1:]):
        if series.get(a) and series.get(b):
            out.append(series[b] / series[a] - 1)
    return out


def beta(p: Panel, sym: str, i: int, n: int = 60):
    """cov(stock, IHSG) / var(IHSG) over trailing n sessions, on adjusted closes.

    Returns, not structure, so close_adj is correct here. None below 45 usable pairs.
    """
    cl = p.close.get(sym)
    if not cl:
        return None
    idxs = [j for j in range(i - n, i + 1) if j in cl and j in p.bench]
    if len(idxs) < 46:
        return None
    rs, rm = _returns(cl, idxs), _returns(p.bench, idxs)
    k = min(len(rs), len(rm))
    if k < 45:
        return None
    rs, rm = rs[-k:], rm[-k:]
    mm = sum(rm) / k
    var = sum((x - mm) ** 2 for x in rm)
    if var <= 0:
        return None
    ms = sum(rs) / k
    cov = sum((rs[t] - ms) * (rm[t] - mm) for t in range(k))
    return cov / var


def market_exposure(positions: list[dict], equity: float) -> float:
    """Beta-weighted gross / equity.

    GGRM, ISAT and ERAA are three different sectors, so a sector cap and a pairwise
    correlation cap both let all three through. On 2026-08-10 they were nonetheless
    one long-beta bet in a distributing market and all three failed together. This is
    the gate that encodes that lesson.
    """
    if equity <= 0:
        return 0.0
    return sum(abs(pos.get("beta") or 1.0) * pos["entry_px"] * pos["lots"] * SHARES_PER_LOT
               for pos in positions) / equity


def corr(p: Panel, a: str, b: str, i: int, n: int = 60):
    """Pearson correlation of daily adjusted returns. A cheap same-trade detector."""
    ca, cb = p.close.get(a), p.close.get(b)
    if not ca or not cb:
        return None
    idxs = [j for j in range(i - n, i + 1) if j in ca and j in cb]
    if len(idxs) < 46:
        return None
    ra, rb = _returns(ca, idxs), _returns(cb, idxs)
    k = min(len(ra), len(rb))
    if k < 45:
        return None
    ra, rb = ra[-k:], rb[-k:]
    ma, mb = sum(ra) / k, sum(rb) / k
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((x - mb) ** 2 for x in rb)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((ra[t] - ma) * (rb[t] - mb) for t in range(k))
    return cov / math.sqrt(va * vb)


def admit(candidate: dict, positions: list[dict], cfg: RiskConfig) -> tuple[bool, str]:
    """Portfolio-level gate. Returns (allowed, reason_if_not)."""
    if len(positions) >= cfg.max_open:
        return False, f"slots full ({cfg.max_open})"
    heat_after = portfolio_heat(positions + [candidate], cfg.equity_idr)
    if heat_after > cfg.heat_cap_pct:
        return False, f"heat {heat_after:.2%} > {cfg.heat_cap_pct:.2%}"
    sec = candidate.get("sector")
    if sec and sum(1 for x in positions if x.get("sector") == sec) >= cfg.max_per_sector:
        return False, f"sector cap ({sec})"
    exp_after = market_exposure(positions + [candidate], cfg.equity_idr)
    if exp_after > cfg.beta_gross_cap:
        return False, f"beta-gross {exp_after:.2f} > {cfg.beta_gross_cap:.2f}"
    return True, ""


# ------------------------------------------------------------------------- self-test

def _selftest() -> int:
    fails: list[str] = []

    def check(name, got, want, tol=0.0):
        ok = abs(got - want) <= tol if isinstance(want, (int, float)) else got == want
        print(f"  [{'ok' if ok else '!!'}] {name}: got {got!r} want {want!r}"
              + (f" (+-{tol})" if tol else ""))
        if not ok:
            fails.append(name)

    print("tick ladder")
    check("tick(150)", tick_size(150), 1)
    check("tick(300)", tick_size(300), 2)
    check("tick(1500)", tick_size(1500), 5)
    check("tick(4990)", tick_size(4990), 10)
    check("tick(21725)", tick_size(21725), 25)

    print("round_tick band boundaries")
    check("down 2005", round_tick(2005, "down"), 2000)
    check("down 1999", round_tick(1999, "down"), 1995)
    check("down 5010", round_tick(5010, "down"), 5000)
    check("up 4995", round_tick(4995, "up"), 5000)
    check("down 19848", round_tick(19848, "down"), 19825)
    check("down 498", round_tick(498, "down"), 498)
    check("up 501", round_tick(501, "up"), 505)

    print("loading panel (raw OHLC, 2y)")
    p = Panel().load()
    print(f"  {p.describe()}")
    i = p.didx.get("2026-08-06")
    if i is None:
        print("  [!!] panel has no 2026-08-06 — cannot run the GGRM assertions")
        return 1

    print("GGRM 2026-08-06 (last session in the local panel)")
    a_w = atr(p, "GGRM", i, 14, "wilder")
    a_s = atr(p, "GGRM", i, 14, "sma")
    close = p.raw_close["GGRM"][i]
    print(f"  close {close:.0f} | ATR14 wilder {a_w:.1f} ({a_w / close:.2%}) "
          f"| sma {a_s:.1f} ({a_s / close:.2%})")
    check("ATR14 is 3-5% of price", 1 if 0.03 <= a_w / close <= 0.05 else 0, 1)

    cfg = RiskConfig()
    lo5 = low_n_prior(p, "GGRM", i, cfg.struct_lookback)
    lo1 = p.low["GGRM"][i]
    stop, basis = stop_price(close, a_w, lo5, cfg)
    check("GGRM produces a TRADEABLE stop (not the too_wide sentinel)",
          1 if stop > 0 else 0, 1)
    if stop <= 0:
        print(f"  [!!] stop_price returned {basis} — the remaining assertions are moot")
        return 1
    print(f"  5d low {lo5:.0f} | 1d low {lo1:.0f} | stop {stop} ({basis}) "
          f"| dist {close - stop:.0f} = {(close - stop) / close:.2%} = "
          f"{(close - stop) / a_w:.2f} ATR")
    check("stop is a valid tick", stop % tick_size(stop), 0)
    check("stop strictly below the prior-day low (the forbidden level)",
          1 if stop < lo1 else 0, 1)
    check("stop within [k_atr_min, k_atr_max] ATR",
          1 if cfg.k_atr_min * a_w <= (close - stop) <= cfg.k_atr_max * a_w else 0, 1)
    check("stop distance under the width ceiling",
          1 if (close - stop) / close <= cfg.max_stop_pct else 0, 1)

    print("the too_wide sentinel must never be sized")
    z = size_position(close, 0, adtv_g := (p.adtv.get("GGRM") or {}).get(i), cfg)
    print(f"  size_position(stop_px=0) -> lots {z['lots']}, reason {z['reason']!r}")
    check("stop_px=0 yields zero lots", z["lots"], 0)
    check("stop_px=0 does not report risk", z["risk_idr"], 0.0)

    print("sizing, Rp100m book at 1.5% risk")
    adtv = adtv_g
    s = size_position(close, stop, adtv, cfg)
    print(f"  lots_raw {s['lots_raw']:.2f} -> {s['lots']} lots | risk Rp{s['risk_idr']:,.0f} "
          f"= {s['risk_pct']:.2%} | notional Rp{s['notional_idr']:,.0f} "
          f"= {s['notional_idr'] / cfg.equity_idr:.1%} equity | binding {s['binding']} "
          f"| drag {s['granularity_drag']:+.1%} {' '.join(s['flags'])}")
    check("sized at least one lot", 1 if s["lots"] >= 1 else 0, 1)
    check("risk never exceeds budget unless flagged",
          1 if (s["risk_idr"] <= cfg.risk_budget_idr or "OVERSIZED_MIN_LOT" in s["flags"]) else 0, 1)
    check("notional within cap",
          1 if s["notional_idr"] <= cfg.max_pos_pct * cfg.equity_idr + 1 else 0, 1)

    print("the min-lot boundary: take-and-flag, or skip — never silently oversize")
    # Two independent constraints bind here and the test must model BOTH, or it will
    # call correct behaviour a failure. A lot of GGRM costs Rp2.06m: at Rp6m that is
    # 34% of the book and breaches the concentration cap no matter how small the risk.
    per_lot_risk = (close - stop) * SHARES_PER_LOT
    per_lot_cost = close * SHARES_PER_LOT
    for eq in (10_000_000, 7_000_000, 6_000_000, 3_000_000):
        c2 = RiskConfig(equity_idr=eq)
        s2 = size_position(close, stop, adtv, c2)
        ratio = per_lot_risk / c2.risk_budget_idr
        affordable = per_lot_cost <= c2.max_pos_pct * eq
        print(f"  Rp{eq/1e6:.0f}m book: one lot = {ratio:.2f}R, "
              f"{per_lot_cost/eq:.0%} of equity ({'fits' if affordable else 'over cap'})"
              f" -> lots {s2['lots']} | risk {s2['risk_pct']:.2%} "
              f"| {s2['reason'] or 'sized'} {' '.join(s2['flags'])}")
        label = f"Rp{eq/1e6:.0f}m"
        if not affordable:
            check(f"{label} SKIPS on the notional cap",
                  1 if s2["lots"] == 0 and s2["reason"] else 0, 1)
        elif ratio <= 1.0:
            check(f"{label} sizes within budget",
                  1 if s2["lots"] >= 1 and s2["risk_idr"] <= c2.risk_budget_idr else 0, 1)
        elif ratio <= c2.min_lot_tolerance:
            check(f"{label} takes one lot and FLAGS it",
                  1 if "OVERSIZED_MIN_LOT" in s2["flags"] else 0, 1)
        else:
            check(f"{label} SKIPS (one lot = {ratio:.2f}R)",
                  1 if s2["lots"] == 0 and s2["reason"] else 0, 1)

    print("R math is frozen at open")
    r_idr = (close - stop) * s["lots"] * SHARES_PER_LOT
    check("entry = 0R", round(r_multiple(close, close, r_idr, s["lots"]), 6), 0.0)
    check("stop = -1R", round(r_multiple(close, stop, r_idr, s["lots"]), 3), -1.0, 0.002)

    print("beta / correlation")
    for sym in ("GGRM", "ISAT", "ERAA"):
        b = beta(p, sym, i)
        print(f"  beta({sym}) = {b:.2f}" if b else f"  beta({sym}) = None")
    c = corr(p, "GGRM", "ISAT", i)
    print(f"  corr(GGRM,ISAT) = {c:.2f}" if c is not None else "  corr = None")

    print("profit legs — lot splitting floors the SOLD side")
    check("1 lot cannot scale", split_lots(1, 0.5), (0, 1))
    check("2 lots split evenly", split_lots(2, 0.5), (1, 1))
    check("3 lots sell 1 keep 2", split_lots(3, 0.5), (1, 2))
    check("3 lots at 2/3 sell 2", split_lots(3, 2 / 3), (2, 1))
    check("10 lots at 1/3 sell 3", split_lots(10, 1 / 3), (3, 7))
    check("fraction 1.0 is not a split", split_lots(10, 1.0), (0, 10))

    print("profit legs — target snaps UP to a tick and never below the raw level")
    tp = target_price(2200, 150.0, 100.0, LegPlan(level=2.0, trigger="R"))
    check("ISAT +2R target", tp, 2500)
    check("target is on the ladder", tp % tick_size(tp), 0)
    tp2 = target_price(2200, 150.0, 100.0, LegPlan(level=1.7, trigger="R"))
    check("+1.7R rounds UP not near", tp2, 2460)   # raw 2455 -> up to 2460
    check("target >= raw level", 1 if tp2 >= 2200 + 1.7 * 150 else 0, 1)
    tpa = target_price(2200, 150.0, 100.0, LegPlan(level=3.0, trigger="ATR"))
    check("+3 ATR target", tpa, 2500)

    print("profit legs — blended R collapses EXACTLY to the single-leg form")
    ent_, rps_, cst_ = 2200.0, 150.0, 0.002
    single = (2500 - ent_ - ent_ * cst_) / rps_
    # exact float equality, not isclose: a generalisation that reorders the arithmetic
    # is precisely the bug this assertion exists to catch.
    check("one leg at w=1", blend_r([(1.0, 2500.0)], ent_, rps_, cst_) == single, True)
    check("two legs at one price", blend_r([(0.5, 2500.0), (0.5, 2500.0)],
                                           ent_, rps_, cst_) == single, True)
    half = blend_r([(0.5, 2500.0), (0.5, 2050.0)], ent_, rps_, cst_)
    check("scale half at +2R, half stopped", round(half, 4), round(
        0.5 * single + 0.5 * (2050 - ent_ - ent_ * cst_) / rps_, 4))
    check("blend_r ignores a later stop change",
          blend_r([(0.5, 2500.0), (0.5, 2050.0)], ent_, rps_, cst_) == half, True)

    print("profit legs — excess uses a PER-LEG benchmark window")
    xa = blend_excess([(0.5, 2500.0, 0.01), (0.5, 2400.0, 0.03)], ent_, cst_)
    xb = blend_excess([(0.5, 2500.0, 0.03), (0.5, 2400.0, 0.03)], ent_, cst_)
    check("shorter window for the early leg matters", round(xa - xb, 6), round(0.5 * 0.02, 6))

    print()
    if fails:
        print(f"[!!] {len(fails)} assertion(s) FAILED: {', '.join(fails)}")
        return 1
    print("[ok] all assertions passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="run assertions and exit")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
