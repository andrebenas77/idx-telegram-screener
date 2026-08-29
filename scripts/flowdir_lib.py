#!/usr/bin/env python3
"""Pure formulas for signed aggressor flow (thesis #13). Framework: reference/flow-direction.md.

No I/O, no network, no mutable module state, so `--selftest` can pin every number by hand.

THE MEASURE. For each intraday bar take `2*clv - 1`, where `clv = (C-L)/(H-L)` is where the bar
closed in its own range, and average it weighted by bar volume:

    sflow = sum((2*clv_b - 1) * V_b) / sum(V_b)          in [-1, +1]

Positive reads as buy-initiated pressure, negative as sell-initiated. This is a SIGNED object and
it is not VPIN: VPIN is the unsigned |V_buy - V_sell| / V, a second-moment quantity that thesis #8
killed at rho = -0.028 against ground truth. The two are not interchangeable and the surviving
conclusions of thesis #8 attach to the unsigned one. Nothing here may be called VPIN or toxicity.

WHY THE SIGN IS THE WHOLE POINT. An unsigned imbalance flags a stock being quietly DUMPED
identically to one being accumulated. That was a real shipped bug in the bot before the VPIN
rewrite, and the hypothesis this library serves is about accumulation specifically.

RESOLUTION IS LOAD-BEARING. `effective_bars` reports 1/sum(w^2) on the volume weights: an hourly
session carries about 3.8 effective bars and a 5-minute session about 14.8. Below roughly 4 the
per-bar sign statistic saturates and stops measuring flow at all. Callers should report it beside
any correlation, because a correlation above `ceiling_vs_truth` is evidence of a shared channel
with the price path, not evidence of a good instrument.

Stdlib only (the repo has no numpy/scipy and no precedent for adding one). `spearman` is imported
from `vpin_lib` rather than redefined -- a second copy would drift from the validated one.
"""
from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vpin_lib import spearman  # noqa: E402,F401  (re-exported on purpose)

OPEN_HHMM = "09:00"
CLOSE_HHMM = "16:00"        # the 16:00 bucket is the MOC; 08:55 is the auction
SMOOTH_K = 5                # sessions in sflow5
ROC_K = 5                   # sessions differenced by d_sflow
REL_WIN = 60                # trailing window for the within-symbol median
SEAS_WIN = 60               # trailing window for the per-bucket volume seasonal


# ---------------------------------------------------------------- session selection

def continuous(bars):
    """Bars of the continuous session only.

    `intraday_lib.read_bars` already drops the 08:55 auction bar, but the m5 feed runs to 16:10 and
    those trailing buckets are the closing auction. A single uncontested MOC print can carry
    several percent of the day's volume, and it closes at one price by construction, so it would
    dominate a volume-weighted average with a CLV that means nothing.
    """
    return [b for b in bars if OPEN_HHMM <= b.hhmm < CLOSE_HHMM]


def clv(high: float, low: float, close: float):
    """(C-L)/(H-L), or None when the bar has no range.

    None, never 0.5. A limit-locked ARA/ARB bar has a genuinely undefined CLV, and inventing a
    neutral reading for the most decisive bars on IDX is how a flow measure gets its sign wrong
    on exactly the sessions that matter. Same convention as `overlay_test.structure`.
    """
    if high is None or low is None or close is None or high <= low:
        return None
    return (close - low) / (high - low)


# ---------------------------------------------------------------- the measure

def sflow(bars, weights=None):
    """Volume-weighted mean of (2*clv - 1). None when no bar carries a usable range and volume.

    `weights` overrides bar volume, so the seasonal and equal-weighted arms share this one
    definition instead of each growing their own copy.
    """
    num = den = 0.0
    for k, b in enumerate(bars):
        c = clv(b.h, b.l, b.c)
        if c is None:
            continue
        w = b.v if weights is None else weights[k]
        if w is None or w <= 0:
            continue
        num += (2.0 * c - 1.0) * w
        den += w
    return num / den if den > 0 else None


def sflow_equal(bars):
    """Unweighted mean of (2*clv - 1) -- a pre-registered Gate 2 rival.

    If volume weighting adds nothing over counting bars, the instrument is a bar-shape statistic
    and the volume clock is decoration.
    """
    xs = [2.0 * clv(b.h, b.l, b.c) - 1.0 for b in bars
          if clv(b.h, b.l, b.c) is not None]
    return statistics.fmean(xs) if xs else None


def sflow_seasonal(bars, seas):
    """sflow with each bar's volume divided by its own trailing time-of-day median.

    IDX intraday volume is U-shaped, so a raw volume weighting hands the open and the close most
    of the weight and the statistic partly measures the time of day. `seas` maps hhmm -> median
    volume from sessions strictly before this one; a bucket with no history is DROPPED rather
    than passed through unnormalised, because mixing normalised and raw weights inside one
    average is neither statistic.
    """
    w = []
    for b in bars:
        s = seas.get(b.hhmm)
        w.append(b.v / s if s and s > 0 else None)
    return sflow(bars, weights=w)


def trailing_seasonal(days, dates_before, hhmms=None):
    """{hhmm: median volume} over `dates_before`, which the caller has already restricted to
    sessions strictly earlier than the one being scored.

    Returned as a dict rather than a function so a caller scoring one session pays for the
    medians once. An hhmm seen fewer than 5 times is omitted: a median of two observations is
    not a seasonal, and letting it through makes early sessions look extreme.
    """
    acc: dict[str, list] = {}
    for d in dates_before:
        for b in days.get(d, ()):
            if hhmms is not None and b.hhmm not in hhmms:
                continue
            acc.setdefault(b.hhmm, []).append(b.v)
    return {k: statistics.median(v) for k, v in acc.items() if len(v) >= 5}


# ---------------------------------------------------------------- resolution diagnostics

def effective_bars(bars):
    """1/sum(w^2) on volume shares -- how many independent bars the session really carries.

    A session whose volume is concentrated in two bars carries two observations however many
    bars were printed. Reported beside every correlation because it bounds what any per-bar
    statistic can measure.
    """
    vols = [b.v for b in bars if b.v and b.v > 0]
    tot = sum(vols)
    if tot <= 0:
        return None
    ssq = sum((v / tot) ** 2 for v in vols)
    return (1.0 / ssq) if ssq > 0 else None


def ceiling_vs_truth(sum_w2: float, sd_truth: float, var_bar: float) -> float:
    """Largest correlation a per-bar statistic can reach against a daily truth, under a model
    where each bar carries the session value plus independent noise:

        rho_max = sd_truth / sqrt(sd_truth^2 + var_bar * sum(w^2))

    A measured rho ABOVE this is not a better instrument. It means the estimator and the truth
    share a channel the model does not contain -- on a price-derived statistic, the price path.
    That is a reason to run the increment test, never a reason to celebrate.
    """
    den = sd_truth * sd_truth + var_bar * sum_w2
    return sd_truth / math.sqrt(den) if den > 0 else 0.0


def split_half(bars):
    """(odd-bar sflow, even-bar sflow) for a reliability estimate.

    Interleaved rather than first-half/second-half: the two halves must differ only by sampling
    noise, and morning flow genuinely differs from afternoon flow, so a chronological split
    would measure the intraday shape and report it as unreliability.
    """
    return sflow(bars[0::2]), sflow(bars[1::2])


def spearman_brown(r_half):
    """Correct a half-length reliability to full length: 2r/(1+r). Caps the correlation any
    instrument can have with anything at sqrt(rho_xx)."""
    if r_half is None or r_half <= -1:
        return None
    return 2.0 * r_half / (1.0 + r_half)


# ---------------------------------------------------------------- series operations

def mean_k(series, i: int, k: int = SMOOTH_K):
    """Mean of `series` over i-k+1..i. None if any element in the window is missing -- a partial
    window is a different and noisier statistic, not a small-sample version of the same one."""
    if i - k + 1 < 0:
        return None
    w = series[i - k + 1:i + 1]
    return statistics.fmean(w) if all(x is not None for x in w) else None


def rel_to_median(series, i: int, win: int = REL_WIN):
    """series[i] minus the median of the prior `win` values, STRICTLY before i.

    Within-symbol and look-ahead free. Subtraction, not a ratio: the series is signed and
    straddles zero, so a ratio explodes near the denominator's zero crossing.
    """
    if series[i] is None or i - win < 0:
        return None
    prior = [x for x in series[i - win:i] if x is not None]
    if len(prior) < win // 2:
        return None
    return series[i] - statistics.median(prior)


def roc(series, i: int, k: int = ROC_K):
    """series[i] - series[i-k]. The desk rate-of-change. See `white_noise_roc_acf` before
    reading anything into it."""
    if i - k < 0 or series[i] is None or series[i - k] is None:
        return None
    return series[i] - series[i - k]


def acf(series, lag: int):
    """Autocorrelation at `lag`, ignoring missing values pairwise. None on a flat series."""
    xs = [x for x in series if x is not None]
    if len(xs) <= lag + 2:
        return None
    mu = statistics.fmean(xs)
    var = statistics.pvariance(xs)
    if var <= 0:
        return None
    cov = sum((xs[t] - mu) * (xs[t - lag] - mu) for t in range(lag, len(xs))) / (len(xs) - lag)
    return cov / var


def white_noise_roc_acf(max_lag: int = 5, smooth: int = SMOOTH_K, k: int = ROC_K):
    """Autocorrelation the ROC would have if the underlying level were WHITE NOISE.

    Differencing a smoothed white series produces strong, entirely mechanical autocorrelation.
    Reading that as "flow builds and then fades" is the trap this function exists to close: the
    ROC of a `smooth`-mean differenced at lag `k` has weights +1 on the last `smooth` values and
    -1 on the `smooth` values ending `k` back, and its ACF follows from those weights alone.

    With smooth=5, k=5 this returns +0.700 +0.400 +0.100 -0.200 -0.500. A measured ACF that
    matches these is evidence of NO dynamics, not evidence of dynamics.
    """
    n = k + smooth
    c = [0.0] * n
    for j in range(smooth):
        c[j] += 1.0
    for j in range(k, k + smooth):
        c[j] -= 1.0
    var = sum(x * x for x in c)
    out = []
    for L in range(1, max_lag + 1):
        cov = sum(c[j] * c[j + L] for j in range(n - L))
        out.append(cov / var if var > 0 else None)
    return out


# ---------------------------------------------------------------- clustered inference

def cluster_bootstrap(pairs, clusters, stat, n_boot: int = 2000, seed: int = 7,
                      lo_q: float = 0.10, hi_q: float = 0.90):
    """Resample whole CLUSTERS with replacement and report point, 10th and 90th percentiles.

    `pairs` is the observation list, `clusters` the same-length list of cluster labels. For the
    tape validation the cluster is the calendar DATE: 2026-08-05 alone contributes nine tapes,
    so treating 41 rows as 41 independent observations overstates precision by roughly a factor
    of two. Bands are 10/90 rather than 5/95 -- with about 20 effective units a 5% tail is not
    resolvable, and quoting one is the same error class as a z-test on clustered events.
    """
    import random
    by: dict = {}
    for p, c in zip(pairs, clusters):
        by.setdefault(c, []).append(p)
    keys = sorted(by)
    if len(keys) < 3:
        return {"point": stat(pairs), "lo": None, "hi": None, "n_clusters": len(keys),
                "note": "too few clusters to resample"}
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        obs = []
        for _ in range(len(keys)):
            obs.extend(by[keys[rng.randrange(len(keys))]])
        v = stat(obs)
        if v is not None:
            draws.append(v)
    draws.sort()
    if not draws:
        return {"point": stat(pairs), "lo": None, "hi": None, "n_clusters": len(keys)}
    return {"point": stat(pairs),
            "lo": draws[int(lo_q * len(draws))],
            "hi": draws[min(len(draws) - 1, int(hi_q * len(draws)))],
            "n_clusters": len(keys), "n_draws": len(draws)}


def effective_n(clusters) -> float:
    """sum(m^2)/sum(m) over cluster sizes -- the design-effect view of how many independent
    observations a clustered sample really carries. Quoted next to every tape correlation."""
    sizes: dict = {}
    for c in clusters:
        sizes[c] = sizes.get(c, 0) + 1
    m = list(sizes.values())
    tot = sum(m)
    return tot / (sum(x * x for x in m) / tot) if tot else 0.0


def fisher_ci(r, n: int):
    """Fisher-z 95% interval. Reported only alongside the clustered band, never instead of it."""
    if r is None or n < 4 or abs(r) >= 1.0:
        return (None, None)
    z = 0.5 * math.log((1 + r) / (1 - r))
    se = 1.0 / math.sqrt(n - 3)
    return (math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se))


# ---------------------------------------------------------------- selftest

def _c(cond, label, fails):
    if not cond:
        fails.append(label)


class _B:
    """Minimal Bar stand-in so the selftest does not need intraday_lib or a file on disk."""
    __slots__ = ("date", "hhmm", "o", "h", "l", "c", "v")

    def __init__(self, hhmm, high, low, close, volume, date="2026-01-01", open_=None):
        self.date, self.hhmm = date, hhmm
        self.o = high if open_ is None else open_
        self.h, self.l, self.c, self.v = high, low, close, volume


def selftest() -> int:
    f: list = []

    # ---- clv
    _c(clv(10, 8, 10) == 1.0, "clv at the high is 1", f)
    _c(clv(10, 8, 8) == 0.0, "clv at the low is 0", f)
    _c(clv(10, 8, 9) == 0.5, "clv mid-range is 0.5", f)
    _c(clv(10, 10, 10) is None, "clv of a locked bar is None, not 0.5", f)
    _c(clv(None, 8, 9) is None, "clv of a missing high is None", f)

    # ---- sflow: hand-computed
    bars = [_B("09:00", 10, 8, 10, 100), _B("09:05", 10, 8, 8, 100)]
    _c(sflow(bars) == 0.0, "one bar on the high and one on the low, equal volume, cancels", f)
    bars = [_B("09:00", 10, 8, 10, 300), _B("09:05", 10, 8, 8, 100)]
    # (1*300 + -1*100)/400 = 0.5
    _c(abs(sflow(bars) - 0.5) < 1e-12, "volume weighting: 300 up vs 100 down is +0.5", f)
    _c(abs(sflow_equal(bars) - 0.0) < 1e-12, "equal weighting of the same two bars is 0", f)
    locked = [_B("09:00", 10, 10, 10, 999), _B("09:05", 10, 8, 10, 100)]
    _c(sflow(locked) == 1.0, "a locked bar is dropped, not scored 0.5", f)
    _c(sflow([_B("09:00", 10, 10, 10, 5)]) is None, "no usable bar gives None, not 0", f)
    _c(sflow([_B("09:00", 10, 8, 10, 0)]) is None, "a zero-volume bar contributes nothing", f)

    # ---- continuous session
    day = [_B("08:55", 10, 8, 9, 50), _B("09:00", 10, 8, 9, 50),
           _B("15:55", 10, 8, 9, 50), _B("16:00", 10, 8, 9, 50), _B("16:10", 10, 8, 9, 50)]
    keep = [b.hhmm for b in continuous(day)]
    _c(keep == ["09:00", "15:55"], "continuous() drops the auction and the MOC buckets", f)

    # ---- effective bars
    _c(abs(effective_bars([_B("09:00", 10, 8, 9, 25) for _ in range(4)]) - 4.0) < 1e-9,
       "four equal-volume bars are four effective bars", f)
    lop = [_B("09:00", 10, 8, 9, 100), _B("09:05", 10, 8, 9, 0.0001)]
    _c(effective_bars(lop) < 1.01, "volume in one bar is one effective bar", f)
    _c(effective_bars([]) is None, "no bars gives None", f)

    # ---- ceiling: more noise or fewer effective bars must lower it
    # each clock is scored with its OWN measured per-bar variance: h60 bars span an hour and
    # close nearer the middle of their range, so var(2clv-1) is 0.505 against m5 at 0.812.
    a = ceiling_vs_truth(0.068, 0.169, 0.812)
    b = ceiling_vs_truth(0.265, 0.169, 0.505)
    _c(a > b, "a finer clock raises the ceiling", f)
    _c(0.55 < a < 0.62, "m5 ceiling lands near 0.585", f)
    _c(0.40 < b < 0.44, "h60 ceiling lands near 0.419", f)
    _c(ceiling_vs_truth(0.265, 0.169, 0.812) < b,
       "holding the clock fixed, noisier bars lower the ceiling", f)

    # ---- split half and Spearman-Brown
    odd, even = split_half([_B("09:00", 10, 8, 10, 100), _B("09:05", 10, 8, 8, 100),
                            _B("09:10", 10, 8, 10, 100), _B("09:15", 10, 8, 8, 100)])
    _c(odd == 1.0 and even == -1.0, "split_half interleaves rather than cutting in two", f)
    _c(abs(spearman_brown(0.5) - 2.0 / 3.0) < 1e-12, "Spearman-Brown of 0.5 is 2/3", f)
    _c(spearman_brown(None) is None, "Spearman-Brown of None is None", f)

    # ---- series ops
    s = [1.0, 2.0, 3.0, 4.0, 5.0]
    _c(mean_k(s, 4, 5) == 3.0, "mean_k over the full window", f)
    _c(mean_k(s, 2, 5) is None, "mean_k refuses a partial window", f)
    _c(mean_k([1.0, None, 3.0], 2, 3) is None, "mean_k refuses a window with a hole", f)
    _c(roc(s, 4, 2) == 2.0, "roc differences at the stated lag", f)
    _c(roc(s, 1, 2) is None, "roc refuses before the window fills", f)
    ser = [0.0] * 60 + [5.0]
    _c(rel_to_median(ser, 60, 60) == 5.0, "rel_to_median subtracts the trailing median", f)
    _c(rel_to_median(ser, 59, 60) is None, "rel_to_median refuses without a full window", f)

    # ---- the white-noise ROC ACF: the number the Gate 3 comparison rests on
    w = white_noise_roc_acf(5, 5, 5)
    want = [0.7, 0.4, 0.1, -0.2, -0.5]
    _c(all(abs(x - y) < 1e-12 for x, y in zip(w, want)),
       "white-noise ROC ACF is +0.7 +0.4 +0.1 -0.2 -0.5 for smooth=5 lag=5", f)
    _c(abs(white_noise_roc_acf(1, 1, 1)[0] + 0.5) < 1e-12,
       "a plain first difference of white noise has ACF -0.5", f)

    # ---- acf reproduces it on a constructed series
    import random as _r
    rng = _r.Random(11)
    x = [rng.gauss(0, 1) for _ in range(20000)]
    lv = [statistics.fmean(x[i - 4:i + 1]) if i >= 4 else None for i in range(len(x))]
    rc = [lv[i] - lv[i - 5] if i >= 9 else None for i in range(len(x))]
    got = acf(rc, 1)
    _c(abs(got - 0.7) < 0.05, "acf() on simulated white noise recovers +0.70 at lag 1", f)

    # ---- trailing seasonal
    days = {"d%d" % k: [_B("09:00", 10, 8, 9, 100 + k), _B("09:05", 10, 8, 9, 50)]
            for k in range(6)}
    se = trailing_seasonal(days, ["d%d" % k for k in range(6)])
    _c(se["09:00"] == 102.5 and se["09:05"] == 50, "trailing seasonal takes bucket medians", f)
    thin = trailing_seasonal({"d0": [_B("09:00", 10, 8, 9, 100)]}, ["d0"])
    _c(thin == {}, "a bucket seen fewer than 5 times is omitted", f)

    # ---- seasonal sflow drops unnormalisable buckets rather than mixing scales
    bb = [_B("09:00", 10, 8, 10, 200), _B("09:05", 10, 8, 8, 100)]
    _c(abs(sflow_seasonal(bb, {"09:00": 100.0, "09:05": 100.0}) - (1 / 3)) < 1e-12,
       "seasonal sflow reweights by the bucket median", f)
    _c(sflow_seasonal(bb, {"09:00": 100.0}) == 1.0,
       "a bucket with no seasonal is dropped, not passed through raw", f)

    # ---- clustered bootstrap and effective n
    cl = ["a"] * 9 + ["b", "c", "d", "e", "f"]
    _c(abs(effective_n(cl) - 14.0 / (86.0 / 14.0)) < 1e-9,
       "effective_n applies the cluster design effect", f)
    _c(effective_n(cl) < len(cl) / 2, "nine tapes on one date roughly halve the effective n", f)
    res = cluster_bootstrap([1.0] * 20, ["a"] * 10 + ["b"] * 10,
                            lambda xs: statistics.fmean(xs) if xs else None)
    _c(res["n_clusters"] == 2 and res["lo"] is None, "fewer than 3 clusters refuses a band", f)
    res = cluster_bootstrap(list(range(30)), [i // 3 for i in range(30)],
                            lambda xs: statistics.fmean(xs) if xs else None)
    _c(res["lo"] is not None and res["lo"] < res["point"] < res["hi"],
       "the clustered band brackets the point estimate", f)

    # ---- fisher
    lo, hi = fisher_ci(0.782, 29)
    _c(lo is not None and 0.57 < lo < 0.60 and 0.88 < hi < 0.90,
       "Fisher interval for rho 0.782 at n=29 is about [0.583, 0.893]", f)
    _c(fisher_ci(1.0, 29) == (None, None), "Fisher refuses a degenerate rho", f)

    # ---- spearman is the imported one, not a second copy
    _c(abs(spearman([1, 2, 3], [1, 2, 3]) - 1.0) < 1e-9,
       "imported spearman is monotone-exact", f)
    _c(abs(spearman([1, 2, 3], [3, 2, 1]) + 1.0) < 1e-9,
       "imported spearman inverts on a reversed series", f)

    print("flowdir_lib selftest: %d checks, %d failed" % (51, len(f)))
    for x in f:
        print("   FAIL " + x)
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
