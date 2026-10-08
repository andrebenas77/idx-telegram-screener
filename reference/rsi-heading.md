# RSI heading, and whether the broker leg earns its place

**Status: PRE-REGISTERED 2026-10-08. No forward return has been computed at the time of writing.**
Framework written before `scripts/heading_test.py` computed a return, per the convention in
`effort.md` and `accumulation.md` §6.6. Section 4 (occupancy) is filled from a counts-only run;
section 9 (result) is appended afterwards and nothing above it is edited once it exists.

Origin: two statements by the owner on 2026-10-08, after SQMI was found to have been outside the
universe:

> "on the broker leg, i think we better not thinking too much about who is the broker, i think
> market maker can always play hide and seek with the broker. i think it is important to see the RSI
> heading, is it heading north or south. if its ahead to 55+ from lower number, then it could be a
> good sign as long the rvol and momentum is there."

and, on how far back to look: "since indonesia market is a little bit slow, i think lookback should
further than 5 session, probably like one month swing (20 session perhaps?)".

---

## 1. The hypotheses

> **H-B. Among stock-days that pass the board's PRICE leg, the ones whose RSI came up to 55+ from
> below it over the last 20 sessions do better over the next 5 sessions than the ones whose RSI was
> already at 55+ twenty sessions ago.**

> **H-A. The broker leg adds to the price leg: price-leg days on which at least one broker passes
> the board's accumulation rule do better than price-leg days on which none does.**

H-C is a read-out, not a hypothesis: how concentrated the day's net buying was in its five largest
buyers, a measure that splitting an order across brokers cannot hide from.

Two primary tests. Everything else the script prints is a read-out and is labelled so in its output.

## 2. Priors, both directions — declared before running

**H-B, for.** The gate reads a level. RSI 58 on the way up from 44 and RSI 58 on the way down from
80 are the same row to it, which is the flaw the daily report already fixed for RVOL with its
direction label (KIJA, 2026-08-27: in the band at 2.57 having spent three sessions above 3.0).

**H-B, against.** (i) On IDX the move up often happens in one limit-up session: SQMI's RSI went from
51 to 79 on 2026-09-01 on a +35% day, so "came up through 55" can mean "already ran", and the
Newtonian-momentum work found Indonesia the most reversal-prone of 21 emerging markets. (ii) The
board's RSI is a simple 14-session average of gains over losses, so its change is close to the
recent return and may carry nothing the RVOL band and the 60-day-high gate do not. (iii) Every idea
here that bought BEFORE confirmation has failed: quiet accumulation, dip timing, absorption at
support.

**Stated to the owner before the run:** a move up through 55 against already being above it is "a
coin flip"; among names already above, RSI lower than it was is likely to underperform RSI higher
than it was; "approaching 55, not there yet" is expected to fail.

**H-A, for.** The audit's post-hoc cut: stock-days with 3+ qualifying brokers earned +3.29pp against
+1.45pp with one. **H-A, against.** RVOL carries +1.15pp of the rule's +1.28pp measured lift
(`build_daily_report.py` docstring), the 50-day volume high passed nine checks on price and volume
alone, and broker skill survived only at +2-3%. **Expectation: the broker leg adds something, but
less than the +0.5pp bar, so H-A FAILS.**

## 3. Definitions — fixed now, not adjustable after seeing results

All gate constants are read from `build_momentum_board.py` at run time, never copied.

**Price leg, P.** `rvol5` in `[1.5, 3.0)`, `rsi >= 55`, `dd60 >= -0.10`, on adjusted closes and raw
volume, exactly as `overlay_test.features()` computes them (the script's lean copy is held to it by
a 300-sample parity check that refuses to run on any mismatch).

**Broker leg, A.** At least one broker with net value `>= Rp500m` and `>= 10%` of the name's 20-day
ADTV on the day, net-positive on at least 2 of the last 3 sessions. The board's rule; who the broker
is plays no part.

**Liquidity floor.** Median daily traded value over the PRIOR 20 sessions `>= Rp5bn`, signal day
excluded. Applied to every arm of every test, because the price leg has no protection of its own
(GEMS passed it on Rp481m). The board without the floor is printed once for reference.

**RSI then.** The same RSI, 20 sessions earlier: `then = rsi(i - 20)`. Missing when the name lacks
60 unbroken sessions ending there; such days are left out of H-B and counted.

**Fresh / already above.** `fresh = then < 55`, `already above = then >= 55`. Today's RSI is `>= 55`
in both, by P.

**Outcome.** Excess return over IHSG, entry at the close of day `i+1`, exit `k` sessions later
(`Panel.excess_return`, `entry_lag=1`). **Primary `k = 5`**, the horizon check 0 measures the board
at. `k = 10` and `k = 20` are printed as read-outs.

**Unit.** One stock-day. Consecutive days of one name are not independent; the bootstrap below
resamples whole dates in 30-session blocks for that reason.

## 4. Measured occupancy — counts only, BEFORE any return

`heading_test.py --occupancy` on the production panel, 2026-10-08 14:16 WIB: 117 symbols, 517
sessions (2024-08-07 to 2026-10-07), 513 benchmark days. Parity with `features()`: 0 mismatches on
300 sampled stock-days. No return was computed on this path.

| Cell | Stock-days |
|---|---|
| With features | 50,770 |
| Above the Rp5bn floor | 40,297 (112 names) |
| Price leg passes, P | 1,808 (4.5% of floored days; 376 dates, 101 names) |
| H-A: P with a qualifying broker / without | 1,235 / 573 (15 blocks each) |
| H-B, lookback 20: fresh / already above / no RSI then | 987 / 772 / 49 (15 blocks each) |
| H-B, lookback 10 | 891 / 892 (15 blocks each) |
| H-B, lookback 40 | 1,072 / 620 (14 blocks each: under 15, a sign check only) |
| "Approaching" read-out cell | 22 |

Brokers qualifying on a P day: none 573, one 537, two 384, three or more 314. The board without the
floor is 1,550 stock-days.

H-B by today's RSI (fresh / already above): 55-65 238 / 212; 65-75 344 / 248; 75+ 405 / 312. Fresh is
the larger arm in every band, so the level-matched contrast has both arms in every cell.

H-B by calendar fold (fresh / already above): 101 / 67; 432 / 325; 336 / 219; 118 / 161. Every fold
clears the 20-per-arm minimum. Folds 2 and 3 hold 75% of the days.

Within "already above": RSI higher than twenty sessions ago 529, lower or equal 243.

Top-5 net buying as a share of the day's value on P days: p10 0.10, p25 0.15, median 0.23, p75 0.35,
p90 0.49.

**Read before the run:** both primary tests sit exactly on the 15-block line for an inferential
band, and the "approaching" cell (22 days) is too thin to say anything about; it will be printed and
not interpreted.

## 5. Inference

- **Baselines first:** every floored stock-day, then P, then P against the rest.
- **Contrast:** mean of arm 1 minus mean of arm 0, in percentage points.
- **Band:** moving-block bootstrap over whole panel dates, 30-session blocks, 2,000 draws, 10th/90th
  percentiles (`lift_lib.date_block_bootstrap`). Below 15 blocks in either arm the band is
  descriptive.
- **Null:** each symbol's arm labels are circularly shifted against its own returns, 200 draws. A
  null that reproduces the real number means the harness leaks.
- **Folds:** four equal calendar stretches; a fold with under 20 stock-days in either arm reports
  n/a.
- **Level-matched contrast (H-B):** the same difference inside three bands of today's RSI
  (55-65, 65-75, 75+), weighted by the fresh count. A name that came up through 55 sits lower in the
  range today than one that has been above it for a month; this holds the level and leaves the
  heading. Both the raw and the matched figure are printed.
- **Lookback sign check (H-B):** the contrast is recomputed at 10 and at 40 sessions.
- **Balance table (H-B):** mean RSI, RVOL5, DD60, median value and broker-leg share for each arm.
- **Check 0 first.** If the validated rule does not earn +0.5pp in this window, the run stops.

## 6. Pass bars — declared in advance, ALL must hold

**H-B (fresh minus already above, k = 5, lookback 20):**

1. difference `>= +0.5pp`, and the 10/90 band clear of zero;
2. the level-matched contrast has the same sign;
3. the same sign at lookbacks 10 and 40;
4. shift null within `+/-0.3pp`;
5. at least 3 of 4 calendar folds positive;
6. check 0 passes;
7. at least 15 blocks in both arms.

**H-A (P with a qualifying broker minus P without, k = 5):** bars 1, 4, 5, 6 and 7 above.

## 7. Refutation — declared symmetrically

- H-B **fails** if any bar fails. A difference whose band spans zero is "heading does not separate
  the candidates", not "almost". A NEGATIVE difference with a band clear of zero is the opposite
  claim (already-above is better) and is recorded as such; it does not ship either without its own
  pre-registration.
- H-A **fails** if any bar fails, and the reading is then "no evidence in this panel that the broker
  leg adds 0.5pp over the price leg", which is the owner's position. It is not "the broker leg is
  worthless": the board was validated as both legs together and that result stands.
- The 10- and 40-session readings are sign checks. Choosing the best of the three lookbacks after
  the fact is not available; 20 was named before the run.
- No re-test at a looser bar, a different RSI threshold or a different horizon. One-sidedness
  (+0.70, +0.16, -0.39) and joint-lift age died exactly that way.

## 8. What ships, and what does not

- **Already shipped, display only:** the daily report and the 07:00 block print RSI as
  `then->now` (for example `44->58`). No word is attached to it and no list is filtered by it.
- **If H-B passes:** fresh names are marked and listed first in the 07:00 block and the 07:30
  report. The gate itself does not change without a further decision by the owner and a forward
  period.
- **If H-B fails:** the two numbers stay as a neutral label; this document records the refutation.
- **If H-A fails:** nothing on the board changes by itself. It licenses one follow-up, separately
  pre-registered: a price-only board, tested against the two-leg board on the same bars.
- **If H-A passes:** the broker leg stays, and "do not think about who the broker is" stands as a
  statement about identity only, which the board already honours.

## Known limits, stated before the result

- Same panel that validated the board, so P's own definition is in-sample; only the two contrasts
  are new.
- The universe is today's list (112 hand-kept names plus six added 2026-10-08), so history before
  mid-2026 is selected on later liquidity.
- About 17 thirty-session blocks. Two years is one market regime and a half.

---

## 9. Result — **H-B: FAIL. H-A: FAIL on the null bar, with every other bar passed.** (2026-10-08)

`heading_test.py` on the production panel, 2026-10-08 14:17-14:26 WIB, panel and benchmark to
2026-10-07, 2,000 bootstrap draws, 200 null draws. Check 0: **+1.26pp** on n=1,491, PASS.
Output in `data/panel/heading_test.json`. Nothing above this line was edited after the run.

### 9.1 Baselines

| Population (floored, k=5) | n | Mean excess | Hit |
|---|---|---|---|
| Every stock-day | 39,052 | +0.31pp | |
| Price leg passes, P | 1,761 | +1.24pp | 48.4% |
| P minus every other stock-day | | **+0.97pp**, band [+0.42, +1.50] | |

Folds for P minus the rest: +1.93, +1.02, +0.79, **+0.09**. The lift of the price leg shrinks through
the panel and is about nothing in the last fold (2026-04 to 2026-09).

### 9.2 H-A — the broker leg

| Arm (P, floored, k=5) | n | Mean | Median | Hit |
|---|---|---|---|---|
| With a qualifying broker | 1,201 | +1.70pp | +0.12pp | 50.5% |
| Without | 560 | +0.25pp | -1.04pp | 43.8% |
| Difference | | **+1.45pp**, band [+0.57, +2.43] | | |

Folds +1.51, +2.31, -0.12, +2.82. Read-outs: k=10 +1.57pp [+0.68, +2.42]; k=20 +1.99pp
[+0.70, +3.27].

| Bar | Result |
|---|---|
| 1 difference >= +0.5pp, band clear of zero | PASS |
| 4 shift null within +/-0.3pp | **FAIL**: null mean -0.37pp (sd 0.51, p05..p95 -1.15 to +0.49) |
| 5 folds positive >= 3/4 | PASS (3 of 4) |
| 6 check 0 | PASS |
| 7 blocks >= 15 in both arms | PASS (15 / 15) |

**Verdict under the declared bars: FAIL.** One bar failed and the bars are not adjustable.

**But the reading of a failure that section 7 wrote in advance does not describe this one, and it
would be false to apply it.** That sentence ("no evidence that the broker leg adds 0.5pp") was
written expecting a failure on SIZE. The size bar passed by a wide margin, and the expectation
stated in section 2 ("less than the +0.5pp bar") was wrong. What failed is the null, and it is
offset in the direction OPPOSITE to the effect: shifting the labels of each name against its own
returns gives -0.37pp, so the names that qualify often are, on average, slightly worse names, and
the within-name timing effect is larger than the headline, not smaller. The real +1.45pp sits beyond
the 95th percentile of the null (+0.49pp). The bar was designed to catch a harness that manufactures
the result; here it caught a composition offset that works against it. That is an observation made
after seeing the numbers and it does not change the verdict.

What the numbers say, verdict aside: **a price-leg day WITHOUT a qualifying broker earned +0.25pp,
which is what any stock-day earned (+0.31pp).** The lift on price-leg days is on the days someone
bought in size.

Read-out, by how many brokers qualify on a P day: none +0.25pp (560), one +1.11pp (518), two
+1.39pp (376), **three or more +3.08pp** (307). Monotone, and the same shape the August audit found
(+1.45 / +1.81 / +3.29). Same panel for the most part, so this is a re-read, not a replication.

### 9.3 H-B — RSI heading

| Arm (P, floored, k=5, lookback 20) | n | Mean | Median | Hit |
|---|---|---|---|---|
| Came up through 55 (fresh) | 951 | +1.73pp | +0.18pp | 51.2% |
| Already above | 763 | +0.80pp | -0.52pp | 45.1% |
| Difference | | **+0.93pp**, band [+0.13, +1.67] | | |

| Bar | Result |
|---|---|
| 1 difference >= +0.5pp, band clear of zero | PASS |
| 2 level-matched contrast same sign | PASS (+0.91pp; 55-65 +0.65, 65-75 +1.78, 75+ +0.33) |
| 3 same sign at lookbacks 10 and 40 | **FAIL**: 10 sessions **-1.37pp** [-2.00, -0.81], 4 of 4 folds negative; 40 sessions -0.96pp [-2.32, +0.28] |
| 4 shift null within +/-0.3pp | PASS (+0.07pp) |
| 5 folds positive >= 3/4 | **FAIL**: -0.57, +0.59, +2.48, -0.23 |
| 6 check 0 | PASS |
| 7 blocks >= 15 in both arms | PASS (15 / 15) |

**Verdict: FAIL. H-B is refuted under its own bars.** The +0.93pp at twenty sessions is one fold
(+2.48pp in 2025-10 to 2026-04), and the sign does not survive a change of lookback: a name that
came up through 55 within the last TEN sessions did 1.37pp WORSE than one already above it, with a
band clear of zero and all four folds negative. "Heading north" is not one thing. Twenty sessions
was named before the run; ten and forty were sign checks, and they disagree with it.

Balance, fresh / already above: RSI today 73.1 / 72.4, RVOL5 1.91 / 1.88, DD60 -3% / -2%, median
value Rp26bn / Rp34bn, broker-leg share 69% / 67%. The arms are alike on what was measured.

### 9.4 Read-outs — every one is after the fact, none is a result

- **Longer holding, lookback 20:** k=10 +1.37pp [+0.06, +2.65]; **k=20 +3.32pp [+0.92, +5.66]**,
  folds +7.66, -0.66, +3.91, +4.53. This is the one figure that fits the "one month swing" the owner
  described (a monthly lookback held for a month). It is a non-primary horizon read after the
  primary failed, one of six read-outs printed, and the 10- and 40-session sign checks were not run
  at k=20. It is a question for its own pre-registration, not a finding.
- **Change in RSI over 20 sessions, quintiles (fell most to rose most):** +0.26, +0.67, +0.65,
  +2.28, +2.70pp.
- **Within "already above", RSI higher than then against lower or equal:** +1.03pp [-0.10, +2.44],
  folds -1.89, +2.21, +0.91, +1.38. The stated expectation (falling underperforms) has the right
  sign and a band that touches zero.
- **Inside the board as it is (P with a broker), fresh against already above:** +0.58pp
  [-0.58, +1.70]. Heading does not separate the candidates the board already lists.
- **"Approaching" cell:** +2.78pp on 21 stock-days. Declared too thin before the run; not read.
- **H-C, top-5 net buying as a share of the day's value, quintiles:** +1.19, +2.03, +1.32, +1.72,
  **-0.04pp**. Not monotone, and the most concentrated fifth is the worst. Concentration in a few
  buyers is not the identity-free measure; the COUNT of brokers buying in size (9.2) is.

### 9.5 What changes

- **Nothing on the board.** Gates, ranking and lists are as they were.
- The `then->now` label stays as two numbers with no word on it. Fresh names are not marked or
  listed first (section 8, "if H-B fails").
- The broker leg stays. The point that WHO the broker is does not matter is consistent with
  everything here; the idea that the leg itself can be thought about less is not.

### 9.6 What is NOT claimed, and the follow-ups that would each need their own pre-registration

- Not claimed: that the broker leg "passed". It failed a declared bar.
- Not claimed: that a month-long RSI swing pays over a month. One read-out says so.
- Follow-up 1: breadth as a ranking input. Three or more qualifying brokers against one, as a
  primary test, on a forward period; the two-year panel has now been read for this twice.
- Follow-up 2: lookback 20 with a 20-session horizon as its own primary, with the lookback sign
  checks run at that horizon.
- Follow-up 3: the lift of the price leg by fold (+1.93, +1.02, +0.79, +0.09). If the last fold is
  the regime, the board is leaning on the broker leg more than its history suggests.
