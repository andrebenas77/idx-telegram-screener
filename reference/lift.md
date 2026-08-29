# Joint Lifting as a Markovian Lift — framework

**Written 2026-08-19, BEFORE the code.** Thesis #7 on IDX broker flow. Six have failed; see
`accumulation.md` and `chaser.md`. The pass bar is declared here so it cannot move afterwards.

## 1. The question

Last week's chaser test asked a *cross-sectional selection* question — given price is rising, does it
matter whether the buyer is a chaser or an accumulator? It found nothing (LOW +0.78pp / HIGH +4.14pp at
k=5, wrong sign, identity-shuffle null ±1.98pp).

This asks a *duration* question instead: **when both cohorts keep taking the offer, day after day, what
are the odds price continues up?** "Day after day" is an AGE, and age is the state variable the prior
test collapsed away.

## 2. Why "Markovian lifting" is the right frame, and which lift

A process becomes Markov once the state is enlarged ("lifted") with a sufficient statistic for its
history. Two canonical lifts exist:

1. **Hawkes with exponential kernel** — the pair (N_t, lambda_t) is exactly Markov.
2. **Semi-Markov / Markov-renewal** — for *any* sojourn-time law F, the pair **(state, age)** is
   Markov, because the residual-life law depends on the past only through age.

**Lift (2) is the right one here and lift (1) is wrong**, for two independent reasons:

- The exponential kernel that makes (N, lambda) Markov is exactly what Lillo–Mike–Farmer rules out.
  Order-flow sign has power-law memory; no finite exponential lift reproduces it.
- At DAILY aggregation, Hawkes parameters are not identified at all. They are not invariant to bin
  size, one bar carries at most one event per class, and any sub-stochastic lag-1 matrix has spectral
  radius below 1 trivially. **A daily "reflexivity index" would be a persistence statistic in a
  Filimonov–Sornette costume. It is dropped from this design.**

So the state is (J, k) with J in {NONE, A_ONLY, C_ONLY, JOINT} and k = age. Twelve cells, three
traded. No trend coordinate, no regime stratum, no RVOL, no 24-state chain — those cells are empty at
this panel size, and their absence is arithmetic, not taste.

## 3. What the prior test silently assumed

That today's LABEL is a sufficient statistic — i.e. that the partition collapsing all ages into one
block is **lumpable** in the Kemeny–Snell sense. That assumption is the thing under test. That is the
entire intellectual content of this thesis, stated correctly.

**But lumpability must be tested against the REWARD, not the transition kernel.** Testing whether age
predicts tomorrow's age is rigged: age is trivially predictive of age.

### 3a. The straw-man hazard test, and why it is banned

A pooled mixture of geometric sojourns across heterogeneous tickers is **provably completely
monotone** — a decreasing pooled hazard is GUARANTEED before any data are seen, whether or not the
thesis is true. **Any "the hazard declines, therefore memory" result on pooled tickers is void.** This
was the original stage-0 kill switch and it is withdrawn.

## 4. Definitions

Cohorts from `broker_profile.py`, field **`xr_trail` (lateness), NOT `xr_same`**. Same-day
correlation is a price-impact-and-size artifact and is plausibly why "chasers are foreign" was found.
Lateness is already the lagged construct (`_xr_trail` ends its window strictly at i-1) and its
disjoint-halves Spearman is +0.793, above the 0.7 stability bar. Point-in-time via
`schedule()` / `scores_for()`; MIN_OBS=3000, WINDOW=250, re-estimated every 21 sessions.

    chaser cohort C      = top quartile by lateness
    accumulator cohort A = bottom quartile by lateness

Lift indicator, two forms:

    WEAK   (flows panel, 25 months)  L_c = 1 iff cohort net value > 0
    STRONG (gross panel,  7 months)  L_c = 1 iff cohort value-weighted (buy_avg - VWAP) > 0
                                              AND cohort net value > 0

The second condition in STRONG matters: paying up while net short is distribution, not accumulation.

    J_t = (L_A, L_C)      JOINT = (1,1)
    age k = consecutive days at the current J, ONSET-ANCHORED (never continuation-anchored)
    K_strict: a run breaks on any non-lifting day.   [PRE-REGISTERED — K_tol1 is not reported as primary]

**Zero-sum caution.** The value-weighted average of all brokers' `buy_avg` IS the day VWAP, so
aggression scores are zero-sum across brokers. That forces cohort scores to be NEGATIVELY correlated
conditional on identity, so the joint rate may sit FAR BELOW the independence product. K1 measures it.
Do not rescue a low base rate by lowering the threshold: a threshold at or below zero redefines
"lifting" as "not the cheapest cohort", which is not lifting.

## 5. The functional: first passage, not conditional mean

The prior test measured a conditional mean forward return on a near-martingale, which is close to zero
by construction. First passage against ASYMMETRIC barriers is a different functional of the return law
and is non-trivial even at zero drift whenever returns are serially dependent.

    pi(k) = P(hit +2.0 ATR before -1.5 ATR | onset-anchored run of age k, enter at next open)
    driftless null: pi = b/(a+b) = 1.5/3.5 = 0.4286        <- hard, non-estimated benchmark
    expected R-multiple: E = 3.5*pi - 1.5   (ATR units)

Estimated by **directly counting empirical paths**, NOT via (I-Q)^-1 R. The fundamental-matrix
factorisation assumes next-day return is independent of next state — false by construction, since
tomorrow's state IS a function of tomorrow's price vs VWAP. The bias runs optimistic, on precisely the
number a screener would print.

Barriers reuse the validated execution layer (`trade_lib.RiskConfig.k_atr = 1.5`).

## 6. The kill switch

Run in order; stop at the first failure. Zero API calls — the panel is already on disk.

**K1 — OCCUPANCY. Count before modelling.** Publish, before looking at any return: base rate of L;
run onsets; runs reaching k = 1,2,3,4,5+; and per age the number of DISTINCT TICKERS and DISTINCT
non-overlapping 30-day CALENDAR BLOCKS.

> KILL if k>=3 runs come from fewer than 30 distinct tickers, OR fewer than 8 distinct 30-day blocks,
> OR the base rate is below 2%.

**K2 — THE PRICE-ONLY TWIN. The real kill test.** A twin event from OHLCV alone, zero broker data:

    c_t = 1 iff Close > (High + Low) / 2

That is exactly "close above the day's midpoint", which is algebraically the sign of
(Close - typical price) where typical price = (H+L+C)/3. Identical run construction, identical age
binning. Report, paired on the SAME calendar days:

    Delta_pi(k) = pi_joint(k) - pi_twin(k)

Day-pairing removes the market component and cuts the standard error by roughly a third.

> KILL if L is more than 75% recoverable from c_t alone AND Delta_pi straddles zero at BOTH k=1 and
> k=2. That means the broker data adds nothing over a variable computable from a price file — i.e.
> this is the sixth momentum thesis. **This is the single most likely way the thesis dies.**

**K3 — DECOMPOSITION.** Re-run K2 on flow-only (net buyers, no paid-up condition) and paid-up-only.
Signed net flow is itself long-memory and will show persistence on its own.

> KILL if the joint event is statistically indistinguishable from flow-only: the "taking the offer"
> framing is then decorative, and the thesis as posed is dead.

**K4 — PLACEBO.** Primary: **circular block shift** — shift each ticker's flow series by a COMMON
random multi-month lag, preserving calendar alignment across tickers AND within-ticker serial
structure, breaking only flow-to-own-price alignment. 1,000 draws.
Explicitly NOT ticker-shuffle as primary: pairing ticker i's flow with ticker j's prices destroys
within-ticker serial structure too, so its null is too tight and would make noise look like a finding.
Secondary: within-day broker-label permutation.

> KILL if the real Delta_pi falls inside the upper decile of the placebo distribution, or if tau (the
> SD of the calendar-day effect) dominates the spread of the state effects.

**K5 — COST.** E = 3.5*pi - 1.5. Round trip plus entry slippage — incurred precisely BECAUSE you are
buying alongside people lifting the offer — needs roughly pi >= 0.50.

> KILL if Delta_pi(k=2) is significant but its lower bound implies E below round-trip cost. A
> statistically real edge below cost is not a finding, it is a cost.

## 7. Inference

- **Resampling unit: whole panel DATES.** Every ticker's row for a date travels together. The dominant
  dependence on IDX is same-calendar-day cross-sectional co-movement.
- **Block = 30 trading days**, not ceil(n^(1/3)). The block must exceed the 95th percentile of
  holding period (~15–25 days under these barriers). A 10-day block splits treatment–outcome pairs
  across boundaries and biases the tail DOWNWARD, i.e. silently against the alternative.
- **Runs are reconstructed INSIDE each replicate**, never carried in pre-formed. A run occupies many
  dates; resampling a date-indexed object while carrying a multi-date object across it either
  fragments or double-counts.
- **Cohort labels are re-estimated inside every replicate.** Freezing them gives intervals conditional
  on the labels being right, which is the assumption in question. This will widen the bands. That is
  the point.
- **Report `n_blocks_with_treatment` next to EVERY interval.** Below 15 the interval is DESCRIPTIVE,
  not inferential, and must be labelled so.
- Bootstrap **bounded statistics only** — first-passage probabilities, differences of proportions.
  No tail exponent, no mean residual life: the n-out-of-n bootstrap is inconsistent for those if the
  heavy-tail premise holds, which would make any result self-refuting.
- Bands reported at the **10th/90th** percentile, not 5th/95th. With roughly 15 independent blocks a
  5% tail is not resolvable, and pretending otherwise is the same class of error as a z-test on
  clustered events.
- **Check 0** (`accumulation.md` §6.0) runs in the harness: `is_momentum` must earn at least +0.5pp
  in-window before any verdict here is read.

## 8. Power, declared in advance

At k>=3 the effective sample is expected to be 25–95 observations, implying an MDE of roughly 1.5–3%
over five days against 30–60bp of cost. **k>=3 is therefore reported DESCRIPTIVELY with its MDE
stated, and a null there must not be read as evidence of absence.** The primary pre-registered test is
at **k>=2**. If the age gradient at k=1,2 is flat or inverted, age is not the right state variable and
the thesis should be re-posed as a level thesis, not an age thesis.

## 9. Deliberately absent

Branching ratio / reflexivity index (not identified at daily frequency). Tail exponent alpha
(bootstrap inconsistent under the heavy-tail premise). "How much size the whale has left to buy" in
rupiah (unidentified). All three would have printed precise, confident, actionable numbers. Sector,
market cap and a fourth flow coordinate are also excluded: there is no headroom.

---

**RESULTS: see [lift-results.md](lift-results.md)** — run 2026-08-19. Verdict: underpowered (8 blocks vs a 15 floor), and the age gradient runs backwards: +0.016 / +0.003 / -0.037 net of a price-matched control. Thesis #7 fails.
