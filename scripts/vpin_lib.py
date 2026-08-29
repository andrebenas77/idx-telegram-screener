"""Pure formulas for the VPIN (order-flow toxicity) study. Framework: reference/vpin.md.

No I/O, no network, no mutable module state, so `--selftest` can pin every number by hand.

THE MEASURE. Partition traded volume into equal-size buckets on a VOLUME clock, ignoring
calendar time, so a busy hour gets the same weight as a quiet one. Classify each bucket's
volume as buy- or sell-initiated, then

    imbalance_j = |V_buy,j - V_sell,j| / V
    VPIN        = mean(imbalance_j) over the trailing n buckets

THREE classifications are implemented on purpose, because the literature says the choice
decides the answer. Andersen & Bondarenko: BVC misclassifies more as volatility rises, which
mechanically inflates the imbalance -- and the imbalance IS VPIN -- so BV-VPIN tracks
volatility BY CONSTRUCTION; BVC is inferior to a plain tick rule against known aggressor
data; and BV-VPIN and TR-VPIN can move in OPPOSITE directions. Hence `bvc`, `tick_rule`, and
`true_aggressor` all exist here and V0 scores them against each other before anything is
concluded.

Stdlib only (the repo has no numpy/scipy and no precedent for adding one). The normal CDF
comes from math.erfc.
"""

from __future__ import annotations

import math
import statistics
import sys

DEFAULT_BUCKETS_PER_DAY = 24     # matches timeframe=15, the primary panel
DEFAULT_N = 50                   # buckets in the VPIN moving average
AUCTION_HHMM = "08:55"           # pre-opening auction: one uncontested print, excluded


# ---------------------------------------------------------------- normal CDF

def norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erfc. Exact to double precision, no scipy.

    Phi(x) = 0.5 * erfc(-x / sqrt(2)).  Using erfc rather than erf keeps precision in the
    far-left tail, where erf(-x) suffers cancellation against 1.
    """
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


# ---------------------------------------------------------------- volume clock

def volume_buckets(bars: list[tuple], bucket_size: float) -> list[list[tuple]]:
    """Split bars onto a volume clock of equal-volume buckets.

    `bars` is [(price_change, volume), ...] in execution order. A bar that straddles a
    bucket boundary is SPLIT PROPORTIONALLY across the buckets it spans, carrying its own
    price change into each piece -- the standard treatment, and the reason the clock is not
    just "group every k bars".

    Returns [[(price_change, volume_piece), ...], ...]; the final bucket may be partial and
    callers decide whether to keep it (V0 drops it, since an incomplete bucket has a
    systematically different imbalance).
    """
    if bucket_size <= 0:
        raise ValueError("bucket_size must be positive")
    out: list[list[tuple]] = []
    cur: list[tuple] = []
    filled = 0.0
    for dp, vol in bars:
        remaining = float(vol)
        while remaining > 0:
            room = bucket_size - filled
            take = min(room, remaining)
            cur.append((dp, take))
            filled += take
            remaining -= take
            if filled >= bucket_size - 1e-9:
                out.append(cur)
                cur = []
                filled = 0.0
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- classification

def bvc_split(bucket: list[tuple], sigma: float) -> tuple[float, float]:
    """Bulk Volume Classification. V_buy = V * Phi(dP / sigma), V_sell = V - V_buy.

    `sigma` is the standard deviation of the bar-level price CHANGE over the estimation
    window, not of returns. A zero or missing sigma makes the argument undefined; the
    neutral 50/50 split is returned rather than raising, because a flat window is a real
    market state (a limit-locked or untraded name) and not a data error.

    This is the arm the literature attacks: classification error rises with volatility, and
    that error IS the signal being measured. Never report it without the TR arm beside it.
    """
    if not sigma or sigma <= 0:
        tot = sum(v for _, v in bucket)
        return tot / 2.0, tot / 2.0
    buy = sum(v * norm_cdf(dp / sigma) for dp, v in bucket)
    tot = sum(v for _, v in bucket)
    return buy, tot - buy


def tick_rule_split(bucket: list[tuple], carry: int = 1) -> tuple[float, float, int]:
    """Plain tick rule: uptick = buy, downtick = sell, zero tick inherits the last sign.

    `carry` is the sign entering the bucket (+1/-1); the updated sign is returned so a
    caller can thread it across bucket boundaries -- the state must persist, or every
    bucket silently restarts from an arbitrary assumption.
    """
    buy = sell = 0.0
    s = carry
    for dp, v in bucket:
        if dp > 0:
            s = 1
        elif dp < 0:
            s = -1
        (buy, sell) = (buy + v, sell) if s > 0 else (buy, sell + v)
    return buy, sell, s


def true_split(bucket_prints: list[tuple]) -> tuple[float, float]:
    """Ground truth from the vendor's aggressor flag.

    `bucket_prints` is [(aggressor, volume), ...] where aggressor is "BUY"/"SELL" -- the
    side that CROSSED THE SPREAD. This is what running-trade's `type` field carries on a
    closed session, already surfaced as `aggressor` by tape_lib.parse_prints().
    """
    buy = sum(v for a, v in bucket_prints if a == "BUY")
    sell = sum(v for a, v in bucket_prints if a == "SELL")
    return buy, sell


# ---------------------------------------------------------------- VPIN

def imbalance(buy: float, sell: float) -> float | None:
    """|V_buy - V_sell| / (V_buy + V_sell). None on an empty bucket rather than 0.0 --
    an empty bucket is an absence of data, not a balanced one."""
    tot = buy + sell
    if tot <= 0:
        return None
    return abs(buy - sell) / tot


def vpin(imbalances: list[float | None], n: int = DEFAULT_N) -> list[float | None]:
    """Trailing mean of bucket imbalances over n buckets.

    Emits None until n buckets exist, never a short-window average: a VPIN computed on 3
    buckets is not a small-sample version of the same statistic, it is a different and much
    noisier one, and letting it through makes the early panel look spuriously extreme.
    """
    out: list[float | None] = []
    win: list[float] = []
    for x in imbalances:
        if x is not None:
            win.append(x)
        if len(win) < n:
            out.append(None)
        else:
            out.append(statistics.fmean(win[-n:]))
    return out


def sigma_dp(bars: list[tuple]) -> float:
    """SD of bar-level price changes, for BVC. Population SD over the whole window."""
    dps = [dp for dp, _ in bars]
    if len(dps) < 2:
        return 0.0
    return statistics.pstdev(dps)


# ---------------------------------------------------------------- helpers

def rel_deltas(closes: list[float]) -> list[float]:
    """Bar-to-bar RELATIVE price changes, for the rolling-sigma arm.

    Relative, not absolute, because a trailing window spans price levels: a name that ran
    from 500 to 900 has mechanically larger absolute dP late in the window, and an absolute
    rolling sigma would read that as a volatility rise when it is a price-level rise. The
    relative SD is then rescaled to today's price level by sigma_rolling().
    """
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1] > 0]


def sigma_rolling(prior_rel: list[float], price_level: float) -> float:
    """BVC sigma from a TRAILING window, rescaled to today's price level.

    This is the FAITHFUL reproduction of the estimator Andersen & Bondarenko attack. Their
    mechanism is that BVC misclassifies more as volatility rises; a trailing sigma cannot
    adapt to today, so a violent session gets divided by a calm window's sigma, |dP/sigma|
    blows up, Phi saturates toward 0 or 1, and the bucket reads as maximally one-sided. That
    inflation IS the accusation, and it is only visible if sigma is estimated out-of-sample.

    The session-sigma arm (sigma_dp) partly neutralises the same mechanism by construction,
    which is why both are computed and reported side by side rather than one being chosen.
    """
    if len(prior_rel) < 2 or price_level <= 0:
        return 0.0
    return statistics.pstdev(prior_rel) * price_level


def bar_deltas(closes: list[float]) -> list[float]:
    """Bar-to-bar price changes. First bar has no predecessor and gets 0.0 -- which BVC maps
    to a neutral 50/50 split, the honest treatment of "no information"."""
    return [0.0] + [closes[i] - closes[i - 1] for i in range(1, len(closes))]


def spearman(a: list[float], b: list[float]) -> float | None:
    """Rank correlation with average ranks for ties. The V0 statistic: VPIN arms are
    monotonically related at best, and one outlier session should not decide the verdict."""
    n = len(a)
    if n < 3 or len(b) != n:
        return None

    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((y - mb) ** 2 for y in rb))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def realized_vol(closes: list[float]) -> float | None:
    """Realized volatility of log returns over the bar series. The thing VPIN is accused of
    secretly being, so it is computed here and carried alongside every VPIN number."""
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i] > 0 and closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    return statistics.pstdev(rets)


# ---------------------------------------------------------------- selftest

def _c(cond: bool, label: str, fails: list) -> None:
    if not cond:
        fails.append(label)


def selftest() -> int:
    f: list[str] = []

    # -- normal CDF, pinned against known values
    _c(abs(norm_cdf(0.0) - 0.5) < 1e-12, "Phi(0) = 0.5", f)
    _c(abs(norm_cdf(1.959963985) - 0.975) < 1e-9, "Phi(1.96) = 0.975", f)
    _c(abs(norm_cdf(-1.959963985) - 0.025) < 1e-9, "Phi(-1.96) = 0.025", f)
    _c(norm_cdf(-40) >= 0.0 and norm_cdf(-40) < 1e-300, "Phi far-left tail stays finite", f)

    # -- volume clock
    b = volume_buckets([(1.0, 100)], 100)
    _c(len(b) == 1 and b[0] == [(1.0, 100)], "clock: one exact bucket", f)
    b = volume_buckets([(1.0, 250)], 100)
    _c(len(b) == 3 and [sum(v for _, v in x) for x in b] == [100, 100, 50],
       "clock: one big bar SPLITS across 3 buckets", f)
    b = volume_buckets([(1.0, 60), (-2.0, 60)], 100)
    _c(len(b) == 2 and b[0] == [(1.0, 60), (-2.0, 40)] and b[1] == [(-2.0, 20)],
       "clock: bar straddling a boundary splits and carries its own dP into both", f)
    _c(sum(v for x in volume_buckets([(1, 37), (2, 55), (3, 11)], 20) for _, v in x) == 103,
       "clock: volume is conserved exactly", f)
    try:
        volume_buckets([(1.0, 10)], 0)
        _c(False, "clock: zero bucket_size must raise", f)
    except ValueError:
        pass

    # -- BVC. dP=0 -> Phi(0)=0.5 -> exact half.
    buy, sell = bvc_split([(0.0, 100)], sigma=2.0)
    _c(abs(buy - 50) < 1e-9 and abs(sell - 50) < 1e-9, "BVC: zero dP splits 50/50", f)
    buy, sell = bvc_split([(2.0, 100)], sigma=2.0)   # Phi(1) = 0.8413447
    _c(abs(buy - 84.13447461) < 1e-6, "BVC: dP = 1 sigma -> 84.13% buy", f)
    buy, sell = bvc_split([(-2.0, 100)], sigma=2.0)
    _c(abs(buy - 15.86552539) < 1e-6, "BVC: dP = -1 sigma -> 15.87% buy", f)
    buy, sell = bvc_split([(5.0, 100)], sigma=0)
    _c(buy == 50 and sell == 50, "BVC: sigma=0 falls back to neutral, does not raise", f)
    b1, s1 = bvc_split([(1.0, 50), (-1.0, 50)], sigma=1.0)
    _c(abs((b1 + s1) - 100) < 1e-9, "BVC: buy+sell = total volume", f)

    # -- tick rule
    buy, sell, s = tick_rule_split([(1.0, 10), (1.0, 10)], carry=1)
    _c(buy == 20 and sell == 0 and s == 1, "tick: upticks all buy", f)
    buy, sell, s = tick_rule_split([(-1.0, 10), (-1.0, 10)], carry=1)
    _c(buy == 0 and sell == 20 and s == -1, "tick: downticks all sell", f)
    buy, sell, s = tick_rule_split([(0.0, 10)], carry=-1)
    _c(sell == 10 and s == -1, "tick: zero tick INHERITS the carried sign", f)
    _, _, s = tick_rule_split([(1.0, 5)], carry=-1)
    _c(s == 1, "tick: sign state is returned for threading across buckets", f)

    # -- true aggressor
    buy, sell = true_split([("BUY", 100), ("SELL", 40), ("BUY", 10)])
    _c(buy == 110 and sell == 40, "true: sums by aggressor flag", f)
    _c(true_split([]) == (0, 0), "true: empty is (0,0)", f)

    # -- imbalance
    _c(imbalance(100, 0) == 1.0, "imbalance: one-sided is 1.0", f)
    _c(imbalance(50, 50) == 0.0, "imbalance: balanced is 0.0", f)
    _c(abs(imbalance(75, 25) - 0.5) < 1e-12, "imbalance: 75/25 is 0.5", f)
    _c(imbalance(0, 0) is None, "imbalance: empty bucket is None NOT 0.0", f)

    # -- VPIN moving average
    v = vpin([0.5] * 10, n=5)
    _c(v[:4] == [None] * 4, "vpin: emits None until n buckets exist", f)
    _c(abs(v[4] - 0.5) < 1e-12 and abs(v[9] - 0.5) < 1e-12, "vpin: constant series", f)
    v = vpin([0.0, 1.0, 0.0, 1.0], n=2)
    _c(v[1] == 0.5 and v[2] == 0.5 and v[3] == 0.5, "vpin: trailing mean of 2", f)
    v = vpin([None, 0.4, 0.6], n=2)
    _c(v[2] == 0.5, "vpin: None buckets are skipped, not counted as zero", f)

    # -- sigma / deltas
    _c(bar_deltas([10, 12, 11]) == [0.0, 2.0, -1.0], "bar_deltas: first bar is 0.0", f)
    _c(abs(sigma_dp([(2.0, 1), (-2.0, 1)]) - 2.0) < 1e-12, "sigma_dp: population SD", f)
    _c(sigma_dp([(1.0, 1)]) == 0.0, "sigma_dp: single bar is 0.0", f)

    # -- rolling sigma (the faithful-reproduction arm)
    _c(rel_deltas([100, 110, 99]) == [0.1, -0.1], "rel_deltas: relative not absolute", f)
    _c(rel_deltas([0, 100]) == [], "rel_deltas: guards divide-by-zero", f)
    # 1% relative SD rescaled to a price of 500 -> absolute sigma 5.0
    _c(abs(sigma_rolling([0.01, -0.01], 500.0) - 5.0) < 1e-12,
       "sigma_rolling: rescales relative SD to today's price level", f)
    _c(sigma_rolling([0.01], 500.0) == 0.0, "sigma_rolling: too few priors is 0.0", f)
    _c(sigma_rolling([0.01, -0.01], 0) == 0.0, "sigma_rolling: bad price level is 0.0", f)
    # the mechanism itself: a violent day against a calm trailing window saturates Phi
    calm = sigma_rolling([0.001, -0.001], 500.0)          # absolute sigma 0.5
    violent_bucket = [(5.0, 100)]                          # dP = 10 sigma
    ib_roll = imbalance(*bvc_split(violent_bucket, calm))
    ib_sess = imbalance(*bvc_split(violent_bucket, sigma=5.0))   # session sigma adapts
    _c(ib_roll > 0.999 and ib_sess < 0.70,
       "MECHANISM: rolling sigma saturates on a violent day where session sigma does not", f)

    # -- spearman
    _c(abs(spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-12, "spearman: perfect +1", f)
    _c(abs(spearman([1, 2, 3, 4], [40, 30, 20, 10]) + 1.0) < 1e-12, "spearman: perfect -1", f)
    _c(spearman([1, 1, 1], [1, 2, 3]) is None, "spearman: no variance is None", f)
    _c(spearman([1, 2], [1, 2]) is None, "spearman: n<3 is None", f)
    _c(abs(spearman([1, 2, 2, 3], [1, 2, 2, 3]) - 1.0) < 1e-12, "spearman: ties average", f)

    # -- realized vol
    _c(realized_vol([100, 100, 100]) == 0.0, "realized_vol: flat is 0.0", f)
    _c(realized_vol([100]) is None, "realized_vol: too short is None", f)

    # -- THE DUPLICATE-BAR TEST. The endpoint returns every bar twice, byte-identical
    #    (BBCA 2026-05-25: 136 rows, 68 distinct timestamps, volume ratio exactly 2.00).
    #    Undeduped input does not merely double a scalar -- it RELOCATES every bucket
    #    boundary, so the guard is load-bearing for the volume clock, not cosmetic.
    raw = [(1.0, 100), (1.0, 100), (-1.0, 50), (-1.0, 50)]      # each bar twice
    ded = [(1.0, 100), (-1.0, 50)]
    _c(sum(v for _, v in raw) == 2 * sum(v for _, v in ded),
       "dedupe: raw volume is exactly 2x deduped", f)
    _c(len(volume_buckets(raw, 150)) == 2 and len(volume_buckets(ded, 150)) == 1,
       "dedupe: undeduped input DOUBLES the bucket count", f)
    ib_raw = imbalance(*bvc_split(volume_buckets(raw, 300)[0], sigma=1.0))
    ib_ded = imbalance(*bvc_split(volume_buckets(ded, 150)[0], sigma=1.0))
    _c(abs(ib_raw - ib_ded) < 1e-9,
       "dedupe: with bucket_size also doubled the imbalance is invariant (sanity)", f)

    # -- end-to-end pinned VPIN on a synthetic one-sided series
    bars = [(1.0, 100)] * 10          # every bar an uptick
    bk = volume_buckets(bars, 200)    # 5 full buckets
    ibs = [imbalance(*bvc_split(x, sigma=0.5)) for x in bk]
    v = vpin(ibs, n=5)
    _c(len(bk) == 5, "e2e: 1000 volume / 200 = 5 buckets", f)
    # dP/sigma = 1.0/0.5 = 2, Phi(2) = 0.9772499, so imbalance = 2*Phi - 1 = 0.9544997.
    # Pinned exactly rather than bounded: a bound would not have caught the arithmetic
    # slip that first wrote this assertion as "> 0.99".
    _c(v[4] is not None and abs(v[4] - (2 * norm_cdf(2.0) - 1)) < 1e-9,
       "e2e: relentless upticks give VPIN = 2*Phi(2)-1 = 0.95450", f)
    bars = [(1.0, 100), (-1.0, 100)] * 5
    ibs = [imbalance(*bvc_split(x, sigma=0.5)) for x in volume_buckets(bars, 200)]
    v = vpin(ibs, n=5)
    _c(v[4] is not None and v[4] < 0.05, "e2e: alternating ticks give VPIN ~0", f)

    n = 50
    print(f"vpin_lib selftest: {n - len(f)}/{n} checks passed")
    for x in f:
        print(f"  FAIL {x}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else (print(__doc__) or 0))
