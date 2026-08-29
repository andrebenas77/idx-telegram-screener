# Signed aggressor flow under a lid — thesis #13

**Status: PRE-REGISTERED 2026-08-29, before `flowdir_lib.py`, `flowdir_measure.py` and
`build_flow_board.py` exist, and before any forward return has been computed.**

Thesis #13. The instrument scoreboard in §2 was measured before this file was written and is
reproduced by `flowdir_measure.py`; everything downstream of §4 is declared here in advance.

This study is **forward-only by construction**. No retrospective forward-return test is run, and
§3 explains why that is a measurement result rather than caution.

---

## 1. The question

Three hypotheses, from the desk:

- **H-A — confirmation.** A name already on the momentum board, with one-sided *buy* aggressor
  pressure, continues further than a momentum name without it.
- **H-B — divergence.** Sustained one-sided buy pressure while the price is *pinned* marks
  accumulation before the markup, and is an entry.
- **H-C — the momentum-market caveat.** IDX pays momentum, not patience. H-B is expected to work
  only where RSI and money flow are already strong.

The desk's original formulation was in terms of the **rate of change of VPIN**. Both halves of
that phrase are replaced here, and §2 and §3 say why:

- *VPIN* — an unsigned, second-moment "toxicity" object — is not the quantity the hypotheses are
  about, and is not measurable from bars on IDX (thesis #8 killed it at rho = −0.028). The
  quantity here is the **signed direction** of aggressor flow, and it *is* measurable.
- *Rate of change* presumes a level with memory. The daily level's lag-1 autocorrelation is
  **+0.073**. There is nothing to differentiate. H-B is restated as a **level** hypothesis.

**The words VPIN and toxicity do not appear anywhere else in this study.** Thesis #8's surviving
conclusions — that a cheap proxy should be the tick rule, and that true toxicity is negatively
related to realized volatility (−0.483) — attach to the *unsigned* object and may not be carried
across to a signed one. Gate 4 enforces this.

## 2. The instrument, measured against ground truth before anything was built

IDX stamps the true aggressor side on every print of a closed session, so "who crossed the
spread" is observable. 41 tapes are cached; **12 are truncated** and are rejected by the existing
`vpin_validate.truncated()` guard against the gross panel's own `buy_freq`. **29 usable**
(20 symbols, 23 dates).

Ground truth: `TRUE_signed` = mean over 24 equal-volume buckets of `(V_buy − V_sell)/V`, RG only,
from the vendor aggressor flag.

| instrument | rho vs TRUE unsigned | rho vs **TRUE signed** |
|---|---|---|
| hourly tick-VPIN | +0.347 — degenerate, median 0.963, sd 0.029 | — |
| hourly signed flow | +0.086 | +0.513 [+0.180, +0.740] |
| daily signed flow (one bar) | — | +0.472 [+0.127, +0.715] |
| daily open-to-close return | — | +0.577 [+0.267, +0.779] |
| **5-min signed flow** | **+0.593 [+0.290, +0.788]** | **+0.782 [+0.583, +0.893]** |

**Hourly is rejected, and the reason is not the point estimate.** It loses to a *free two-price
rival* — the daily open-to-close return scores +0.577 against its +0.513 — correlates +0.543 with
that rival, and its partial correlation given the daily bar is +0.331 with CI **[−0.040, +0.622]**,
which includes zero. An hourly flow board is the daily return wearing a flow label.

**5-minute is admitted.** rho +0.782 against true direction, above the tick rule +0.559 that
thesis #8 settled on, while only +0.219 correlated with the daily CLV and +0.404 with the daily
return. It carries intraday information the daily bar does not.

### 2a. The resolution ceiling, and why it is a warning rather than a verdict

Effective independent bars per session, `1/sum(w_b^2)` on volume weights:

| | sum(w^2) | effective bars | ceiling vs TRUE, per-bar CLV | measured |
|---|---|---|---|---|
| h60 | 0.265 | 3.77 | 0.419 | 0.513 — **above** |
| m5 | 0.068 | 14.76 | 0.585 | 0.782 — **above** |

Under a model where each bar carries the session imbalance plus independent noise, the attainable
correlation is `sd(TRUE)/sqrt(sd(TRUE)^2 + var(2clv−1)*sum(w^2))`. Both instruments exceed it, so
neither is a clean average of independent per-bar aggressor signals: there is a shared channel,
and the price path is the obvious candidate.

**This is why the pass condition is an INCREMENT, not a level.** A high rho that merely restates
the daily return is worth nothing. Gate 2 is the load-bearing test.

## 3. Why there is no retrospective study, stated as measurement

`check_zero` (`accumulation.md` §6.0) requires the known-good momentum rule to earn at least
+0.5pp in a window before any result from that window is read.

| window | sessions | 30-day blocks | check 0 |
|---|---|---|---|
| full panel 2024-08..2026-08 | 481 | 16 | **+0.97pp PASS** — but no 5-minute data exists there |
| m5 store 2026-02-11..08-19 | 120 | 4 | **−0.98pp FAIL** |
| last three months | ~60 | 2 | **−2.16pp FAIL** — worst cell on record |
| "last one month" | ~20 | **0.67** | not computable |

Server retention is tiered: tf=D and tf=60 give 2 years, tf=15 one year, **tf=5 about six
months**. The window where the instrument exists and the window where a verdict is possible do
not overlap, and buying resolution cannot fix it — this is the same blockage that left thesis #10
inconclusive.

Additionally, on the 2-year window the tests would be unpowered: with a 5-day excess sd of
**10.43pp** (measured on this panel; an earlier draft quoted 12.9pp, which came from a design
estimate and from a `flowdir_power.excess_sd` that was silently returning its own fallback
constant — see §11.6) and a date-clustering design effect calibrated from the `dispersion.md`
observed band, the MDE at 80% power for H-A is several times the +0.6pp bar.

**A one-month backtest is 0.67 of one block. It cannot confirm or refute in either direction.**
The rate-of-change intuition is about the *holding horizon* (k = 3–5 days) and is respected there;
it does not shorten the history needed to estimate an edge.

So: **measurement now, prediction later, from a record that does not yet exist.**

## 4. Definitions — fixed here, trailing-only, look-ahead free

Continuous session only, `09:00 <= hhmm < 16:00`. The 08:55 pre-opening auction is one
uncontested print; the 16:00 bucket is the MOC.

- **`seas(sym, hhmm, i)`** — median volume in that 5-minute bucket over the previous **60**
  sessions of that symbol, strictly before `i`. Mandatory: IDX intraday volume is U-shaped.
  Thesis #12 lost its instrument (+0.512 to +0.384) exactly by using full-sample seasonals.
- **`sflow(i)`** = `sum((2*clv_b − 1) * V_b) / sum(V_b)` over the session bars, where
  `clv_b = (C_b − L_b)/(H_b − L_b)`. Signed, in [−1, +1]. A bar with `H == L` is **dropped**,
  never scored 0.5: an ARA/ARB-locked bar has genuinely undefined CLV.
- **`sflow_seas(i)`** — the same with `V_b` replaced by `V_b / seas(sym, hhmm_b, i)`.
- **`sflow5(i)`** = mean of `sflow` over sessions `i−4..i`.
- **`sflow_rel(i)`** = `sflow5(i) − median(sflow5 over the prior 60 sessions, strictly before i)`.
  Within-symbol, look-ahead free. **This is the sorter.**
- **`d_sflow(i)`** = `sflow_rel(i) − sflow_rel(i−5)`. Computed and stored, but **not a sorter
  unless Gate 3 passes.**
- **`coil(i)`** — from the existing `overlay_test.features`: `range_pct` in the bottom tercile of
  the 120-session history of that symbol, **and** `rvol5 < 1.5`, **and** `dd60 >= −0.10`.
- `rsi`, `cmf20`, `rvol5`, `dd60`, `trend` are taken from `overlay_test` unchanged. The desk
  phrase "strong money flow" is `cmf20`, which already exists and is already a momentum-board
  read-out column.

**Quintiles are formed pooled on `sflow_rel`, not within date.** With around 20 eligible names per
session a within-date quintile is 4 names and is noise.

## 5. Gates — all zero-request, run before the board is built

`flowdir_measure.py` writes `data/panel/flowdir_measure.json` and **exits non-zero on any
failure**. The board is not built on a failed cascade.

| # | gate | pass condition |
|---|---|---|
| 0 | resolution ceiling `1/sum(w^2)` for m5, reported beside the measured rho | reported, not gating. A rho above ceiling must be explained by Gate 2, never celebrated |
| 1 | **split-half reliability** of `sflow` — odd vs even bars within session, Spearman-Brown corrected, over the whole m5 store | `rho_xx >= 0.30`. Runs first because it is estimated on ~12,000 symbol-days (SE ~0.01) while the tape rho has n_eff ~20 |
| 2 | **increment over the best free rival** against `TRUE_signed`, date-clustered bootstrap over the 29 clean tapes | 10th percentile of `rho(sflow) − max rho(rival)` **> 0** |
| 3 | **does the rate of change exist**, on `sflow_seas` | lag-1 autocorrelation `>= +0.15` **AND** the ROC autocorrelation departs `>= 0.10` from the white-noise difference-filter response at some lag <= 5 |
| 4 | **estimand hygiene** — an unsigned arm scored against `TRUE_unsigned` | `rho >= +0.40` **AND** `rho` vs realized volatility `< 0`. If it fails, no unsigned column ships and no thesis-#8 conclusion may be cited |

Rivals for Gate 2 are fixed now and may not be added to afterwards: the open-to-close return,
`sign(open-to-close)`, an equal-weighted (not volume-weighted) per-bar CLV mean, and `sflow_seas`.

**Gate 3 is expected to FAIL** on the pre-planning measurement (lag-1 +0.073; the ROC
autocorrelation sits within 0.09 of the white-noise prediction at every lag). Declaring that
expectation here is the point: a pass would be a surprise worth acting on, and a failure is not a
disappointment to be worked around.

## 6. The board — three cells, and the control that tests H-C

| cell | condition | tests |
|---|---|---|
| **CONFIRM** | `is_momentum` true AND `sflow_rel` in the top quintile | H-A |
| **DIVERGENCE** | `coil` AND `sflow_rel` top quintile AND `sflow5 > 0` AND `rsi >= 55` AND `cmf20 > 0` | H-B and H-C |
| **DIVERGENCE-WEAK** | as above but failing the `rsi`/`cmf20` leg | the **control** for H-C |

Predicted signs, declared now: `CONFIRM > momentum-without-flow`; `DIVERGENCE > 0` against a
matched baseline; `DIVERGENCE > DIVERGENCE-WEAK`.

The weak cell exists so that H-C cannot become a post-hoc rescue. `exit_test.py` and
`dispersion.md` §9.3 both name promoting a secondary after the primary fails as forbidden; the
only legitimate way to test "it works when RSI is strong" is to declare the stratification and
its control before seeing an outcome. That is done here.

## 7. Honest prior

**Twelve flow theses have been run in this repo. None has shipped.** DIVERGENCE is a near-twin of
the ABSORPTION state, already refuted at +0.16pp against a +1.2pp bar with a gradient running
backwards, and it shares its conditioning set with theses #1 to #4.

What is genuinely new is the instrument. Every prior absorption test inferred accumulation from
**broker net value** or from the position of the close in the daily range. None had a measure of
who crossed the spread, validated against the vendor aggressor flag at rho +0.782. That is the
entire bet, and it is a modest one.

The joint-lift standing lesson applies directly and is recorded here as the most likely way this
dies: *the state with the order-splitting signature has no edge; the state with no persistence
has the only edge. The whale footprint is real and measurable; it just does not predict.*

## 8. The forward record, and when it may be read

`build_flow_board.py` appends one row per candidate per session to `data/panel/flow_record.csv`,
**append-only, never rewritten**. Forward returns are joined later from the panel, so no outcome
touches the file at write time and look-ahead is impossible by construction.

Block count depends on **calendar span**, not on how many names qualify per session:

| milestone | sessions from 2026-08-29 | approx. |
|---|---|---|
| MDE falls to 2x the attenuated bar | 263 | ~Sep 2027 |
| 8 blocks — descriptive checkpoint only, no verdict | 240 | ~Aug 2027 |
| 15 blocks — inferential at `BLOCK_DAYS=30` | 450 | ~Aug 2028 |
| 15 blocks under the `block=15` sensitivity arm | 225 | ~Jul 2027 |

The binding constraint is the block count, not the MDE: power arrives about a year before
independence does, and reading on power alone would be the same error as a z-test on clustered
events.

The `block=15` arm is **declared now as a sensitivity, never a promotion**. The `lift_lib` rule is
that the block must exceed the 95th percentile of holding period; for a fixed k=5 horizon 15 days
is 3x, whereas `BLOCK_DAYS = 30` was set for first-passage barriers with 15 to 25 day holds. The
primary remains 30. Both are reported side by side, and the `block=15` number may never be quoted
alone. Declaring it now is what stops it being renegotiated in 2027 when the primary is short.

`flowdir_power.py` computes `blocks_with_treatment` and the MDE at 80% power and **refuses to
compute a lift** before the record is readable.

Outcome, when read: `Panel.excess_return(sym, i, k, entry_lag=1)`, excess over IHSG. Primary
k=5; 3 and 10 reported. Mean excess, not hit rate — the payoff is right-skewed and ranking on hit
rate was already shown to invert out of sample. Bands are 10th/90th from
`lift_lib.date_block_bootstrap`, every interval quoted with its block count, and `check_zero` runs
on the record window with its universe convention stated.

## 9. Refutation

- A flat or inverted gradient across `sflow_rel` quintiles refutes it, and is not a licence to use
  the loosest cut.
- A feature-shift null within 0.3pp of the real result: indistinguishable from noise whatever the
  point estimate.
- **Failing orthogonality while passing the headline is a REFUTATION, not a partial pass.** If the
  effect does not survive in at least 2 of 3 RVOL terciles, and separately in at least 2 of 3
  terciles of the same-day return, it is momentum wearing a flow label.
- If Gate 2 fails, nothing else is read: the instrument does not beat two prices.
- If `DIVERGENCE` and `DIVERGENCE-WEAK` are indistinguishable, H-C is refuted and may not be
  reintroduced as a filter.

## 10. What ships

Nothing into the momentum board. `build_momentum_board.py`, `momentum_board.json`,
`docs/momentum.html` and `docs/index.html` are md5-verified byte-identical before and after every
run of the new board. `run_daily.sh` is untouched until the board has run clean manually for a
week. Nothing writes into the `quant_screener` bot repo.

The board itself ships either way, carrying `validated: false` and a `DESCRIPTIVE` or `REFUTED`
tag on every row and in the page header, with the failing numbers printed on the page — the
treatment ABSORPTION already gets in the bot. It never feeds `trade_plan.py` and never sizes a
position.

## 11. Result -- **1 of 4 gating checks passed.** (2026-08-29)

`scripts/flowdir_measure.py`; payload `data/panel/flowdir_measure.json`. Zero requests. 41 cached
tapes, **29 usable** after the truncation guard rejected 12; 12,485 m5 sessions over 111 symbols;
51,488 h60 sessions over 159. The ground-truth clock is checked against `vpin_validate.tr_daily`
on every tape, so TRUE here is the same object thesis #8 measured.

| # | gate | result | |
|---|---|---|---|
| 0 | resolution | m5 **21.5** effective bars, ceiling 0.593; h60 **4.1**, ceiling 0.449 | reported |
| 1 | reliability rho_xx >= 0.30 | **+0.270** within-symbol | **FAIL** |
| 2 | increment 10th pct > 0 | **+0.251, band [-0.002, +0.415]** | **FAIL** |
| 3 | rate of change exists | lag-1 **+0.065**, departure from white **0.052** | **FAIL** (predicted) |
| 4 | estimand hygiene | rho +0.593 vs unsigned truth, **-0.389** vs realized vol | PASS |

### 11.1 The instrument is the best cheap one measured here, and it still does not clear

`sflow_seas` scores **rho +0.856** against true signed aggressor flow, against +0.604 for the
sign of the open-to-close return and +0.564 for the return itself. The increment is **+0.251**
-- but the date-clustered 10th percentile is **-0.002**. It misses by two thousandths.

That is not a near-miss to be rounded up. It is 29 tapes over 23 dates with an effective n of
20.5, and the band is honest about what that supports. The instrument is *probably* better than
two prices; the evidence available does not establish it.

For contrast, h60 signed flow scores +0.513 and **loses outright** to the return's +0.577. Gate 0
explains why: 4.1 effective bars cannot carry more.

### 11.2 Specification error in Gate 2, recorded rather than edited away

The rival list in section 5 included `sflow_seas`, while section 4 of this same document mandates
the trailing seasonal as the instrument *normalisation*. Both cannot hold. Measured, `sflow_seas`
(+0.856) beats plain `sflow` (+0.814), so the primary as written fails on the instrument losing
to a better version of **itself** -- which says nothing about the price-path question the gate
exists to answer. Requiring it as a rival also cut the sample from 29 tapes to 21, because the
trailing seasonal needs 20 prior sessions.

All three readings are computed and stored in the payload:

```
as pre-registered  sflow    vs 4 rivals   n=21  rho +0.814  increment -0.042  [-0.153, +0.033]
sflow vs price-only rivals, full sample   n=29  rho +0.782  increment +0.134  [-0.087, +0.266]
GATING sflow_seas vs price-only rivals    n=21  rho +0.856  increment +0.251  [-0.002, +0.415]
```

The gating reading is the one the gate was written to perform. **Every reading fails**, so the
correction changes no verdict -- which is the only condition under which making it is legitimate.

### 11.3 The desk rate-of-change variable does not exist

```
level ACF lags 1-5   +0.065  +0.048  +0.036  +0.043  +0.006
ROC   ACF lags 1-5   +0.718  +0.434  +0.137  -0.155  -0.448
white-noise filter   +0.700  +0.400  +0.100  -0.200  -0.500
```

The level is white. The ROC's entire autocorrelation structure is the mechanical response of a
difference filter to white noise, matching to within **0.052** at every lag, and `corr(ROC, level)`
is **+0.654** against the +0.707 identity. Differencing this series produces a number that looks
like a dynamic and is a filter artefact.

This was pre-registered as expected-to-fail, and it failed. **H-B stands as a LEVEL hypothesis
only.** No rate-of-change column is shown on the board; `d_sflow` is written to the record so the
question can be reopened if a future instrument has memory, and it sorts nothing.

### 11.4 What Gate 4 bought

`rho(TRUE unsigned, realized volatility) = -0.483` on this sample -- **the same figure thesis #8
published**, reproduced on independent code and a partly different tape set. The unsigned bar arm
tracks unsigned truth at +0.593 and runs -0.389 against volatility, so it reproduces the sign.
That is a genuine replication of the one finding thesis #8 kept.

It is also the boundary marker: it belongs to the *unsigned* object, and nothing in the signed
study may borrow it.

### 11.5 Verdict

**Thesis #13 does not clear its own gates.** The board ships DESCRIPTIVE under section 10, and
the forward record starts. What was bought for zero requests: a measured scoreboard of every
cheap flow instrument available on IDX, a demonstration that hourly resolution cannot carry this
question, a replication of the thesis-#8 volatility finding, and a definitive answer that the
rate-of-change formulation has no variable underneath it.

**Thirteen theses have now failed or been blocked. Nothing ships into the momentum board, which
is unchanged and md5-verified.**

### 11.6 Two bugs found in `flowdir_power.py` on 2026-08-30, both mine

**The reported 5-day excess sd of 12.9pp was never measured.** `main()` built the panel with
`Panel(); p.load_prices()` and never called `load_benchmark()`, so `Panel.excess_return` hit its
`if a not in self.bench: return None` guard on **every** event, `excess_sd` collected zero
observations, and it returned its own hardcoded `0.129` fallback — which is exactly the 12.9pp
that was then quoted as a measurement. A plausible constant is the worst possible fallback: it
looks like a result. `excess_sd` now raises rather than falling back, and refuses to report from
fewer than 100 observations.

**Measured properly, the sd is 10.43pp.** MDE at 80% power on one-observation-per-date moves
25.94pp (was 32.08pp), and the sessions needed for MDE to reach 2x the bar falls from ~402 to
**~263**. No verdict changes — the record was and remains NOT READABLE, gated on the block count.

Separately, `excess_sd` sampled by walking `sorted(p.close)` and breaking out of both loops at
4,000 samples, consuming only the first 64 of 159 symbols — **alphabetically A..GEMS**. A prefix
of the alphabet, not a sample of the market, and one that would have shifted silently the moment
a symbol with an early code (COIN) entered the panel. Both fixed; the file now samples every
symbol with an adaptive per-symbol stride.
