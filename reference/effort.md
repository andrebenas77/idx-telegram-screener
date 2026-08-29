# Effort vs Result — narrowness as a daily coordinate

**Status: PRE-REGISTERED 2026-08-26. No forward return has been computed at the time of writing.**
Thesis #9. Framework written before `scripts/effort_test.py` exists, per the convention in
`vpin.md` §5 and `accumulation.md` §6.6.

Origin: `idx_quant_skill.md` ("IDX Quant Game Theory & VSA"), which proposes a screen for 1m/5m
candles with `RVOL > 3.0` and `(H-L)/C < 0.005` and low `freq` per unit volume, read as a whale
anchoring price with iceberg bids underneath a retail flush.

---

## 1. The hypothesis

> **H1. Among days of elevated relative volume, NARROW-range days behave differently from
> WIDE-range ones, and the effect is monotone in narrowness.**

Wyckoff's effort-vs-result: heavy volume that fails to move price means the move is being absorbed.
Heavy volume that moves price a long way is a climax.

This is the only leg of the source thesis that is both new to this repo and testable on data already
on disk. The other legs are dead or mis-specified; §9 records why, so nobody re-tests them.

## 2. Priors, both directions — declared before running

**For.** The repo's own best measurement of *who is informed* points here. True order-flow toxicity
(measured on true aggressor flags, not inferred) is NEGATIVELY correlated with realized volatility,
rho = -0.483, 95% CI [-0.733, -0.118] (`vpin-results.md`). The sessions where informed flow is most
one-sided are the CALM ones. "Big volume, small range" is literally "high volume, low volatility" —
an independent route to the same region of the state space.

**Against, and it is heavy.** Eight flow theses have been refuted here, all of the form "buy what the
whale is quietly buying": quiet accumulation (-0.04/+0.01pp), net-persistence (-0.06pp), coalition
(sign-flipping), one-sidedness (+0.16pp, null-equal, gradient BACKWARDS), chaser, joint-lift, VPIN.
The only rule that has ever survived a walk-forward is the momentum board's, which requires price
confirmation. `lift-results.md` §8a is the sharpest version: the footprint is real, persistent, and
carries **zero** forward edge — persistence and predictiveness are inverted.

**Therefore the honest expectation is that H1 fails.** It is run because it is free, because it is the
first well-powered test of this coordinate, and because a refutation is worth having in writing.

## 3. Definitions — fixed now, not adjustable after seeing results

Let `H, L, C` be the RAW printed high, low and close of session `i` (decisions on raw bars; returns on
adjusted — `alpha_lib.py:8-18`).

**Relative volume.** `rvol5(i) = mean(volume[i-4..i]) / mean(volume[i-19..i])`. Identical to
`overlay_test.features()`, deliberately, so this study and the board speak the same units. Note it is
a 5-day/20-day volume REGIME ratio, not a single bar's RVOL. The source thesis's `RVOL > 3.0` is a
bar-level quantity; the numeral coinciding with `EXHAUST_RVOL = 3.0` is a coincidence and the two must
never be used to argue about each other.

**Narrowness, three definitions.** Lower raw value = narrower. All three are computed; the sign must
agree across them.

| id | formula | what it controls for | role |
|---|---|---|---|
| `nr_self` | `(H-L) / median(H-L over i-19..i)` | the name's OWN recent range regime | **PRIMARY** |
| `nr_tick` | `(H-L) / tick_size(C)` | the IDX price-banded tick ladder | robustness |
| `nr_lit` | `(H-L) / C` | nothing — the thesis's literal form | robustness |

`nr_self` is primary because it is unit-free and stock-normalised, so it cannot be a price-level or
volatility-regime screen in disguise. `nr_lit` demonstrably IS one: with ticks at 1/2/5/10/25 by band,
one tick is 1.0% of price at Rp500-2000 (so only a ZERO-range bar can satisfy the thesis's `< 0.005`)
and 0.10% at Rp25,000 (where five ticks satisfy it). Same defect as the DEWA lab's fixed 100,000-lot
threshold silently tightening 62% as the stock ran.

**Narrowness score.** `score = -nr`, so HIGHER = NARROWER. Quintile 5 = narrowest, quintile 1 =
widest. The headline statistic is **Q5 - Q1**, and the thesis predicts it is POSITIVE.

**Population.** Symbol-days with (a) 20 sessions of prior history, (b) `rvol5 >= 1.5`, (c) a computable
forward excess return. The 1.5 floor is the momentum board's own `RVOL_MIN` — an existing constant,
not a fitted one. The full `rvol5` x narrowness surface is reported as a read-out with per-cell n, but
the PRIMARY population is fixed at `rvol5 >= 1.5` and does not move.

**Outcome.** `Panel.excess_return(sym, i, k, entry_lag=1)` — excess over IHSG, entry at the close of
`i+1`. Primary `k = 5`; `k = 3` and `k = 10` reported. Entry lag 1 is mandatory here as everywhere.

**Quintiles are pooled**, not per-date: the primary feature is already stock-normalised, and at
`rvol5 >= 1.5` a per-date sort would put a handful of names into five buckets.

## 4. Measured occupancy — computed 2026-08-26, BEFORE any return

Counting the state before computing a return is `lift_probe.py`'s pattern and it is what keeps this
pre-registration honest.

```
panel                     159 symbols x 475 sessions, 2024-08-15 .. 2026-08-14
symbol-days with history  68,463
rvol5 >= 3.0                 668  (0.98%)   325 distinct dates
   narrow nr_lit  < 0.005      90
   narrow nr_tick <= 3        146
   narrow nr_self  < 0.7       70
   wide   nr_self >= 1.3      410
rvol5 >= 2.0               3,670  (5.36%)   450 distinct dates
   narrow nr_lit / nr_tick / nr_self   409 / 839 / 416      wide 2,123
```

**And the reason the obvious study is NOT the one being run.** The repo's exhaustion result
(-1.85pp 3d / -3.82pp 5d) is measured on ACCUMULATION EVENTS at `rvol5 >= 3.0`, a population of
**197 stock-days**, of which the narrow cell is **7 / 13 / 17** depending on definition. Partitioning
that effect is impossible. H1 therefore drops the accumulation requirement and tests narrowness as a
continuous sorter on the full panel. **H1 does not partition the exhaustion effect and no result from
it may be described as doing so.**

## 5. Inference

- **Bands: `lift_lib.date_block_bootstrap`**, which resamples WHOLE PANEL DATES in 30-day blocks with
  10th/90th bands. Same-calendar-day cross-sectional co-movement is the dominant dependence on IDX; a
  per-observation bootstrap would report a band far too narrow. Never a z-test: the same data once
  gave z = -3.47 ("decisive") against a block bootstrap reading of 5.2% ("unremarkable").
- **`lift_lib.blocks_with_treatment` printed next to every interval.** Below
  `MIN_BLOCKS_INFERENTIAL = 15` the cell is labelled DESCRIPTIVE and cannot carry a verdict.
- **Baselines, computed first.** The unconditional mean over all 68,463 symbol-days, AND the mean
  within the `rvol5 >= 1.5` population. Q5-Q1 is a within-population difference so baselines cancel,
  but quoting a conditional statistic without its unconditional baseline credits the signal with the
  universe's drift (`lift.md` §3d corollary).
- **Null: feature-shift.** Circularly shift each symbol's feature series by an independent random
  offset, leaving that symbol's return series in place. Preserves each stock's return distribution and
  each feature's autocorrelation while destroying feature-to-return alignment. Same logic as
  `accum_test.shift_dates`, applied to features rather than flows. 200 draws. A label-shuffle null is
  NOT run: it is a broken control here for the same reason it was broken in the accumulation work —
  it agrees with the real result by construction.
- **Folds: 4 equal CALENDAR stretches**, never equal event counts. Per-fold n printed; a fold with
  n < 50 is reported as n/a rather than as a stability result.
- **Check 0 runs in the harness, not in prose.** `trade_backtest.check_zero(p, cands, bar=0.005)` on
  momentum candidates over the same window. If the known-good rule does not clear +0.5pp here, nothing
  this study produces can separate a bad rule from a bad period, and the run stops with exit code 4.

## 6. Pass bar — declared in advance, ALL must hold

1. **Primary.** `nr_self` Q5-Q1 at k=5 is **>= +1.0pp**, with the 10/90 date-block band **clear of
   zero**.
2. **Gradient monotone in >= 4 of 5** quintile steps at k=5.
3. **Sign stable across all three narrowness definitions** (`nr_self`, `nr_tick`, `nr_lit`).
4. **Feature-shift null within +/- 0.3pp of zero.**
5. **>= 3 of 4 calendar folds positive** at k=5.
6. **Check 0 passes** on the window.
7. `blocks_with_treatment >= 15` for the primary cell.

## 7. Refutation — declared symmetrically

- **A flat or INVERTED gradient refutes H1.** It does not license using the loosest threshold. This
  repo has produced two inverted gradients — one-sidedness (+0.70 -> +0.16 -> -0.39pp as `osr`
  tightened) and joint-lift age (+0.016 -> +0.003 -> -0.037) — and in both cases the loosest cell
  carried the whole number, which is the signature of a variable that is doing nothing.
- **Sign instability across the three narrowness definitions refutes H1**, and specifically indicates
  the coordinate is a price-level or volatility-regime artifact rather than effort-vs-result.
- Q5-Q1 below +1.0pp, or a band straddling zero, is a FAIL and is written up as such.
- If the null reproduces the real result to within 0.3pp, the result is indistinguishable from noise
  regardless of its point estimate. This is exactly how one-sidedness died (+0.16pp real vs -0.06pp
  null, a 0.22pp gap inside a +/-0.3pp tolerance).

## 8. What ships, and what does not

**Nothing ships into the momentum board from this study.** Hard constraint from the user: the board
and every file it imports (`build_momentum_board.py`, `momentum_setup.py`, `overlay_test.py`,
`trade_plan.py`) are not touched. `momentum_board.json` must diff byte-identical after this work.

If H1 passes, the outcome is a **read-out column** on a separate surface, and a proposal — to be
approved separately — to promote it into `rank_score`'s existing +/-20% quality tilt. Never a veto:
the RVOL1 exhaustion veto cost -0.66pp and the CLV structure veto -1.01pp, both at the **16th
percentile** of random filters of the same size, and the entire intraday entry-gating layer topped out
at the 60th.

If H1 fails, it is recorded here beside the other eight and the VSA half of the source thesis is
closed for zero API requests.

## 9. Legs of the source thesis already closed — do not re-test

| leg | verdict | evidence |
|---|---|---|
| passive 5-block TWAP ladder | dead | `entry_fill_test.py`: P0 one-shot at close +2.03% size-weighted excess; every ladder at the 0th-2nd percentile of a same-acceptance null; P2 is 100% filled with no adverse selection and still loses 39bp, because a lower fill puts the stop lower |
| fixed 2% stop | dead | `RiskConfig.min_stop_pct = 0.025` is already wider; the 1.5xATR stop's entire validated product is tail protection (worst -2.88R vs -4.67R) |
| "buy the quiet absorption" | dead | eight refuted theses |
| closing distribution (close < session VWAP) | dead | measured on 12,532 cached sessions: P(close<VWAP)=0.558, P(close<(H+L)/2)=0.538, **agreement 0.892** against a declared redundancy bar of 0.75; the daily form (`clv`) already failed as a veto at -1.01pp |
| "footprints are visible therefore predictive" | dead as prediction | `lift-results.md` §8a |

**Mis-specifications, fixed in §3 rather than inherited:** `(H-L)/C < 0.005` fires on **48.5%** of all
775,583 cached 5m bars and is a price-level screen (hence `nr_self` as primary); the thesis's RVOL
denominator ("average intraday candle volume") makes the open and close buckets clear 3.0 on an
average day by construction, because IDX intraday volume is U-shaped; and the MOC exclusion window is
wrong for this feed — there are no 15:50/15:55 buckets, MOC lands entirely in **16:00**.

## 10. Result — **VERDICT: FAIL. H1 is refuted.** (2026-08-26)

Produced by `scripts/effort_test.py`; full payload in `data/panel/effort_test.json` with panel
fingerprint. Cost: **0 API requests.** n = 8,243 symbol-days at `rvol5 >= 1.5` over 440 dates,
16 blocks (inferential). **Check 0 PASSED** — the known-good momentum rule earns **+0.98pp** in this
window on n=1,564 against a +0.5pp bar — so unlike the first one-sidedness run, this verdict counts.

**Five of eight pre-registered checks fail.**

| # | check | result | |
|---|---|---|---|
| 1 | Q5-Q1 >= +1.0pp | **+0.141pp** | FAIL |
| 1b | band clear of zero | **[-1.13, +1.37]** | FAIL |
| 2 | monotone >= 4/4 steps | **2/4** | FAIL |
| 3 | sign stable across definitions | **flips** | FAIL |
| 4 | null within +/-0.3pp | **+0.007pp** (sd 0.804, 200 draws) | PASS |
| 5 | folds positive >= 3/4 | **2/4** | FAIL |
| 6 | check 0 | +0.98pp | PASS |
| 7 | blocks >= 15 | 16 | PASS |

### 10.1 The gradient is U-SHAPED, and that is the finding

```
quintile        n     mean pp    median pp     hit
Q1 widest    1,648     +2.729       -0.060    49.7%
Q2           1,610     +1.548       -0.025    49.9%
Q3           1,681     +1.314       -0.057    49.4%
Q4           1,624     +1.709       -0.107    49.4%
Q5 narrowest 1,680     +2.871       -0.133    48.9%
```

Both tails beat the middle. **Narrowness does not order returns; extremeness does.** The widest
quintile (+2.73pp) is statistically indistinguishable from the narrowest (+2.87pp), and both sit
~1.5pp above the middle three. This is a volatility/attention effect and it is precisely what
effort-vs-result predicts against: Wyckoff requires narrow to beat wide, and here wide and narrow are
the same number.

Note also the sign disagreement between mean and median: **the median is NEGATIVE in every
quintile** while every mean is strongly positive, and the hit rate is ~49% throughout and barely
moves. The whole cross-section is a right tail. Nothing here sorts the typical day.

### 10.2 The sign flips across definitions — the artifact clause fires

```
nr_self  Q5-Q1  +0.141pp   monotone 2/4     (stock-normalised, PRIMARY)
nr_tick  Q5-Q1  +1.559pp   monotone 2/4     (tick-ladder scaled)
nr_lit   Q5-Q1  -0.840pp   monotone 1/4     (the thesis's literal form)
```

§7 declared in advance that sign instability "specifically indicates the coordinate is a price-level
or volatility-regime artifact rather than effort-vs-result". It fires. `nr_lit` and `nr_tick`
disagree in SIGN on the same days, and they differ only by whether the denominator is price or the
tick ladder — which is the confound §3 predicted before any number was computed. **The thesis's
literal `(H-L)/C < 0.005` measures the tick ladder, not absorption.**

### 10.3 Real vs null: indistinguishable

Real **+0.141pp**, feature-shift null **+0.007pp**. A gap of 0.13pp against a declared tolerance of
+/-0.3pp. This is the same death one-sidedness died (+0.16pp real vs -0.06pp null, 0.22pp gap). The
null's own sd is 0.804pp — six times the real effect.

The null passing is the part that makes the verdict trustworthy: the harness does not leak, so the
near-zero result is a property of the world and not of the code.

### 10.4 Folds

```
1  2024-09-11 .. 2025-02-27   n=1,543   +2.281pp
2  2025-02-27 .. 2025-08-27   n=2,092   -1.342pp
3  2025-08-27 .. 2026-02-09   n=2,416   +1.057pp
4  2026-02-09 .. 2026-08-05   n=2,192   -0.893pp
```

Alternating sign, 2 of 4 positive, range 3.6pp. No period structure.

### 10.5 The cell that would have been promoted — and why it is not

The `rvol5 >= 3.0` band shows narrowest-quintile **+8.73pp on n=72** against a negative middle:

```
rvol [3.0,inf)  n=466   Q1:+1.97(246) Q2:-1.57(75) Q3:-0.69(41) Q4:-0.96(32) Q5:+8.73(72)
                Q5-Q1 +6.76pp   band [-0.03, +13.54]
```

This is the most seductive number in the study and it is **not a result**. The band is 13.6pp wide
and its lower edge sits on zero. The gradient inside the band is U-shaped, not monotone. n=72. And
it is a post-hoc cell selected by eye from a 15-cell surface after the primary failed — the exact
move this repo's discipline exists to prevent, and the reason every surface cell is printed with its
own interval rather than as a bare mean.

For completeness, the other two bands: `[1.5,2.0)` Q5-Q1 **-0.83pp** [-1.93,+0.28]; `[2.0,3.0)`
**+1.46pp** [-0.80,+3.63]. Neither band excludes zero, and the three bands do not agree in sign.

### 10.6 What is NOT claimed, and the one follow-up that would need its own pre-registration

- **Elevated volume itself is worth something and narrowness adds nothing to it.** Within-population
  mean is **+2.038pp** against an unconditional **+0.862pp** across all 68,787 symbol-days. The
  `rvol5 >= 1.5` population outperforms by ~1.18pp; splitting it by range recovers nothing further.
  Do not read H1's failure as "high volume is uninformative" — read it as "range adds nothing".
- The U-shape suggests an **extremeness** coordinate (`|nr_self - 1|`) rather than a directional
  narrowness one. That is a DIFFERENT hypothesis with a different sign prediction, and promoting it
  now on the strength of a shape seen after the primary failed would be exactly the post-hoc move
  that §7 forbids. **It was not tested.** If ever revisited it needs its own pre-registration, and it
  should be checked first against the null objection of `lift.md` §3e: ask what that statistic does
  under the null given the heterogeneity actually present, because a U-shape in pooled data across
  heterogeneous units can be guaranteed before any data are seen.
- VSA is not refuted at BAR resolution. H1 is a DAILY test. Bar-level `freq` -- the thesis's one
  corroborated leg -- has never been available historically and remains untested. See H4.

### 10.7 Running tally

Refuted flow theses, now **nine**: quiet accumulation (-0.04/+0.01pp), net-persistence (-0.06pp),
coalition (sign-flipping), one-sidedness (+0.16pp, null-equal, gradient backwards), chaser,
joint-lift, VPIN (killed at measurement), and now **effort-vs-result (+0.141pp, null-equal, gradient
U-shaped, sign unstable)**. The only rule that has ever survived a walk-forward here remains the
momentum board's, which requires price confirmation.

**Nothing ships. The momentum board is unchanged.**
