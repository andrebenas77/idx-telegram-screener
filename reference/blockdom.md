# Block dominance and retail churn — who is buying, and does it matter?

**Status: PRE-REGISTERED 2026-08-26. No forward return computed at the time of writing.**
Thesis #10. Written before `scripts/block_dom.py` exists.

Two hypotheses from one variable family. H2 comes from `idx_quant_skill.md`'s third trigger leg
("freq low relative to volume ⇒ block trades, not retail clicks"). H3 comes from the user's own
framing, which the document does not contain: *"Indonesia is a market where market makers thrive and
we follow the signal when they trigger, where retail will follow and end up with wash trading."*

---

## 1. The hypotheses

> **H2 (block dominance).** Stock-days bought in LARGE tickets outperform stock-days bought in many
> small ones.

> **H3 (retail churn).** Stock-days where a large share of buying comes from HIGH-frequency,
> small-ticket participants underperform — high frequency per rupiah is the wash-trading state.

H3 is not the negation of H2. H2 asks whether the presence of block buyers helps; H3 asks whether the
presence of churn hurts. A day can have both, and on IDX it usually does.

## 2. Prior — this is the one leg where the thesis and this repo already agree

`accumulation.md` §7b, measured on real tape, after the repo had to **reverse its own prior**:

```
slice_z = ln(freq_share / value_share)
  accumulators NEGATIVE   TP -1.05, AI -1.07, IF -0.68     tickets ~Rp41m
  retail       POSITIVE   XL +1.04 / +0.85                 tickets ~Rp5-7m
                          25,337 buy prints on DSSA in one session
```

The original prior ("whales slice orders to inflate frequency") was backwards. High frequency
relative to value is the CROWD, not the whale. The whale takes size in large clips.

That is exactly the source thesis's claim, reached independently. Two routes to the same sign is the
strongest agreement anywhere in this comparison — which is why H2/H3 are worth testing even though
the family's track record is nine refutations.

**Counter-prior.** `lift-results.md` §8a: the footprint is real, persistent, and carries zero forward
edge. Being able to SEE the whale has never once implied being able to trade it here.

## 3. Definitions — fixed now

Source: `data/panel/gross-YYYY-MM.csv.gz`, per (date, symbol, broker):
`buy_value` (IDR), `buy_freq` (trade count), `buy_lot` (LOTS, 1 lot = 100 shares), `buy_avg`.
Coverage measured: **325,030 broker-days · 109 sessions 2026-02-18..2026-08-12 · 76 symbols ·
5,989 symbol-days.**

Per (date, symbol), over all brokers present:

- `tot_bval = Σ buy_value` · `tot_bfreq = Σ buy_freq`
- **`ticket = tot_bval / tot_bfreq`** — the average buy ticket in IDR
- **`ticket_rel = ticket / median(ticket over the symbol's previous <=20 gross sessions)`**
  (>=10 required). Stock-normalised, so it cannot degrade into a price-level or market-cap screen —
  the mistake `nr_lit` made in thesis #9.
- broker `slice_z_b = ln((buy_freq_b / tot_bfreq) / (buy_value_b / tot_bval))`, defined only where
  both shares are > 0
- **`churn_share = Σ buy_value_b over brokers with slice_z_b > 0, / tot_bval`** — the value share
  bought by participants spending more TRADES than their rupiah share implies. Identity-free.
- **`retail_share = Σ buy_value_b for b in {XL, XC, YP, KK, XA} / tot_bval`** — identity-based, from
  `reference/brokers.csv`'s behavioural taxonomy. Secondary, because it depends on a hand-made list.

**Scores and predicted signs, declared now.** Quintile 5 is always the end the hypothesis says is
BETTER, so every headline `Q5 - Q1` is predicted POSITIVE.

| hypothesis | score (higher = better, per the hypothesis) | Q5 is | predicted |
|---|---|---|---|
| **H2** | `+ticket_rel` | largest tickets | Q5-Q1 > 0 |
| **H3** | `-churn_share` | least churn | Q5-Q1 > 0 |
| H3b | `-retail_share` | least retail | Q5-Q1 > 0 |

**Outcome.** `Panel.excess_return(sym, i, k, entry_lag=1)`, excess over IHSG. Primary `k = 5`; 3 and
10 reported. Entry lag 1 is mandatory — flow publishes ~18:00 WIB.

**Population.** Every (date, symbol) in the gross panel with a computable `ticket_rel` and a
computable 5d excess. No volume or momentum filter: this is a cross-sectional sorter over the
universe the gross panel covers, and conditioning it on momentum candidates is FORBIDDEN — see §5.

## 4. Inference

Identical machinery to thesis #9: `lift_lib.date_block_bootstrap` (30-day blocks, 10/90 bands),
`blocks_with_treatment` printed beside every interval, feature-shift null (circularly shift each
symbol's feature series against its own returns, 200 draws), 4 equal CALENDAR folds,
`trade_backtest.check_zero` restricted to the gross window and run in the harness.

## 5. Power — the ceiling, stated before the numbers

**109 sessions is ~4 blocks against `MIN_BLOCKS_INFERENTIAL = 15`.** `lift_lib.is_inferential` will
label every result here DESCRIPTIVE and no amount of computation changes that. This is a property of
how much gross data has been bought, not of the analysis.

**Therefore: a PASS here cannot ship.** It can only justify paying to extend the gross panel
backwards via Sectors. A FAIL, however, is a real refutation — a variable that cannot sort returns
over 5,989 symbol-days is not going to start doing so with more of them.

**Conditioning on momentum candidates is forbidden.** That subset is n≈236 in this window, MDE
≈3.9pp against a base momentum effect of 1.37pp — the conditioner would have to be three times the
entire momentum edge to be detectable. Splitting 236 events in half produces a number that means
nothing, and this repo has already been bitten by reading exactly such a split.

## 6. Pass bar — declared in advance

For each of H2, H3 (H3b is a robustness read-out, not a gate):

1. `Q5 - Q1` at k=5 **>= +1.0pp**, with the 10/90 date-block band clear of zero.
2. Gradient **monotone in >= 4 of 5** quintile steps at k=5.
3. Feature-shift null within **+/- 0.3pp**.
4. **>= 3 of 4** calendar folds positive.
5. Check 0 passes in-window (known reference point: +0.84pp on 2026-02-18..08-12, n=314).

Clearing all five means "worth paying to extend", **not** "ship".

## 7. Refutation

- Flat or INVERTED gradient refutes the hypothesis. It is not a licence to use the loosest cut. Two
  inverted gradients are already on record here (one-sidedness, joint-lift age) and both times the
  loosest cell carried the whole number.
- Null within 0.3pp of the real result ⇒ indistinguishable from noise, whatever the point estimate.
- **If H2 and H3 disagree in sign, neither is read.** They are built from the same numerator and
  denominator; a sign disagreement means the variable is tracking something other than who is buying.
- If `churn_share` and `retail_share` disagree in sign, the identity-based list is doing the work and
  the result is about `brokers.csv`, not about the market.

## 8. What ships

**Nothing ships into the momentum board.** Hard user constraint: the board and every file it imports
are untouched, and `momentum_board.json` must diff byte-identical.

## 9. Result — **VERDICT: INCONCLUSIVE. The window is blocked.** (2026-08-26)

`scripts/block_dom.py` exits **4** at check 0 and computes no forward return. This is not a
failure of H2/H3; it is a refusal to read them. Cost: **0 API requests.**

The gross panel joined cleanly — 5,989 symbol-days → **4,903** with a computable `ticket_rel` and a
5d return, 93 sessions, 76 symbols. The variables are all constructible. Then:

```
check 0 (in-window): momentum lift -0.41pp on n=194  ->  FAIL   (bar +0.50pp)
```

### 9.1 It is not a near miss — every window we own data for fails

Measured across spans, under two matched-baseline conventions. **A is the repo-canonical one**
(`trade_backtest.check_zero`: baseline = every (symbol, day) in the universe of names that have
candidates *in that window*). B widens the baseline to all names with candidates anywhere.

| span | n | A (canonical) | B (all names) |
|---|---|---|---|
| FULL PANEL 2024-08..2026-08 | 1,564 | **+0.97pp PASS** | +0.97pp PASS |
| everything BEFORE 2026-05-18 | 1,507 | **+1.07pp PASS** | +1.07pp PASS |
| **gross window FULL** 2026-02-18..08-12 | 220 | **-0.98pp FAIL** | -0.01pp FAIL |
| gross window as H2/H3 trimmed it | 194 | **-0.34pp FAIL** | +0.57pp PASS |
| gross window first half | 163 | **-1.17pp FAIL** | +0.63pp PASS |
| gross window second half | 58 | **-2.16pp FAIL** | -1.82pp FAIL |
| **m5 intraday cache window** 2026-02-11..08-10 | 221 | **-0.98pp FAIL** | -0.03pp FAIL |

**Both halves of the gross window fail. So does the entire 5-minute intraday cache.** There is no
sub-period of the purchased microstructure data in which the known-good rule works.

### 9.2 What this actually means — and it is bigger than H2/H3

**Every microstructure dataset this repo owns was bought inside a period in which the only rule that
has ever survived a walk-forward does not work.** The gross broker panel (2026-02-18..08-12) and the
5-minute cache (2026-02-11..08-10) are the same six months, and it is the six months containing the
`-1.39pp since 2026-05-18` drawdown already on record in `accumulation.md`.

Consequences, in order of importance:

1. **H2 and H3 are INCONCLUSIVE, not refuted.** Recording them as refuted would repeat exactly the
   error that had to be withdrawn for one-sidedness — written up as REFUTED on -0.83pp over 59
   sessions, then found to be a hostile window. They keep their pre-registered bar and are re-run
   unchanged if the window ever becomes readable.
2. **The binding constraint on the whole VSA programme is NOT the missing `freq` field.** It is that
   all microstructure history sits in one hostile six-month window. Buying more `freq` at higher
   resolution inside the same window buys nothing readable.
3. **The highest-value spend is therefore to extend the gross panel BACKWARD** into 2024-08..2026-02,
   where check 0 passes at +1.07pp — not to buy more of 2026-02..08. This inverts the priority in the
   original plan, which had the backward extension as a contingent follow-up.
4. The forward 1-minute capture keeps its value but its payoff is further out than stated: it needs
   to accumulate across a regime change, not merely across months.

### 9.3 Methodological note — check 0 is sensitive to the universe convention

The same window reads **-0.98pp** under convention A and **-0.01pp** under B, and the trimmed window
flips sign outright (-0.34 vs +0.57). Cause: the in-window candidate universe collapses to **59
names** from 147, which materially changes the matched baseline. Convention A is canonical here and
is also the conservative one.

This is worth carrying forward: **a check-0 result should be quoted with its universe convention, and
a window whose verdict flips with that choice is a window to distrust regardless of which side it
lands on.** The earlier +0.84pp recorded for this window in `accumulation.md` was computed over that
study's own 75-symbol panel — a third convention again, which is why it disagrees with both columns
above.

**Nothing ships. No forward return was computed. The momentum board is unchanged.**
