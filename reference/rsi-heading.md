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

*(pasted from `heading_test.py --occupancy` on the production panel; see the commit that adds it)*

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
