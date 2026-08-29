# Volume dispersion — recovering clip size without trade counts

**Status: PRE-REGISTERED 2026-08-27, before the tf=60 backfill and before any forward return.**
Thesis #12. Written before `scripts/fetch_hourly.py` and `scripts/dispersion_test.py` exist.

This is the workaround for the blockage recorded in `blockdom.md` §9: the variable that separates
whale from retail (trade counts) exists only in a window where check 0 fails. Thesis #12 recovers
that variable from volume SHAPE, which is available over the full check-0-passing window.

---

## 1. The identity

Model bucket volume as compound Poisson: bucket `b` receives `N_b` trades of size `S`, with
`E[N_b] = lambda`. Then

    E[V_b]   = lambda * E[S]
    Var(V_b) = lambda * E[S^2]
    ------------------------------------------
    Var(V_b) / E(V_b) = E[S^2] / E[S]          <- size-biased mean trade size

**The arrival rate cancels.** The dispersion index of intraday bucket volumes estimates average clip
size without observing how many trades occurred. High dispersion = lumpy = blocks; low = smooth =
churn. This is the standard index-of-dispersion (Fano factor) argument from point-process theory.

## 2. It is already validated against ground truth — 0 requests, measured 2026-08-26

Against the gross panel's true `buy_value / buy_freq`, on the m5/gross overlap (48 symbols).
**Within-symbol is the relevant correlation**; cross-sectional is only +0.172 because that merely
ranks names by liquidity.

| trailing k | 5-min buckets (~68/day) | HOURLY buckets (~8/day) |
|---|---|---|
| k=1 | +0.462 | +0.403 |
| **k=10** | **+0.562** | **+0.512** |
| k=20 | +0.558 | — |

Smoothing plateaus at k=10. Hourly loses only ~0.05, which is what makes this affordable.

**Orthogonal to what the board already sees** (within-symbol, k=10): rho with `rvol5` **+0.311**,
with relative range **+0.201** — about 10% shared variance with RVOL. It is not RVOL in disguise.

**Disclosure:** that validation used FULL-SAMPLE per-bucket seasonal medians, a mild look-ahead. It
is a measurement correlation, not a return result, so it cannot inflate any P&L — but the study below
uses TRAILING seasonals only, and re-reports the correlation under them as a sanity check.

## 3. Why this defeats check 0 — the structural move

**Check 0 gates PREDICTION studies, not MEASUREMENT studies.** So the question splits across two
windows and the true variable is never needed in the good one:

| window | check 0 | role |
|---|---|---|
| overlap 2026-02..08 (gross + m5) | **FAILS** (-0.98pp) | gives the proxy its MEANING — §2, done |
| good window 2024-09..2026-08 | **PASSES** (+0.97 / +1.07pp) | tests whether it PAYS — §6 |

## 4. Data

`multi_time_chart(sym, from, to, timeframe=60)` — **one request per symbol for 469 sessions**
(2024-09-02 .. 2026-08-26, ~7,316 bars). Probed 2026-08-26 at zero cost.

Server-side retention, from the 422 messages themselves: **tf=D and tf=60 = 2 years; tf=15 = 1 year.**
A `from` outside the window is a 422 that costs nothing. Payload keys are
`['close','date','high','low','open','volume']` — **no `value`, no `freq`**, which is exactly why the
proxy is necessary.

Backfill cost: **~159 requests** for the 159-name panel, 0.5% of the ~33k remaining. Stored as
`data/intraday/h60-{SYM}.csv.gz` — a NEW prefix; the m5 store is not touched.

**Every bar is returned exactly twice, byte-identical.** `intraday_lib.parse_payload()` dedupes on
`(date, hhmm)` and that guard is load-bearing: doubled volume changes every dispersion estimate.

## 5. Definitions — fixed now

Continuous session only, `09:00 <= hhmm < 16:00`: the 08:55 auction is one uncontested print and the
16:00 bucket is the MOC.

- **Trailing seasonal.** `seas(sym, hhmm, i)` = median volume in that bucket over the symbol's
  previous **60** sessions, strictly before `i`. Mandatory: IDX intraday volume is U-shaped, so a raw
  dispersion index would mostly measure the time of day.
- **Daily dispersion.** `d(i) = Var(x_b) / E(x_b)` over that session's buckets, where
  `x_b = V_b / seas(sym, b, i)`. Requires >= 5 buckets.
- **`disp10(i)`** = mean of `d` over sessions `i-9..i`. k=10 is taken from §2's plateau, not fitted here.
- **`disp_rel(i) = disp10(i) / median(disp10 over the prior 60 sessions, strictly before i)`.**
  Within-symbol and look-ahead-free. **This is the sorter.** Pooled raw dispersion is explicitly NOT
  used: §2 showed the cross-sectional signal is weak (+0.172) and would rank names by liquidity.
- Quintiles pooled on `disp_rel`. **Q5 = most block-like.** Predicted `Q5 - Q1 > 0`.

**Population.** Symbol-days in the good window with a computable `disp_rel` and a 5d excess return.
No volume or momentum filter — this is a cross-sectional sorter.

**Outcome.** `Panel.excess_return(sym, i, k, entry_lag=1)`, excess over IHSG. Primary `k=5`;
3 and 10 reported.

**Secondary, free, pre-registered here so it is not post-hoc: Amihud illiquidity.**
`amihud(i) = mean(|r_t| / value_t)` over `i-9..i`, stock-normalised the same way. Blocks cross at one
price (low impact per rupiah), churn grinds the tape — so the prediction is that **LOW** Amihud goes
with block-like flow. Score = `-amihud_rel`, Q5 = lowest impact, predicted `Q5 - Q1 > 0`. Daily panel
only, 0 requests, 475 sessions.

## 6. Pass bar — declared in advance

**The bar is attenuated on purpose and here is the arithmetic.** `blockdom.md` §6 set +1.0pp on the
TRUE variable. A proxy correlated `r` with truth recovers roughly `r` times a linear effect, and
r = 0.512 hourly. So the same underlying effect should read **~+0.5pp** through this instrument. The
bar is set there — lowered for attenuation, NOT because +1.0pp was missed.

Because a lower bar admits more noise, the supporting checks are not relaxed:

1. `disp_rel` Q5-Q1 at k=5 **>= +0.5pp**, 10/90 date-block band **clear of zero**.
2. Gradient **monotone in >= 4 of 5** quintile steps.
3. **ORTHOGONALITY: Q5-Q1 positive in >= 2 of 3 RVOL terciles.** The load-bearing check. If the
   effect lives in one RVOL tercile it is a volume effect wearing a dispersion label.
4. Feature-shift null within **+/- 0.3pp**.
5. **>= 3 of 4** calendar folds positive.
6. Check 0 passes in-window (expected: the good window is where it does, +0.97pp full panel).
7. `blocks_with_treatment >= 15`.
8. Trailing-seasonal validation correlation against gross truth **>= +0.40** in the overlap, i.e.
   the instrument still measures clip size once look-ahead is removed.

## 7. Refutation

- Flat or inverted gradient refutes it, and is not a licence to use the loosest cut.
- Null within 0.3pp of the real result: indistinguishable from noise whatever the point estimate.
- **Failing check 3 while passing check 1 is a REFUTATION, not a partial pass.** A dispersion effect
  confined to one RVOL tercile is RVOL.
- If check 8 fails, the instrument does not survive removal of look-ahead and nothing else is read.
- If Amihud and dispersion disagree in sign, both are reported and neither is promoted: they are
  different proxies for the same latent variable and a disagreement means at least one is measuring
  something else.

**Honest prior: ten flow theses have failed or been blocked here.** The base rate says this fails
too. It is run because the instrument is now validated, the window is finally the right one, and the
cost is 159 requests.

## 8. What ships

Nothing into the momentum board. Board files stay byte-identical, verified by md5. A pass would
justify a separately-approved read-out column, nothing more.

## 9. Result — **VERDICT: FAIL.** (2026-08-27)

`scripts/fetch_hourly.py` + `scripts/dispersion_test.py`; payload `data/panel/dispersion_test.json`.
Backfill **158 requests** (estimate was 159), 156/156 symbols, **508,838 hourly bars**, dedupe exactly
50% on every symbol — confirming again that every bar is returned twice.

Population **53,056 symbol-days**, 151 symbols, 407 sessions, 2024-10-29..2026-08-04, **15 blocks**.
**Check 0 PASSED at +0.98pp**, so for the first time in this programme a forward-return study on
microstructure-derived data ran in a window that can actually carry a verdict.

| # | check | result | |
|---|---|---|---|
| 1 | Q5-Q1 >= +0.5pp | **+0.122pp** | FAIL |
| 1b | band clear of zero | **[-0.38, +0.66]** | FAIL |
| 2 | monotone >= 4/4 | **2/4** | FAIL |
| 3 | orthogonality >= 2/3 terciles | **1/3** | FAIL |
| 4 | null within +/-0.3pp | -0.181pp | PASS |
| 5 | folds >= 3/4 | 3/4 | PASS |
| 6 | check 0 | +0.98pp | PASS |
| 7 | blocks >= 15 | 15 | PASS |
| 8 | trailing-seasonal rho >= +0.40 | **+0.384** | FAIL |

### 9.1 Check 8 is the important failure, and it corrects §2

**Removing the look-ahead from the seasonal baseline drops the instrument from rho +0.512 to
+0.384.** §2 disclosed this risk in advance — the validation used full-sample per-bucket medians —
and the disclosure was warranted: the instrument is materially weaker than the headline number
suggested. **The honest figure for "how well does volume dispersion measure clip size" is +0.384,
not +0.512**, and any future use must quote the trailing-seasonal number.

Measured on a broader sample too (n=4,904, 66 symbols, from the h60 store) than the original
(n=3,650, 48 symbols, m5), so the drop is not a sampling artifact.

This also re-prices the attenuated bar retrospectively: at r=0.384 the same true effect would read
~+0.38pp, not +0.5pp. The bar is NOT lowered to match — that would be shopping, and the headline
+0.122pp misses even the re-derived figure.

### 9.2 The dispersion gradient is U-shaped — for the second time

```
Q1 widest-clip-normalised +0.575   Q2 +0.420   Q3 +0.356   Q4 +0.463   Q5 most-block-like +0.697
```

Both tails above the middle, monotone 2/4. This is **the same U-shape thesis #9 found for
narrowness** (`effort.md` §10.1: Q1 +2.73, middle ~+1.3, Q5 +2.87). Two independent volume-shape
coordinates, built from different data at different resolutions, produce the same shape.

The parsimonious reading is that neither measures what it was designed to measure, and both are
picking up **extremeness in volume shape** — an attention/event proxy — which is mildly associated
with higher returns in either tail. That is a description of two failed studies, not a new
hypothesis; testing it would need its own pre-registration and the §3e objection applies (a U-shape
in pooled data across heterogeneous units can be guaranteed before any data are seen).

Orthogonality is decisive independently: **1 of 3 RVOL terciles positive** (+0.14 / -0.46 / -0.31).
Whatever little is there does not survive controlling for volume.

### 9.3 Amihud came closer — and is NOT promoted

The pre-registered secondary did better than the primary:

```
Q1 +0.311  Q2 +0.478  Q3 +0.256  Q4 +0.573  Q5 +0.844
Q5-Q1 +0.533pp   band [+0.10, +0.92]   monotone 3/4   blocks 15
RVOL terciles  T1 +0.18   T2 +0.43   T3 +0.83     <- 3/3 positive, and RISING with volume
folds  +0.944 / +0.850 / +0.178 / -0.390          <- 3/4
null   +0.230pp (sd 0.325)
```

It clears the headline bar, its band excludes zero, and it passes orthogonality 3/3. It fails
monotonicity (3/4) — and it is not promoted, for three reasons, in order of weight:

1. **The null is +0.230pp against a +0.533pp effect.** The feature-shift null destroys TIMING but
   preserves which symbols occupy which quintile, so a null this large says **~43% of the effect is
   cross-sectional composition, not timing**. The incremental, timing-dependent part is ~+0.30pp —
   below the +0.5pp bar. A statistic whose null is nearly half its value is not a signal.
2. **Promoting a secondary after the primary fails is the forbidden move.** `exit_test.py` states it
   for the fourteen-config case: "the fallback is NOT 'ship the best of the fourteen'". Same logic.
3. **Amihud was never validated against ground truth.** Check 8 exists for the dispersion instrument;
   there is no equivalent evidence that Amihud measures clip size rather than plain illiquidity —
   which has its own well-known return premium and would explain the cross-sectional null exactly.

§7's disagreement clause does not fire: both proxies are positive, so they agree in sign. They simply
disagree in magnitude, and the one with the larger effect is the one with no instrument validation
and a null half its size.

### 9.4 What was bought for 158 requests

A real answer instead of a blocked one. Before this, H2/H3 were inconclusive because every window
carrying the data failed check 0. The hourly backfill moved the question into a window that passes,
and the answer there is negative — which is worth strictly more than an inconclusive in a bad window.

The h60 store (159 symbols x 469 sessions) is now on disk and reusable at zero further cost for any
intraday-shape question over the full two years.

**Eleven theses have now failed or been blocked. Nothing ships. The momentum board is unchanged.**
