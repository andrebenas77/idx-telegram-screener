# The four cases — RSI heading crossed with volume, forward only

**Status: PRE-REGISTERED 2026-10-09, before the close of the first session of the sample.**
No return has been computed for this question on any data, old or new. Section 10 (result) is
appended after the deciding read and nothing above it is edited once the sample has begun.

Origin: the owner, 2026-10-08, reading `SQMI 81->58` in the morning block:

> "like SQMI the RSI at 81 but at what volume that RSi? compare to current 58, the volume will be
> much different. is there any way how to read this volume level on RSI. it will be so much
> different story if we look the RSI that way"

It was different: 850m shares a day in the week RSI read 81, 405m in the week it read 58, price 105
against 106. The label became `81->58 (vol x0.5)`, and the question became whether the four
combinations it can show lead anywhere.

Three choices were put to the owner on 2026-10-09 with the counts in section 4 in front of them,
and are fixed here as answered:

| Choice | Answer |
|---|---|
| Which evidence decides | **Forward only.** New sessions on the universe the report scores. The ten-year history stays unread. |
| Holding period | **Both 5 and 20 sessions.** |
| What is claimed | **Up/up is the case to act on; down/up is the case to avoid.** |

---

## 1. The claims

Among the names the 07:00 message lists under VOLUME HIGH:

> **Claim "act". UU, RSI higher than 20 sessions ago on more volume than that week, does better
> than the other three cases.**

> **Claim "avoid". DU, RSI lower than 20 sessions ago on more volume than that week, does worse
> than the other three cases.**

Each is judged at 5 sessions and at 20 sessions. **A claim passes only if both horizons pass.** One
horizon alone is recorded as PARTIAL and ships nothing.

## 2. Priors, both directions — declared before any return

**For "act".** Price strength with more trading behind it is the oldest reading of volume there is,
and the board's own evidence leans the same way: RVOL carries most of the momentum rule's lift, and
on 2026-10-08 price-leg days with brokers buying in size earned +1.70pp against +0.25pp without.

**Against "act".** RSI heading on its own was refuted on 2026-10-08 (`rsi-heading.md` section 9): the sign
reversed with the lookback. Every name in this population already traded a volume high TODAY, so
"more volume than a month ago" may add little to what selected it. And UU is half the population;
the half cannot be far from the whole.

**For "avoid".** The ten-year volume-ramp study (2026-10-06) found that heavy volume in a falling
stock trailed a random liquid stock after fees (reversal setup: mean -0.93%).

**Against "avoid".** Indonesia is the most reversal-prone of 21 emerging markets at short horizons;
a name with a collapsed RSI on heavy volume is where a five-session bounce comes from.

**Expectation: "avoid" is the likelier of the two to pass, "act" is more likely to fall short of
its size bar than to clear it, and at least one of the four readings will disagree with its twin
at the other horizon.**

## 3. Definitions — fixed now, not adjustable

**Population.** Every name written to `data/forward/volume_high_cases.jsonl` by the 07:00 job with
`list` equal to `band`, `below` or `hot`: volume on the session above 90% of the name's own last 50
sessions, mean traded value over the prior 20 sessions of Rp20bn or more, on the universe the daily
report scores that morning (board panel, the market's top-40-by-value list, the hand-kept extras).
The universe moves with the market; what it was on a given morning is what the ledger says.

**Case.** Two letters, from `build_daily_report.case_of`:

- first: `U` if the 14-session RSI is above its value 20 sessions earlier, else `D`;
- second: `U` if mean volume over the last 5 sessions is above mean volume over the 5 sessions
  ending 20 sessions earlier, else `D`.

Equal counts as `D` on both axes. A name with no RSI or no volume 20 sessions back has no case and is
outside the test; it is counted.

**Left out.** A name whose close halved or doubled in one session inside its 50-session lookback is
logged with `list = left_out` and is not in the population.

**Outcome.** Excess return over IHSG: in at the close of the session AFTER the one the name was
listed for (the close a reader of the 07:00 message can trade), out 5 and 20 sessions later. Stock
legs on Yahoo adjusted closes fetched at the read; index legs from `data/panel/benchmark-ihsg.csv`;
sessions from the board panel's calendar. A name whose raw close halves or doubles between its
listing and its exit is VOID at that horizon and listed by name. A missing price or index value
leaves the row without an outcome and is counted by reason.

**Unit.** One name on one morning. A name listed on consecutive mornings is consecutive rows; the
bootstrap resamples whole dates in 30-session blocks for that reason.

## 4. Counts taken on the unread history — planning only, no return computed

Whole market, Yahoo ten-year pull of 2026-10-06, sessions to 2026-10-05, volume-high days above the
Rp20bn floor (`research/idx-rsi-volume/occ_four_cases.py` on the laptop, outside this repo; it
computes counts and nothing else):

| Sample | Days | UU | UD | DU | DD |
|---|---|---|---|---|---|
| 2016-10 to 2024-08, every name | 15,042 | 7,683 | 917 | 5,333 | 1,109 |
| 2024-08 to 2026-10, names off the board panel | 1,594 | 989 | 59 | 435 | 111 |
| 2024-08 to 2026-10, board panel names | 4,566 | 2,353 | 257 | 1,618 | 338 |

About 12 volume-high names a session across the whole market in the last twelve months; fewer on
the report's narrower universe. UU is about half and DU about a third, so both claims compare a
large case with a large remainder. On names passing the board's PRICE leg the two volume-down cases
barely exist (230 and 130 of 8,930 days), which is why the population here is the volume-high list
and not the price leg.

**This history is deliberately NOT read for this question.** It is the only sample of any size that
has never been looked at for it. Computing one return on it for these four cases spends it.

## 5. The ledger

- `run_daily.sh` calls `build_daily_report.py --ideas --log-cases` at 07:00 and commits the file as
  "Volume-high cases <date>". The commit time is the proof that a label existed before the session
  it is judged on had traded.
- **First write wins.** A session already in the file is never rewritten.
- One marker line per session, so a morning with no volume-high name is a logged session.
- **Gaps are not backfilled.** A morning the job did not log is listed as a gap by `--status` and is
  out of the sample; a label written later would be written on restated prices. The morning
  message warns the same day.
- Only the 07:00 job writes. `--log-cases` is ignored with `--date`, `--backtest-render` or
  `--dry-run`.
- The first session that can enter is **2026-10-09**, logged by the run of 2026-10-12.

## 6. When it is read — fixed by a session count, not by what it shows

- `four_case_test.py --status` at any time: counts and dates, no return.
- **One interim look** when 240 logged sessions have a complete 20-session outcome (about
  November 2027). DESCRIPTIVE: under 15 blocks no band is inferential. It prints the table and
  cannot pass, fail or ship anything.
- **The deciding read** when 450 have (about September 2028): 15 thirty-session blocks, the floor
  this repo sets for an inferential band.
- Each is taken once. The script writes a file when it runs and refuses to run again; before the
  count is reached it refuses to compute a return at all.

The wait is the cost of the choice in the table at the top. It is not shortened by a shorter block,
a looser band or an early look.

## 7. Inference and pass bars — ALL must hold, at BOTH horizons, for a claim to pass

For each claim and each horizon, the statistic is the mean excess return of the claimed case minus
the mean of the other three, in percentage points.

1. **Size.** At least 0.5pp at 5 sessions and 1.0pp at 20, on the claimed side; and the 10/90
   date-block bootstrap band (30-session blocks, 2,000 draws) clear of zero on that side.
2. **Null.** Beyond the 95th percentile (5th, for "avoid") of a null in which each name's case
   labels are circularly shifted against its own outcomes, 200 draws. Not "the null is near zero":
   on 2026-10-08 that form failed a result for an offset that ran against it.
3. **Folds.** At least 3 of 4 equal calendar stretches on the claimed side.
4. **Blocks.** At least 15 thirty-session blocks in both arms.
5. **No single name.** The sign survives the removal of any one ticker.
6. **Check 0.** The validated momentum rule earns at least +0.5pp on the board panel inside the
   forward window. If it does not, the verdict on both claims is INCONCLUSIVE, neither pass nor
   fail: a hostile window cannot separate a bad rule from a bad period.

Printed beside them, as read-outs: the four cases' own means, medians and hit rates at each horizon,
and each case against every classified name.

## 8. Refutation — declared symmetrically

- A claim **fails** if neither horizon passes all six bars. A band that spans zero is "the case does
  not separate", not "almost".
- **PARTIAL** (one horizon only) ships nothing and licenses one follow-up at that horizon, on data
  logged after the read.
- A result on the OPPOSITE side with a clear band (UU trails, or DU beats) is recorded as that
  finding. It does not ship without its own pre-registration.
- No re-test at another lookback, another volume window, another cut point or another population.
- The interim look changes nothing: not the bars, not the read date, not the labels.

## 9. What ships, and what does not

- **Now, and until the deciding read:** the label stays as numbers, `81->58 (vol x0.5)`, with no
  word on it and no list reordered.
- **If "act" passes:** UU names are marked and listed first in the 07:00 block.
- **If "avoid" passes:** DU names are marked in the 07:00 block and the 07:30 report.
- **If a claim fails:** the label stays as numbers and this document records the refutation.
- Nothing here touches the momentum board, its gates or its ranking, whatever the result.

## Known limits, stated before the result

- About two years to the read. The market that is read may not be the market that prompted the
  question.
- The universe is the report's, which follows the market's most traded names; it is not a fixed
  list, and a name can leave it between its listing and its exit. Outcomes are fetched by ticker
  at the read, so a name delisted in between has no price and is counted as such.
- Yahoo restates history. Labels are frozen in the ledger; outcomes are whatever Yahoo's adjusted
  series says on the day of the read.
- Two claims at two horizons is four readings. Requiring both horizons for a pass is what keeps
  four chances from being four times the luck.
