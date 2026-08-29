"""Pure formulas for the joint-lift (Markovian lift) study. Framework: reference/lift.md.

Imported by lift_probe.py and lift_test.py. No I/O, no API, no globals with state — so
`--selftest` can pin every number by hand.

THE STATE. Semi-Markov (state, age): for ANY sojourn-time law the pair (current state,
age in that state) is Markov, because the residual-life law depends on the past only
through age. That is the lift used here. The Hawkes/exponential lift is NOT used: its
kernel is exactly what Lillo-Mike-Farmer rules out, and at daily aggregation its
parameters are not identified at all (not invariant to bin size; any sub-stochastic lag-1
matrix has spectral radius below 1 trivially). See reference/lift.md section 2.

BANNED. Pooled-hazard "the hazard declines, therefore memory" reasoning. A mixture of
geometric sojourns across heterogeneous tickers is provably completely monotone, so a
decreasing pooled hazard is guaranteed before any data are seen. There is deliberately no
hazard-shape test in this file; the kill switch is occupancy plus the price-only twin.
"""

from __future__ import annotations

import math
import statistics
import sys

# Barriers reuse the validated execution layer: trade_lib.RiskConfig.k_atr = 1.5.
UP_ATR = 2.0
DOWN_ATR = 1.5
# Driftless first-passage benchmark for asymmetric barriers, b/(a+b). Hard, not estimated.
DRIFTLESS_NULL = DOWN_ATR / (UP_ATR + DOWN_ATR)   # 0.42857...
MAX_HOLD = 30          # sessions; beyond this a path is UNRESOLVED, never silently a loss
ATR_N = 14
BLOCK_DAYS = 30        # bootstrap block; must exceed the 95th pct holding period
MIN_BLOCKS_INFERENTIAL = 15


# ---------------------------------------------------------------- the price-only twin

def price_twin(high: float | None, low: float | None,
               close: float | None) -> bool | None:
    """The K2 twin event, built from OHLCV alone with ZERO broker data.

        c_t = 1 iff Close > (High + Low) / 2

    which is algebraically the sign of (Close - typical price), typical = (H+L+C)/3:
        C > (H+L+C)/3  <=>  3C > H+L+C  <=>  2C > H+L  <=>  C > (H+L)/2.

    This is the kill test. If the broker joint-lift state is largely recoverable from this
    one line of arithmetic, the broker data is decorative and this is the sixth momentum
    thesis. Returns None when the bar is degenerate (H == L, e.g. a limit-locked session),
    because the sign is then undefined rather than False.
    """
    if high is None or low is None or close is None:
        return None
    if high <= low:
        return None
    return close > (high + low) / 2.0


# ---------------------------------------------------------------- runs and ages

def runs_strict(flag_by_i: dict[int, bool]) -> list[tuple[int, int]]:
    """Onset-anchored runs of consecutive TRUE sessions -> [(onset_i, length), ...].

    K_strict, as pre-registered: a run breaks on any non-lifting day AND on any session
    the symbol is absent from the panel. A gap is a break, not a bridge — bridging a gap
    would silently manufacture length out of missing data, which on IDX means out of a
    suspension or a non-trading name.
    """
    if not flag_by_i:
        return []
    out: list[tuple[int, int]] = []
    onset: int | None = None
    prev: int | None = None
    for i in sorted(flag_by_i):
        on = bool(flag_by_i[i])
        contiguous = prev is not None and i == prev + 1
        if on and onset is not None and contiguous:
            pass                              # run continues
        elif on:
            if onset is not None:
                out.append((onset, prev - onset + 1))
            onset = i                         # new run starts here
        else:
            if onset is not None:
                out.append((onset, prev - onset + 1))
            onset = None
        prev = i
    if onset is not None:
        out.append((onset, prev - onset + 1))
    return out


def age_bin(k: int) -> str:
    """Pre-registered age bins. k>=3 is one terminal bin: finer is not estimable, and
    pretending otherwise is how a 25-observation cell prints a confident number."""
    if k <= 1:
        return "1"
    if k == 2:
        return "2"
    return "3+"


def ladder(runs: list[tuple[int, int]], max_k: int = 6) -> dict[int, int]:
    """How many runs REACH age k, for k = 1..max_k. Survival counts, not exact-length
    counts: a run of length 4 reaches ages 1, 2, 3 and 4."""
    out = {k: 0 for k in range(1, max_k + 1)}
    for _, ln in runs:
        for k in range(1, min(ln, max_k) + 1):
            out[k] += 1
    return out


def block_id(i: int, block: int = BLOCK_DAYS) -> int:
    """Non-overlapping calendar-block index for session i. Used to count how many
    INDEPENDENT market episodes stand behind a cell — the number that decides whether an
    interval is inferential or merely descriptive."""
    return i // block


# ---------------------------------------------------------------- ATR and first passage

def true_range(high: float, low: float, prev_close: float | None) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr_pct(highs: list[float], lows: list[float], closes: list[float],
            n: int = ATR_N) -> float | None:
    """ATR(n) as a FRACTION of the last close. Inputs must be ADJUSTED and in time order,
    ending on the signal day. Returns None when history is short."""
    if len(closes) < n + 1 or closes[-1] <= 0:
        return None
    trs = [true_range(highs[j], lows[j], closes[j - 1])
           for j in range(len(closes) - n, len(closes))]
    if len(trs) < n:
        return None
    return statistics.fmean(trs) / closes[-1]


def first_passage(entry: float, atrp: float,
                  path_high: list[float], path_low: list[float],
                  up: float = UP_ATR, down: float = DOWN_ATR,
                  max_hold: int = MAX_HOLD) -> str:
    """Walk the ADJUSTED forward path and report which barrier is touched first.

    Returns "win" | "loss" | "unresolved".

    Bars are ADJUSTED, deliberately. A raw path books a corporate action as a barrier
    touch: PACK 2026-01-12 was raw -91.7% against adjusted +9.4%, and 17 of the 20 worst
    "stop losses" in the earlier trade backtest were ex-dates. Structure is judged on raw
    bars everywhere else in this repo; a BARRIER is a return question, so it takes the
    adjusted series.

    Same-bar ambiguity resolves PESSIMISTICALLY: if a single bar spans both barriers we
    cannot know the order from daily data, so it is booked as a loss. The alternative
    flatters exactly the violent bars the stop exists for.

    Never returns "loss" for a path that simply ran out of days — that is "unresolved",
    counted and reported separately.
    """
    if entry <= 0 or atrp is None or atrp <= 0:
        return "unresolved"
    hi_b = entry * (1.0 + up * atrp)
    lo_b = entry * (1.0 - down * atrp)
    for t in range(min(max_hold, len(path_high))):
        h, l = path_high[t], path_low[t]
        if h is None or l is None:
            return "unresolved"
        hit_up, hit_dn = h >= hi_b, l <= lo_b
        if hit_up and hit_dn:
            return "loss"
        if hit_dn:
            return "loss"
        if hit_up:
            return "win"
    return "unresolved"


def pi_hat(outcomes: list[str]) -> dict:
    """P(hit up before down), counted directly from empirical paths.

    NOT computed via (I - Q)^-1 R. That factorisation needs next-day return independent of
    next state, which is false by construction here: tomorrow's state IS a function of
    tomorrow's price versus tomorrow's VWAP. The bias runs optimistic, on exactly the
    number a screener would print.

    Unresolved paths are excluded from the ratio and reported, so the reader can see how
    much of the sample the horizon threw away.
    """
    w = sum(1 for o in outcomes if o == "win")
    l = sum(1 for o in outcomes if o == "loss")
    u = sum(1 for o in outcomes if o == "unresolved")
    n = w + l
    return {"pi": (w / n) if n else None, "n_resolved": n, "wins": w, "losses": l,
            "unresolved": u,
            "E_R": (UP_ATR + DOWN_ATR) * (w / n) - DOWN_ATR if n else None}


def expected_r(pi: float) -> float:
    """E = 2.0*pi - 1.5*(1-pi) = 3.5*pi - 1.5, in ATR units. Zero at the driftless null."""
    return (UP_ATR + DOWN_ATR) * pi - DOWN_ATR


# ---------------------------------------------------------------- inference helpers

def date_block_bootstrap(per_date: dict[int, list], stat, n_boot: int = 2000,
                         block: int = BLOCK_DAYS, seed: int = 7,
                         lo_q: float = 0.10, hi_q: float = 0.90) -> dict:
    """Moving-block bootstrap resampling WHOLE PANEL DATES.

    `per_date` maps session index -> list of that date's observations; every ticker's row
    for a date travels together, because the dominant dependence on IDX is same-calendar-
    day cross-sectional co-movement. `stat` maps a flat list of observations to a float or
    None.

    Block defaults to 30 trading days, not ceil(n^(1/3)): the block must exceed the 95th
    percentile of holding period (~15-25 days under these barriers). A 10-day block splits
    treatment-outcome pairs across boundaries and biases the tail DOWNWARD, i.e. silently
    against the alternative.

    Bands are 10th/90th by default, not 5th/95th. With roughly 15 independent blocks a 5%
    tail is not resolvable, and reporting one is the same class of error as a z-test on
    clustered events.
    """
    import random
    dates = sorted(per_date)
    n = len(dates)
    if n < block * 2:
        return {"point": stat([x for d in dates for x in per_date[d]]),
                "lo": None, "hi": None, "n_dates": n, "block": block,
                "note": "too few dates for a block bootstrap"}
    nblocks = math.ceil(n / block)
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        obs = []
        for _ in range(nblocks):
            s = rng.randrange(n)
            for t in range(block):
                obs.extend(per_date[dates[(s + t) % n]])
        v = stat(obs)
        if v is not None:
            draws.append(v)
    draws.sort()
    if not draws:
        return {"point": None, "lo": None, "hi": None, "n_dates": n, "block": block}
    return {"point": stat([x for d in dates for x in per_date[d]]),
            "lo": draws[int(lo_q * len(draws))],
            "hi": draws[min(len(draws) - 1, int(hi_q * len(draws)))],
            "n_dates": n, "block": block, "n_draws": len(draws)}


def blocks_with_treatment(session_idxs, block: int = BLOCK_DAYS) -> int:
    """Count of DISTINCT non-overlapping calendar blocks contributing observations.
    Printed next to every interval; below MIN_BLOCKS_INFERENTIAL the interval is
    descriptive, not inferential, and must be labelled so."""
    return len({block_id(i, block) for i in session_idxs})


def is_inferential(n_blocks: int) -> bool:
    return n_blocks >= MIN_BLOCKS_INFERENTIAL


# ---------------------------------------------------------------- selftest

def _c(cond: bool, label: str, fails: list) -> None:
    if not cond:
        fails.append(label)


def selftest() -> int:
    f: list[str] = []

    # -- price twin
    _c(price_twin(110, 90, 101) is True, "twin: close above midpoint is True", f)
    _c(price_twin(110, 90, 99) is False, "twin: close below midpoint is False", f)
    _c(price_twin(110, 90, 100) is False, "twin: exactly at midpoint is False", f)
    _c(price_twin(100, 100, 100) is None, "twin: degenerate bar is None not False", f)
    _c(price_twin(None, 90, 100) is None, "twin: missing high is None", f)

    # -- runs: contiguity, gaps, boundaries
    _c(runs_strict({}) == [], "runs: empty", f)
    _c(runs_strict({1: True, 2: True, 3: True}) == [(1, 3)], "runs: single run of 3", f)
    _c(runs_strict({1: True, 2: False, 3: True}) == [(1, 1), (3, 1)],
       "runs: break on False", f)
    # a missing session is a BREAK, not a bridge
    _c(runs_strict({1: True, 3: True}) == [(1, 1), (3, 1)],
       "runs: gap in sessions breaks the run", f)
    _c(runs_strict({5: True, 6: True, 8: True}) == [(5, 2), (8, 1)],
       "runs: gap after a length-2 run", f)
    _c(runs_strict({1: False, 2: False}) == [], "runs: all False", f)
    _c(runs_strict({4: True}) == [(4, 1)], "runs: trailing run closes at end", f)

    # -- ladder is SURVIVAL counts: a run of 4 reaches ages 1,2,3,4
    lad = ladder([(0, 4), (10, 2), (20, 1)], max_k=5)
    _c(lad[1] == 3, "ladder: 3 runs reach age 1", f)
    _c(lad[2] == 2, "ladder: 2 runs reach age 2", f)
    _c(lad[3] == 1, "ladder: 1 run reaches age 3", f)
    _c(lad[4] == 1, "ladder: 1 run reaches age 4", f)
    _c(lad[5] == 0, "ladder: none reach age 5", f)

    _c(age_bin(1) == "1" and age_bin(2) == "2" and age_bin(7) == "3+",
       "age_bin: terminal bin at 3+", f)

    # -- blocks
    _c(block_id(0) == 0 and block_id(29) == 0 and block_id(30) == 1, "block_id edges", f)
    _c(blocks_with_treatment([0, 5, 29, 30, 61]) == 3, "blocks: 3 distinct", f)
    _c(is_inferential(15) and not is_inferential(14), "inferential threshold at 15", f)

    # -- true range / ATR, hand-computed
    _c(true_range(10, 8, None) == 2, "TR: no prev close is H-L", f)
    _c(true_range(10, 8, 12) == 4, "TR: gap down dominates (|L-Cp|=4)", f)
    _c(true_range(10, 8, 5) == 5, "TR: gap up dominates (|H-Cp|=5)", f)
    # 15 bars, each H-L=2, flat closes at 100 -> ATR14 = 2, atr_pct = 0.02
    hs = [101.0] * 15
    ls = [99.0] * 15
    cs = [100.0] * 15
    _c(abs(atr_pct(hs, ls, cs) - 0.02) < 1e-12, "atr_pct: flat 2-wide bars on 100 -> 2%", f)
    _c(atr_pct(hs[:5], ls[:5], cs[:5]) is None, "atr_pct: short history is None", f)

    # -- first passage. entry 100, atr 2% -> up barrier 104, down barrier 97.
    _c(first_passage(100, 0.02, [104.0], [99.0]) == "win", "fp: up barrier touched", f)
    _c(first_passage(100, 0.02, [103.9], [99.0]) == "unresolved",
       "fp: just short of the barrier, one bar", f)
    _c(first_passage(100, 0.02, [101.0], [97.0]) == "loss", "fp: down barrier touched", f)
    _c(first_passage(100, 0.02, [104.0], [97.0]) == "loss",
       "fp: same-bar both barriers resolves PESSIMISTICALLY", f)
    _c(first_passage(100, 0.02, [102.0, 104.0], [99.0, 99.5]) == "win",
       "fp: wins on the second bar", f)
    _c(first_passage(100, 0.02, [101.0] * 40, [99.0] * 40) == "unresolved",
       "fp: ran out of horizon is unresolved, NOT a loss", f)
    _c(first_passage(100, 0.02, [101.0, 104.0], [99.0, 99.0], max_hold=1) == "unresolved",
       "fp: max_hold truncates before the win", f)
    _c(first_passage(100, None, [104.0], [99.0]) == "unresolved", "fp: no ATR", f)

    # -- pi_hat and the driftless null
    ph = pi_hat(["win", "win", "loss", "unresolved"])
    _c(ph["pi"] == 2 / 3, "pi_hat: unresolved excluded from the ratio", f)
    _c(ph["n_resolved"] == 3 and ph["unresolved"] == 1 and ph["wins"] == 2,
       "pi_hat: counts (resolved = wins+losses)", f)
    _c(pi_hat([])["pi"] is None, "pi_hat: empty is None not 0", f)
    _c(abs(DRIFTLESS_NULL - 3 / 7) < 1e-12, "driftless null = 1.5/3.5 = 3/7", f)
    _c(abs(expected_r(DRIFTLESS_NULL)) < 1e-12, "E_R is exactly zero at the driftless null", f)
    _c(abs(expected_r(0.5) - 0.25) < 1e-12, "E_R at pi=0.5 is +0.25 ATR", f)
    # the pi that clears a 0.25 ATR hurdle
    _c(abs((0.25 + DOWN_ATR) / (UP_ATR + DOWN_ATR) - 0.5) < 1e-12,
       "pi >= 0.50 is what clears a +0.25 ATR cost hurdle", f)

    # -- bootstrap plumbing: a constant series must return the constant with a tight band
    per_date = {i: [1.0] for i in range(200)}
    bs = date_block_bootstrap(per_date, lambda xs: statistics.fmean(xs) if xs else None,
                              n_boot=200)
    _c(bs["point"] == 1.0 and bs["lo"] == 1.0 and bs["hi"] == 1.0,
       "bootstrap: constant series has a degenerate band", f)
    _c(bs["block"] == BLOCK_DAYS, "bootstrap: block is 30 days, not n^(1/3)", f)
    thin = date_block_bootstrap({i: [1.0] for i in range(10)},
                                lambda xs: statistics.fmean(xs) if xs else None)
    _c(thin["lo"] is None, "bootstrap: refuses a band on too few dates", f)

    n = 44
    print(f"lift_lib selftest: {n - len(f)}/{n} checks passed")
    for x in f:
        print(f"  FAIL {x}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else
             (print(__doc__) or 0))
