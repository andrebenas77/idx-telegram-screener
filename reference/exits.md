# Exits, sizing and fills on the momentum board

Written 2026-08-15. Same register as `accumulation.md` §6/§7: hypotheses, bars declared
before the numbers, and the failures recorded alongside the one thing that survived.

Motivating trade, now closed: ISAT bought 2200 on 2026-08-12, sold ~2500 on 08-14
(+13.6% gross, **exactly +2.0R** against the 2050 stop). The question was whether 2500
was early. The system had no opinion, because it has never had a rule above the entry
price. A second position opened the following session and ran under the same rules.

---

## 0. Three corrections to the measurement, made before anything was tested

The first result of this work was that the recorded evidence for the *stop* layer was
overstated. All three defects predate this file and all three were silent.

### 0.1 P&L was computed on RAW prices — corporate actions booked as returns (D3)

`simulate()` took decisions on raw bars (correct — a stop is a raw price) and *also*
computed the return from them. Any trade spanning an ex-date booked the adjustment as
P&L. **PACK on 2026-01-12 printed raw −91.7% against an adjusted +9.4%**: the stock rose
and the backtest recorded **−11.6R**. 17 of the 20 worst "stop" losses were ex-dates.
40 of 425 stop exits landed on a corporate action, mean −2.38R against −1.20R overall.

Fix: decisions stay on raw bars; the *return* converts via `f = close_adj / raw_close`
per leg. `f` is 1.0 on ordinary sessions, so it is a no-op on ~96% of trades.

Effect: the unstopped baseline's worst trade goes **−11.32R → −4.67R**, and mean excess
rises 1.34% → 1.75%. The old fat tail was mostly splits.

### 0.2 Stop fills used the prior close as a gap proxy — one-sidedly optimistic (D1)

`Panel` never parsed `open`, though `prices-*.csv.gz` carries it on 72,448/72,448 rows.
A stop gapped through overnight filled at `min(prior_close, stop)`. Measured across
29,028 stop episodes: **8.9% gap through the open, 0.0% are pessimistic**, mean −0.40%
of entry when it bites.

Opens are clamped into `[low, high]`; 1,271 rows (1.75%) print outside it, spread evenly
across **24 of 25 months** — a structural pre-opening-auction artefact, not a vendor
period, so no exclusion run is warranted. Clamping is the conservative reading.

### 0.3 What the stop layer is actually worth

| | recorded | corrected (D1+D3) |
|---|---|---|
| ATR stop + E2, mean excess | +1.44% | **+2.00%** |
| like-for-like over stoppable baseline | +0.82pp | **+0.64pp** |
| paired 95% CI (block bootstrap) | — | **[−0.06, +1.37]pp — straddles zero** |
| worst trade | −1.71R | **−2.88R** (baseline −4.67R) |
| folds won | 3/4 | 3/4, margins −1.00 / +0.31 / **+0.90** / +0.04 |

**The honest statement: the stop is bought for the TAIL, not for the mean.** The worst
ten trades improve from −4.7…−3.0R to −2.9…−1.8R, which is the real product at 1.5%
risk. The return edge is +0.64pp with a band that includes zero, and fold 4's margin
(+0.04pp) is inside the noise. Any future claim about the stop should quote the tail.

D2 (close-based exits filling at the next *open* rather than next *close*) resolves a
doc/code contradiction and is near-neutral (+0.02pp). Kept as a sensitivity, not adopted
— taking the more flattering of two defensible conventions after seeing the scores is a
search, not a decision.

---

## 1. Take-profit: REFUTED, and the gradient says why

### 1.1 The excursion study (`mfe_study.py`) — descriptive, ships nothing

n=1088 tradeable candidates. `post_touch_R = terminal_R − threshold` is the R still
earned *after* first touching a level — exactly what a full exit trades away.

| level | reached | post-touch R | 95% CI | gave it all back | finished ≥ level |
|---|---|---|---|---|---|
| +1.0R | 33% | **+0.88** | [+0.51, +1.26] | 16% | 60% |
| **+2.0R** | 16% | **+1.39** | **[+0.93, +1.85]** | **5%** | **72%** |
| +3.0R | 10% | +1.41 | [+0.84, +2.05] | 3% | 74% |

Positive with a band clear of zero at every level, and on the less-conditioned `fixed10`
horizon too (+0.52 at 2R, [+0.08, +1.03]). The reading rule declared before these
numbers therefore fires: **a hard target is destructive; build none.**

Context that matters for the trader: the median trade keeps only **40%** of its
close-basis peak and **33% of trades peak on session 1**. Give-back *feels* constant
because it is. But the payoff is right-skewed — cutting at a level kills the minority
of trades that pay for everything. Same shape as the broker-alpha result where ranking
on hit rate was worse than not ranking at all.

### 1.2 The bake-off (`exit_test.py`) — 14 configurations, all negative

Paired trade-for-trade against the incumbent. A `scale` leg never breaks the loop, so
the remainder's exit path is identical and the pairing is exact.

| rule | Δ bp of excess | 95% CI | folds |
|---|---|---|---|
| scale 33% @ +2R | **−31.9** | [−52.1, −13.0] | 1/4 |
| scale 50% @ +3R | −35.1 | [−60.4, −12.7] | 0/4 |
| scale 50% @ +2.5R | −39.8 | [−69.1, −15.5] | 1/4 |
| **scale 50% @ +2R** (pre-registered primary) | **−47.8** | [−78.2, −19.5] | 1/4 |
| scale 67% @ +2R | −63.8 | [−104.2, −26.0] | 1/4 |
| FULL @ +2R | **−95.6** | [−156.3, −39.0] | 1/4 |

**Every row loses, and every band excludes zero — this is a cost, not noise.** The
gradient is monotone in both axes: more taken and earlier taken is worse. That
monotonicity is what makes this a curve rather than a table to cherry-pick from.

**The timing null is the subtle part.** Selling the same half on a random session inside
the trade's own life, permuting the observed offsets: the real timing sits at the
**100th percentile** of 400 draws. Selling at +2R is the *best available moment to sell*.
The rule is well-timed; the act of selling is what costs. So "time it better" is not
available as a fix.

Primary verdict: **DOES NOT BEAT THE INCUMBENT.** Rank 4 of 14, fails meanR, fails
excess, 1/4 folds. Only the null and the band pass.

---

## 2. Entry fills: one shot at the close (`entry_fill_test.py`)

Ladder referenced to `close[i]` and `ATR[i]` — both known pre-open on i+1. Referencing
`close[i+1]` would be look-ahead.

| policy | fill | fill on winners | fill on losers | size-weighted excess | book total |
|---|---|---|---|---|---|
| **P0 one-shot at close** | 100% | 100% | 100% | **+2.03%** | **21.83** |
| P2 ladder3, chase unfilled | 100% | 100% | 100% | +1.64% | 17.72 |
| P4 ladder2 deep | 87% | 84% | 89% | +1.69% | 10.44 |
| P1 ladder3, abandon | 89% | **86%** | **92%** | +1.08% | 7.97 |

P1/P3/P4 sit at the **0th–2nd percentile** of the same-acceptance-rate null — worse than
random filters, the same verdict that killed the screener vetoes.

The decisive row is **P2**: it chases unfilled tranches, so it ends 100% filled with no
adverse selection and no capital shortfall — and still loses 39bp. So the loss is not
mainly about missing the runners. Buying lower also puts the stop lower, which gives
losers more room and holds them longer; the better fill is more than repaid.

**Ship: one shot, full size, at the close.** Primary statistic was size-weighted excess
throughout; mean R is a diagnostic, because a ladder that skips winners improves mean R
while shrinking the book.

---

## 3. Sizing: the sweep is a tie, and saying so is the result

`portfolio_sim.py` walks the book day by day — mark, exit, then admit greedily against
the real caps. One exact simplification: a candidate's exit path is size-independent, so
trades are simulated once and the walk decides only which and how big.

**The informativeness check fired.** CAGR spread across the whole grid **19.7pp** against
a mean single-path 90% band of **46.0pp**. The grid spread sits inside one path's own
noise. CAGR across risk levels runs +7.1 / +3.3 / −1.8 / −1.6 / +11.2 — non-monotone,
i.e. noise. **No configuration can be selected on return, and no CAGR from this file
should be quoted as an expectation.**

What *is* readable, because it is arithmetic rather than a return estimate:

| risk/trade | MDD (path) | MDD p5 (band) | avg open | avg gross | trades taken |
|---|---|---|---|---|---|
| 0.50% | −11.2% | **−17.9%** | 3.1 | 25% | 215 |
| 0.75% | −15.6% | −30.6% | 3.0 | 35% | 212 |
| 1.00% | −19.3% | −35.2% | 2.7 | 39% | 195 |
| 1.50% | −15.0% | −35.4% | 2.3 | 41% | 159 |
| 2.00% | −18.1% | −32.4% | 2.1 | 42% | 142 |

Three findings that are structural, not noise:

1. **Smaller positions → more names → materially smaller drawdown.** Monotone.
2. **`max_open = 5` is not the binding constraint.** At 1.5% risk the book averages
   **2.3** open names and hits the slot cap on 7% of days; raising 5→8 changes nothing
   (158 trades either way). `max_open = 3` binds 41% of days and costs real trades. The
   concentration preference is already delivered by the sizing; the name cap is slack.
3. **The caps help.** UNCONSTRAINED is worse on both return and drawdown (−3.0% CAGR,
   −18.7% MDD) than the capped book.

**Against a −15% tolerance, nothing on this grid qualifies on the p5 band.** 0.5%
risk/trade is the only setting close, at −17.9%. Stated plainly: at this book's
volatility, a −15% worst-case and genuine concentration are not simultaneously
available. That is a trade-off to choose, not a parameter to solve.

Caveat on every number here: **two years, one regime, IHSG −8.4% CAGR over the window.**

---

## 4. Running tally

| hypothesis | verdict |
|---|---|
| 1.5×ATR stop + E2 | **SHIPS** — for the tail; mean edge +0.64pp, CI straddles zero |
| hard R / ATR target | REFUTED — −96bp at +2R, band excludes zero |
| partial scale-out | REFUTED on merit — −48bp at 50%/+2R, rank 4 of 14, 1/4 folds |
| layered entry | REFUTED — every variant worse; abandoning ones below random |
| trailing stop, time stop, blow-off exit | previously refuted, unchanged |
| screener vetoes, intraday entry gates | previously refuted, unchanged |
| sizing by backtest | **INCONCLUSIVE BY MEASUREMENT** — grid inside path noise |

Nothing on the profit side has ever survived here. The prior against the next
"take some off the table" idea should be strong, and it should have to beat both the
incumbent *and* the timing-matched null before it is discussed as a rule.

---

## 5. Harness invariants (do not remove)

- **Panel fingerprint on every result JSON**, `--regress` refuses to diff across
  panels. The stored `trade_backtest.json` said n=915 while the same code on the current
  panel says n=1088 — nothing was wrong; they describe different data.
- **`--regress` compares floats by `repr()`, not `isclose`.** It guards a refactor, not
  a tolerance: a 1e-12 drift is the signature of reordered arithmetic, which is exactly
  what generalising the single-leg formula to `fraction = 1.0` produces. The two-leg work
  sits behind an early `if legs is None:` guard for this reason and reproduces v1a
  bit-identically.
- **Check 0 runs in the harness, not in prose.** Current window passes: pooled momentum
  lift **+1.06pp**, folds +1.06 / +0.57 / +1.43 / +0.57, all above the +0.5pp bar.
- **Block bootstrap, never a z-test.** `alpha_lib.block_ci` (circular moving-block).
  For paired comparisons bootstrap the per-trade *difference*, never two independent means.
- **Fold n is 172 / 542 / 504 / 349** — equal in time, not in events. "3 of 4 folds" is
  substantially "3 of 3 usable folds"; print per-fold n on every table.
- **Fees 0.0010/0.0010 from `secrets/.trade.env` = 0.20% round trip.** `RiskConfig`
  defaults are 0.0015/0.0025; a run without the env present measures a different market.
- `build_events` drops events whose forward horizons are unavailable, so candidates stop
  ~7 sessions before the panel end. "No signals lately" is partly a harness artefact.
