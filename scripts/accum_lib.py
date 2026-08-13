#!/usr/bin/env python3
"""Accumulation features, score and buckets. Pure functions, no I/O, no network.

`py accum_lib.py --selftest` checks every formula, including the real BREN 2026-08-11
and 2026-08-12 numbers.

This module is imported by BOTH the validation harness and the board renderer, for the
same reason `build_momentum_board` imports `is_momentum` from `momentum_setup`: if the
thing that was tested and the thing that ships are two separate implementations of "the
same" rule, they drift, and the board's numbers stop meaning what the backtest measured.

Every formula and threshold here is specified in reference/accumulation.md, which was
written BEFORE this code. Thresholds marked GUESS are to be settled by walk-forward;
thresholds marked STRUCTURAL are not tunable, because changing them changes what the
number means rather than how strict it is.

THE ONE IDEA
    Net value cannot separate an accumulator from a market maker. On BREN 05->12 Aug, CC
    netted +33.7bn on 132.1bn of buying and 98.5bn of selling; DX netted +55.5bn on
    56.4bn of buying and 0.9bn of selling. Ranked by net they sit side by side. Ranked by
    the RATIO of the gross sides, one is churn at 57% and the other is an accumulator at
    98%. A ratio is not recoverable from a difference, which is why the gross partition
    is mandatory.
"""
from __future__ import annotations

import argparse
import math
import sys

BN = 1_000_000_000

# ---- definedness (STRUCTURAL) ------------------------------------------------
# A ratio of two small numbers is noise. Without this floor a broker that bought Rp40m
# and sold nothing scores osr = 1.00 and outranks a desk that moved Rp200bn.
#
# THE FLOOR MUST SCALE WITH THE WINDOW. ADTV is a DAILY average, so a fixed multiple of
# it is a different level of strictness at every window length. The first version used a
# flat 0.5 x ADTV and, applied to a 5-session window, silently returned None for every
# broker on every name — including TP on BREN — because no desk does half a day's total
# turnover inside five sessions. The board rendered with nothing in any bucket and no
# error, which is the worst way for a threshold to be wrong.
#
# Expressed instead as a share of the window's own gross activity: a stock trades about
# w x ADTV in value over w sessions, which is 2 x w x ADTV of broker-side gross (every
# rupiah has a buyer and a seller). A broker below GROSS_FLOOR_SHARE of that is too small
# a participant for its ratio to mean anything.
GROSS_FLOOR_IDR = 5 * BN
GROSS_FLOOR_SHARE = 0.02        # 2% of the window's two-sided gross

# ---- gates (GUESS unless noted) ----------------------------------------------
OSR_BUY = 0.80          # accumulator threshold
OSR_SELL = 0.20         # mirror, for distribution
OSR_ABSORB = 0.85       # the tighter bar on the entry bucket
SOFTRUN_MIN = 0.60
NET_MIN_IDR = 10 * BN
NET_MIN_ADTV = 0.20
COST_PASSIVE = -0.0015  # bid-side absorption
COST_PAYUP = 0.0025     # paying up: markup underway
JITTER_TOL = 0.10       # osr movement at W+-2 beyond which a row is 'unstable'

# ---- score normalisation caps (STRUCTURAL choice, values are GUESS) ----------
# Fixed caps, NOT the day's maximum. The crowded board normalises by day_max, which is
# right for a chatter ranking but wrong here: it makes scores incomparable across days
# and hostage to one outlier. A board whose job is "fire early" has to let you see that
# today's 62 is weaker than last Tuesday's 81.
CAP_OSR_LO, CAP_OSR_HI = 0.60, 0.95
CAP_ADTV_PCT = 1.50
CAP_SOFTRUN = 0.80
CAP_ABSORB = 0.60
CAP_COST_LO, CAP_COST_HI = 0.0025, -0.0025
CAP_SLICE = 0.70

# Five candidate weight vectors, DECLARED IN ADVANCE (accumulation.md 4.1). Walk-forward
# picks among them; nothing fits continuous weights. Order: osr, size, pers, absorb,
# cost, slice.
WEIGHT_VECTORS = {
    "V1_equal":   (1 / 6, 1 / 6, 1 / 6, 1 / 6, 1 / 6, 1 / 6),
    "V2_osr":     (0.45, 0.20, 0.15, 0.10, 0.05, 0.05),
    "V3_absorb":  (0.20, 0.15, 0.15, 0.40, 0.05, 0.05),
    "V4_design":  (0.28, 0.22, 0.16, 0.18, 0.10, 0.06),
    "V5_no_tilt": (0.28, 0.22, 0.16, 0.18, 0.10, 0.06),   # same weights, tilt forced 1.0
    # V6 exists because the calibration set inverted the slicing sign (accumulation.md
    # 7b): accumulators run NEGATIVE slice_z (large clips) and it is RETAIL that runs
    # positive. V6 feeds `block_z = -slice_z` into the same slot. Added as a DECLARED
    # ALTERNATIVE for the walk-forward to settle, not as a silent correction — the
    # observation is four stock-days and that is an anecdote, not evidence.
    "V6_block":   (0.28, 0.22, 0.16, 0.18, 0.10, 0.06),   # n_slice fed block_z
}
DEFAULT_VECTOR = "V4_design"
BLOCK_VECTORS = {"V6_block"}


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _norm(x, lo, hi):
    """Linear map lo->0, hi->1, clamped. Handles hi < lo (inverted scales, e.g. cost)."""
    if x is None or lo == hi:
        return None
    return clamp((x - lo) / (hi - lo))


# ---------------------------------------------------------------- flow shape

def osr(buy_value: float, sell_value: float, adtv: float | None = None,
        window: int = 1):
    """One-sidedness = buy / (buy + sell). None when the gross is too small to mean
    anything — and a None broker is EXCLUDED from scoring, never scored neutral.

    `window` is the number of sessions the buy/sell figures span. It is not optional in
    spirit: see GROSS_FLOOR_SHARE above for why a window-independent floor silently
    empties the board.
    """
    bv, sv = buy_value or 0.0, sell_value or 0.0
    gross = bv + sv
    floor = GROSS_FLOOR_IDR
    if adtv:
        floor = max(floor, GROSS_FLOOR_SHARE * 2.0 * max(1, window) * adtv)
    if gross < floor:
        return None
    return bv / gross


def softrun(daily_nets: dict[str, float], window_dates: list[str]) -> float | None:
    """Share of sessions in the window with a positive net.

    Primary over the strict run: one flat day must not reset a three-week campaign, and
    `broker_alpha.build_events`'s 3-day lookback cannot see an 8-day one at all.
    """
    if not window_dates:
        return None
    return sum(1 for d in window_dates if (daily_nets.get(d) or 0) > 0) / len(window_dates)


def run_buy(daily_nets: dict[str, float], upto: str) -> int:
    """Consecutive sessions of positive net ending at `upto`."""
    n = 0
    for d in reversed(sorted(d for d in daily_nets if d <= upto)):
        if (daily_nets.get(d) or 0) > 0:
            n += 1
        else:
            break
    return n


def adtv_pct(net: float, adtv: float | None) -> float | None:
    """Net as a fraction of the name's own 20-day liquidity.

    Deliberately UNCAPPED for multi-session windows: a broker taking 3x ADTV over 20
    days is the reading, not an error to be clipped.
    """
    if not adtv:
        return None
    return net / adtv


def ats(value: float, freq: float) -> float | None:
    """Average ticket in IDR."""
    return (value / freq) if freq else None


def slice_z(broker_freq, broker_value, total_freq, total_value) -> float | None:
    """ln(freq_share / value_share) — the order-slicing detector.

    Positive means the broker is spending more TRADES than its rupiah share implies, i.e.
    deliberate fragmentation. On BREN 2026-08-12 AK carried Rp94.9bn of net across 6,107
    buy prints — retail-sized tickets, institutional size.

    Relative, never absolute: the same rupiah ticket is 128x the lots on a Rp50 stock as
    on a Rp6,400 one, so an absolute clip-size rule flags every penny stock and no blue
    chip. Same reasoning as reference/scoring.md.
    """
    if not (broker_freq and broker_value and total_freq and total_value):
        return None
    fs, vs = broker_freq / total_freq, broker_value / total_value
    if fs <= 0 or vs <= 0:
        return None
    return math.log(fs / vs)


def cost_gap(buy_avg: float | None, vwap: float | None) -> float | None:
    """Where a broker's fills sat relative to the market's average price.

    Negative = absorbing on the bid (patient, the stealth signature).
    Positive = paying up (urgent, markup underway, you are late).

    This is the broker-level fix for a limitation the repo already documents:
    `overlay_test.structure()` concedes CLV/CMF20 infer accumulation from where the
    STOCK's close sat in its range and are "not a true volume-at-price profile".
    """
    if not buy_avg or not vwap:
        return None
    return buy_avg / vwap - 1.0


def absorb_score(daily_nets: dict[str, float], daily_xr: dict[str, float],
                 window_dates: list[str]) -> float | None:
    """Share of a broker's buying that happened on down/flat sessions.

    `is_momentum` requires rsi >= 55 AND dd60 >= -10% — price already strong. This
    requires the opposite sign on the day. The two rules are near-disjoint BY
    CONSTRUCTION, which is the mechanical reason 2026-08-11 was invisible to the
    momentum board.
    """
    pos = sum(max(daily_nets.get(d, 0.0), 0.0) for d in window_dates)
    if pos <= 0:
        return None
    weak = sum(max(daily_nets.get(d, 0.0), 0.0) for d in window_dates
               if (daily_xr.get(d) or 0.0) <= 0)
    return weak / pos


def absorb_today(net_today: float | None, xr_today: float | None,
                 adtv: float | None) -> bool:
    """Did this broker take size TODAY, on a weak day? The same-day form from
    accumulation.md 3.6.

    `absorb_score` (the window share) and this boolean are two different questions, and
    which belongs in the entry bucket was never settled by evidence — it was an arbitrary
    pick when the doc was written. It matters: on BREN 2026-08-11, TP cleared every other
    entry condition (osr5 97%, net5 Rp186.6bn against a Rp45bn floor, xr -0.25%,
    dd20 -10.8%) and was blocked solely by absorb_score_5 = 0.30, because only two of
    those five sessions were down. The window share asks "has most of this desk's recent
    buying been into weakness", which is a stricter and different claim from "it absorbed
    today".

    BOTH forms are now on the walk-forward grid (accumulation.md 6.1). Neither is
    validated; the board is observation-mode and says so.
    """
    if net_today is None or xr_today is None:
        return False
    floor = max(500_000_000.0, 0.05 * adtv) if adtv else 500_000_000.0
    return net_today >= floor and xr_today <= 0.005


def absorb_pair(accum_net: float, total_negative_net: float) -> float | None:
    """What share of the day's net distribution the accumulators took the other side of.
    Names WHO was absorbed, not just that absorption happened."""
    if not total_negative_net:
        return None
    return accum_net / abs(total_negative_net)


# ---------------------------------------------------------------- routing anomaly

def routing_anomaly(broker_gross_in_stock: float, stock_total_gross: float,
                    broker_market_gross: float, market_total_gross: float):
    """How over-represented a broker is in ONE name versus its normal market footprint.

    The BREN accumulators were TP (OCBC) and IF (Samuel Sekuritas) — not the desks the
    market watches. Routing size through a broker nobody tracks IS the disguise. Expressed
    as a ratio so it never depends on a hand-maintained list of "obscure" brokers, which
    would go stale the moment the pattern changed.
    """
    if not (stock_total_gross and broker_market_gross and market_total_gross):
        return None
    conc = broker_gross_in_stock / stock_total_gross
    prom = broker_market_gross / market_total_gross
    if prom <= 0 or conc <= 0:
        return None
    return conc / prom


def stealth_router(anomaly: float | None, osr_20: float | None,
                   slice_z_val: float | None) -> float | None:
    """Obscure route + one-sided + fragmented. All three or it is not this pattern."""
    if anomaly is None or osr_20 is None or anomaly <= 0:
        return None
    return math.log(anomaly) * osr_20 * max(0.0, slice_z_val or 0.0)


# ---------------------------------------------------------------- windows

def window_factor(net_5: float | None, net_20: float | None,
                  net_60: float | None) -> float:
    """Multi-window agreement gate. NEVER an average.

    Window choice flips the sign: BK was +Rp73.1bn in BREN over 8 days and -Rp413.4bn
    over 90. Averaging the two one-sidedness figures (0.83 and 0.31) gives 0.57, which
    reads as "churn" — wrong in a new way. The information lives in the disagreement, so
    a conflict is penalised and BADGED, never smoothed away.
    """
    if net_5 is None or net_60 is None:
        return 1.0
    s5, s60 = (net_5 > 0), (net_60 > 0)
    if s5 != s60:
        return 0.60
    if net_20 is not None and (net_20 > 0) == s5:
        return 1.15
    return 1.00


def window_conflict(net_5, net_60) -> bool:
    return (net_5 is not None and net_60 is not None
            and (net_5 > 0) != (net_60 > 0))


def jitter_unstable(osr_minus: float | None, osr_at: float | None,
                    osr_plus: float | None, tol: float = JITTER_TOL) -> bool:
    """True when one-sidedness moves more than `tol` for a +-2 session change in the
    window. A real 8-day campaign is insensitive to where you start counting; a single
    block trade is not."""
    vals = [v for v in (osr_minus, osr_at, osr_plus) if v is not None]
    return len(vals) >= 2 and (max(vals) - min(vals)) > tol


def quality_tilt(best_rank: int | None, n_brokers: int | None) -> float:
    """Broker-quality tilt, byte-identical to build_momentum_board.rank_score so the two
    boards cannot silently disagree about what a good broker is.

    TILT, NEVER GATE — broker skill survived walk-forward by only ~2-3%, and ranking by
    hit-rate instead of mean excess is already refuted (-2.3%/-2.8% vs +2.3%/+2.1%).
    """
    if not best_rank or not n_brokers or n_brokers < 2:
        return 1.0
    return 1.2 - 0.4 * (best_rank - 1) / max(1, n_brokers - 1)


# ---------------------------------------------------------------- score

def block_z(slice_z_val: float | None) -> float | None:
    """-slice_z. Positive when a broker takes size in LARGE clips.

    On the calibration set the accumulators sat at slice_z -0.68 to -1.07 (TP did 14% of
    BREN's prints for 40% of its value) while the retail brokers XL and YP sat at +0.34 to
    +1.04 on Rp5-7m tickets. High frequency relative to value is the crowd, not the whale.
    """
    return None if slice_z_val is None else -slice_z_val


def retail_slice(slice_z_retail: float | None) -> float | None:
    """The same number pointed at the question it actually answers: how heavily is the
    retail cohort participating? Feeds the trap metric's retail_absorb leg as a
    MEASUREMENT rather than a broker label."""
    return slice_z_retail


def normalised(feat: dict, vector: str = DEFAULT_VECTOR) -> dict:
    """The six normalised score terms. None where the input is missing."""
    sz = feat.get("slice_z20")
    slice_term = block_z(sz) if vector in BLOCK_VECTORS else sz
    return {
        "n_osr": _norm(feat.get("osr20"), CAP_OSR_LO, CAP_OSR_HI),
        "n_size": _norm(feat.get("adtv_pct20"), 0.0, CAP_ADTV_PCT),
        "n_pers": _norm(feat.get("softrun20"), 0.0, CAP_SOFTRUN),
        "n_absorb": _norm(feat.get("absorb20"), 0.0, CAP_ABSORB),
        "n_cost": _norm(feat.get("cost_gap20"), CAP_COST_LO, CAP_COST_HI),
        "n_slice": _norm(slice_term, 0.0, CAP_SLICE),
    }


def stealth_score(feat: dict, vector: str = DEFAULT_VECTOR,
                  tilt: float = 1.0, wf: float = 1.0) -> float:
    """0-100 composite. Missing terms contribute 0 and their weight is NOT
    redistributed — a name with no cost data should score lower than one with it, not be
    silently promoted by having its remaining terms re-weighted upward."""
    w = WEIGHT_VECTORS.get(vector, WEIGHT_VECTORS[DEFAULT_VECTOR])
    n = normalised(feat, vector)
    terms = (n["n_osr"], n["n_size"], n["n_pers"],
             n["n_absorb"], n["n_cost"], n["n_slice"])
    raw = sum((t or 0.0) * wi for t, wi in zip(terms, w))
    if vector == "V5_no_tilt":
        tilt = 1.0
    return 100.0 * raw * tilt * wf


# ---------------------------------------------------------------- buckets

BUCKETS = ("distribution", "markup", "absorption", "stealth", "churn", "cooling", "none")


def hard_gate(osr20: float | None, net20: float | None, adtv: float | None,
              theta_osr: float = OSR_BUY, theta_adtv: float = NET_MIN_ADTV) -> bool:
    """The structural gate from accumulation.md 4.3, applied BEFORE scoring.

    A broker must be one-sided AND large before its state is worth naming. Kept separate
    from classify_bucket because the two have different jobs: this decides who is worth
    evaluating, that decides what the evaluation says. Conflating them was a real bug —
    without this, the theta_osr axis of the walk-forward grid never reached the stealth
    bucket and nine grid cells returned an identical Level-2 count.

    The BOARD deliberately does NOT filter on this: `churn` exists precisely to show a
    busy market maker being rejected, and a gated-out broker can never be displayed as
    rejected. The HARNESS does apply it, because Level 2 counts only actionable buckets.
    """
    if osr20 is None or net20 is None:
        return False
    floor = max(NET_MIN_IDR, theta_adtv * adtv) if adtv else NET_MIN_IDR
    return osr20 >= theta_osr and net20 >= floor


def classify_bucket(row: dict) -> str:
    """First match wins, in the order set out in accumulation.md 4.3.

    Churn is evaluated LAST on purpose: scoring.md already establishes that churn is the
    ABSENCE of a read and must never pre-empt one — it once masked a real institutional
    print in MDKA.

    Only `absorption` and `stealth` ask you to buy. Everything else exists to be looked
    at and not traded, exactly like the momentum board's Exhaustion section.
    """
    osr20 = row.get("osr20")
    osr20_prev = row.get("osr20_prev")
    osr1d = row.get("osr1d")
    osr5 = row.get("osr5")
    xr = row.get("xr")
    xr5 = row.get("xr5")
    xr20 = row.get("xr20")
    rvol5 = row.get("rvol5")
    stealth = row.get("stealth") or 0.0
    net5 = row.get("net5")
    adtv = row.get("adtv")
    softrun20 = row.get("softrun20")
    softrun5 = row.get("softrun5")
    absorb5 = row.get("absorb5")
    dd20 = row.get("dd20")
    gross20 = row.get("gross20")
    cost5 = row.get("cost_gap5")

    # 1 — distribution / retail trap
    if (osr20_prev is not None and osr20_prev >= OSR_BUY
            and osr1d is not None and osr1d <= 0.35
            and xr is not None and xr >= 0.05
            and rvol5 is not None and rvol5 >= 2.5):
        return "distribution"

    # 2 — markup underway
    if (stealth >= 40 and xr5 is not None and xr5 >= 0.08
            and cost5 is not None and cost5 >= COST_PAYUP):
        return "markup"

    # 3 — absorption on weakness (THE ENTRY)
    # `absorb_mode` selects between the two forms in accumulation.md 3.6. Default is the
    # same-day boolean; "window" restores absorb_score_5 >= 0.60. Both are on the
    # walk-forward grid because neither has evidence behind it yet — see absorb_today().
    absorb_ok = (row.get("absorb_today") is True
                 if row.get("absorb_mode", "today") == "today"
                 else (absorb5 is not None and absorb5 >= 0.60))
    # The grid axes must reach the BUCKET, not only the Level-1 event definition. Without
    # these overrides every cell of the theta sweep produced an identical Level-2 count
    # (494 in the first gate run), i.e. the sweep measured nothing at all while looking
    # like it had run.
    th_osr = row.get("theta_osr", OSR_ABSORB)
    th_adtv = row.get("theta_adtv", 0.30)
    if (osr5 is not None and osr5 >= th_osr
            and net5 is not None and adtv
            and net5 >= max(NET_MIN_IDR, th_adtv * adtv)
            and absorb_ok
            and xr is not None and xr <= 0
            and dd20 is not None and dd20 <= -0.02):
        return "absorption"

    # 4 — stealth accumulation.
    # No osr condition here, deliberately: accumulation.md 4.3 puts one-sidedness in the
    # HARD GATE BEFORE SCORING ("at least one broker with osr20 >= 0.80"), not inside this
    # predicate. Callers apply that gate when selecting which brokers to evaluate — see
    # hard_gate() — which is also what makes theta_osr bite on Level 2 in the harness.
    if (stealth >= 35
            and xr20 is not None and abs(xr20) <= 0.08
            and rvol5 is not None and rvol5 <= 1.3
            and softrun20 is not None and softrun20 >= 0.55):
        return "stealth"

    # 5 — churn (LAST, see docstring)
    if (osr20 is not None and 0.40 <= osr20 <= 0.60
            and gross20 is not None and adtv and gross20 >= 3 * adtv):
        return "churn"

    # 6 — cooling: was actionable recently, campaign has stopped
    if row.get("was_actionable_10d") and (
            (softrun5 is not None and softrun5 <= 0.20)
            or (osr5 is not None and osr5 <= 0.50)):
        return "cooling"

    return "none"


# ---------------------------------------------------------------- retail trap

def trap_rate(accum_sell_value_on_d: float, position_value_at_d1: float):
    """"The whale sold X% of the position it had just built, on the day it marked it up."

    The denominator is STRUCTURAL, not tunable: changing it changes what the number
    means. The accumulator set must be frozen at D-1 — selecting it with D's own data
    makes the metric circular and guarantees a large answer.
    """
    if not position_value_at_d1:
        return None
    return accum_sell_value_on_d / position_value_at_d1


def trap_tag(rate: float | None, retail_absorb: float | None,
             n_still_buying: int = 0, n_accum: int = 0) -> str:
    if rate is None:
        return "unknown"
    hits = sum([rate >= 0.35, (retail_absorb or 0) >= 0.30])
    if hits == 2:
        return "RETAIL TRAP (confirmed)"
    if hits == 1:
        return "PARTIAL DISTRIBUTION"
    return f"MARKUP, WHALE STILL LONG ({n_still_buying}/{n_accum} still net buying)"


# ---------------------------------------------------------------- selftest

def _selftest() -> int:
    fails = []

    def check(name, got, want, tol=1e-6):
        if want is None:
            ok = got is None
        elif isinstance(want, bool):
            ok = got is want
        elif isinstance(want, (int, float)) and isinstance(got, (int, float)):
            ok = abs(got - want) <= tol
        else:
            ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<44} got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    # --- osr, on real BREN 05->12 Aug figures
    check("TP osr (accumulator)", round(osr(213.8 * BN, 8.6 * BN), 4), 0.9613, 1e-4)
    check("CC osr (churn)", round(osr(132.1 * BN, 98.5 * BN), 4), 0.5729, 1e-4)
    check("DX osr (98% one-way)", round(osr(56.4 * BN, 0.9 * BN), 4), 0.9843, 1e-4)
    check("DP osr (pure seller)", osr(0.0, 146.9 * BN), 0.0)
    check("definedness guard excludes tiny gross", osr(40e6, 0.0), None)
    check("guard scales with ADTV", osr(6 * BN, 0.0, adtv=154.3 * BN, window=1), None)
    # The window-scaling bug that emptied the board: TP's real 5-session gross on BREN
    # must be ADMITTED at w=5, and a flat 0.5xADTV floor rejected it.
    check("TP 5d gross admitted at w=5",
          round(osr(105 * BN, 3 * BN, adtv=154.3 * BN, window=5), 3), 0.972)
    check("same gross rejected at w=20 (20x the bar)",
          osr(105 * BN, 3 * BN, adtv=154.3 * BN, window=20), None)
    check("TP 20d gross admitted at w=20",
          round(osr(199.6 * BN, 10.8 * BN, adtv=154.3 * BN, window=20), 3), 0.949)

    # --- persistence
    nets = {"d1": 5.0, "d2": -1.0, "d3": 3.0, "d4": 2.0, "d5": 4.0}
    check("softrun ignores one flat day", softrun(nets, list(nets)), 0.8)
    check("run_buy is strict", run_buy(nets, "d5"), 3)
    check("run_buy stops at the gap", run_buy(nets, "d2"), 0)

    # --- slicing: AK on BREN 2026-08-12 carried big value on many small prints
    z = slice_z(broker_freq=6107, broker_value=94.9 * BN,
                total_freq=20000, total_value=380 * BN)
    check("AK slice_z > 0 (fragmented)", z > 0, True)
    check("slice_z is 0 when shares match",
          round(slice_z(100, 10 * BN, 1000, 100 * BN), 9), 0.0)
    check("ats", ats(94.9 * BN, 6107) / 1e6 > 15, True)

    # --- cost gap
    check("TP absorbed at the bid on 08-11",
          round(cost_gap(3309.60, 3310.0), 6), -0.000121, 1e-6)
    check("paying up reads positive", cost_gap(3400.0, 3300.0) > COST_PAYUP, True)

    # --- absorption
    dn = {"a": 10.0, "b": 20.0, "c": 5.0}
    xr = {"a": -0.02, "b": -0.01, "c": 0.03}
    check("absorb_score", absorb_score(dn, xr, ["a", "b", "c"]), 30.0 / 35.0)
    check("absorb_pair (BREN 08-11)",
          round(absorb_pair(55.3 * BN, -77.3 * BN), 3), 0.715, 1e-3)

    # --- routing anomaly: a 0.2% market-share broker doing 20% of one name
    check("routing_anomaly = 100x",
          round(routing_anomaly(20, 100, 2, 1000), 1), 100.0)

    # --- windows: the BK sign-flip trap
    check("BK 5d buyer / 60d seller is penalised",
          window_factor(73.1 * BN, 50 * BN, -413.4 * BN), 0.60)
    check("all three agree -> boost",
          window_factor(10.0, 10.0, 10.0), 1.15)
    check("mixed middle -> neutral",
          window_factor(10.0, -1.0, 10.0), 1.00)
    check("conflict detected", window_conflict(73.1 * BN, -413.4 * BN), True)
    check("jitter flags an unstable osr", jitter_unstable(0.95, 0.80, 0.78), True)
    check("jitter passes a steady campaign", jitter_unstable(0.95, 0.96, 0.94), False)

    # --- tilt matches the momentum board exactly
    check("tilt best rank", quality_tilt(1, 5), 1.2)
    check("tilt worst rank", round(quality_tilt(5, 5), 6), 0.8)
    check("tilt with no ranking", quality_tilt(None, None), 1.0)

    # --- score
    strong = {"osr20": 0.95, "adtv_pct20": 1.2, "softrun20": 0.8,
              "absorb20": 0.7, "cost_gap20": -0.002, "slice_z20": 0.5}
    weak = {"osr20": 0.62, "adtv_pct20": 0.1, "softrun20": 0.2,
            "absorb20": 0.1, "cost_gap20": 0.004, "slice_z20": None}
    s_strong = stealth_score(strong)
    s_weak = stealth_score(weak)
    check("strong scores high", s_strong > 80, True)
    check("weak scores low", s_weak < 20, True)
    check("score is capped at 100 before tilt", stealth_score(strong) <= 100.0, True)
    check("V5 ignores tilt",
          stealth_score(strong, "V5_no_tilt", tilt=1.2),
          stealth_score(strong, "V5_no_tilt", tilt=1.0))
    check("conflict penalty bites",
          round(stealth_score(strong, tilt=1.0, wf=0.60), 4),
          round(s_strong * 0.60, 4))
    check("missing terms are not re-weighted up",
          stealth_score({"osr20": 0.95}) < stealth_score(strong), True)

    # The calibration-set sign inversion: TP (accumulator) ran slice_z -1.05 while XL
    # (retail) ran +1.04. V4 rewards XL's shape; V6 rewards TP's. Both are on the grid so
    # the walk-forward decides, and the two must NOT score the same.
    whale = dict(strong, slice_z20=-1.05)
    crowd = dict(strong, slice_z20=+1.04)
    check("block_z flips the sign", block_z(-1.05), 1.05)
    check("V4 scores the retail shape higher",
          stealth_score(crowd, "V4_design") > stealth_score(whale, "V4_design"), True)
    check("V6 scores the whale shape higher",
          stealth_score(whale, "V6_block") > stealth_score(crowd, "V6_block"), True)

    # --- buckets. BREN 2026-08-11 must be the ENTRY.
    # The REAL TP/BREN figures on 2026-08-11, from the gross partition.
    bren_0811 = {"osr5": 0.97, "osr20": 0.94, "net5": 186.6 * BN, "adtv": 149 * BN,
                 "absorb5": 0.30, "absorb_today": True,
                 "xr": -0.0025, "dd20": -0.108, "rvol5": 0.9,
                 "softrun20": 0.7, "stealth": 71, "xr20": 0.01}
    check("BREN 2026-08-11 -> absorption", classify_bucket(bren_0811), "absorption")
    # Under the window form BREN does not vanish — it falls through to the watchlist
    # bucket instead of the entry bucket. So the choice between the two forms decides
    # WHEN you act, not WHETHER the name is seen at all. Worth knowing before the
    # walk-forward picks one.
    check("window mode demotes it to stealth, not none",
          classify_bucket({**bren_0811, "absorb_mode": "window"}), "stealth")
    check("absorb_today needs size AND a weak day",
          absorb_today(55 * BN, -0.0025, 149 * BN), True)
    check("absorb_today rejects a strong day",
          absorb_today(55 * BN, 0.04, 149 * BN), False)
    check("absorb_today rejects a small print",
          absorb_today(1 * BN, -0.02, 149 * BN), False)

    trap_row = {"osr20_prev": 0.95, "osr1d": 0.20, "xr": 0.13, "rvol5": 2.8}
    check("a real hand-over -> distribution", classify_bucket(trap_row), "distribution")

    churn_row = {"osr20": 0.57, "gross20": 4 * 154.3 * BN, "adtv": 154.3 * BN}
    check("CC-style churn -> churn", classify_bucket(churn_row), "churn")

    quiet = {"stealth": 44, "xr20": 0.02, "rvol5": 1.1, "softrun20": 0.7}
    check("quiet campaign -> stealth", classify_bucket(quiet), "stealth")

    # Churn must not pre-empt a real read: same row, but it also qualifies as stealth.
    both = dict(churn_row)
    both.update({"stealth": 44, "xr20": 0.02, "rvol5": 1.1, "softrun20": 0.7})
    check("stealth beats churn when both match", classify_bucket(both), "stealth")

    check("nothing matches -> none", classify_bucket({}), "none")

    # --- trap metric, on the real BREN numbers
    check("BREN trap_rate is small",
          round(trap_rate(4.0 * BN, 359.6 * BN), 4), 0.0111, 1e-4)
    check("BREN tag", trap_tag(0.0111, 0.05, 1, 2).startswith("MARKUP"), True)
    check("a genuine trap tags confirmed",
          trap_tag(0.55, 0.42), "RETAIL TRAP (confirmed)")
    check("one leg only -> partial", trap_tag(0.55, 0.05), "PARTIAL DISTRIBUTION")

    print()
    if fails:
        print(f"SELFTEST FAILED: {len(fails)} check(s) -> {', '.join(fails)}")
        return 1
    print(f"SELFTEST PASSED")
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
