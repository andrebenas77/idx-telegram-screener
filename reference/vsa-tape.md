# H4 — is the iceberg visible? A measurement study on the tape

**Status: PRE-REGISTERED 2026-08-26. No statistic computed at the time of writing.**
Thesis #11. Written before `scripts/tape_vsa.py` exists.

**This is a MEASUREMENT study, not a prediction study.** It computes no forward return, so check 0
does not gate it — and that matters, because check 0 has just blocked every forward-return study on
the purchased microstructure data (`blockdom.md` §9). H4 is the one question this data can still
answer.

---

## 1. The hypothesis

`idx_quant_skill.md` asserts a mechanism: on a high-volume, narrow-range candle, a whale is sitting
on the bid with an iceberg, passively absorbing retail supply in large clips.

> **H4. On bars satisfying the deseasonalised, tick-scaled trigger, the buy side is DOMINATED by one
> broker, that broker is PASSIVE (its bid is being hit, not lifting the offer), and it trades in
> LARGE clips (`slice_z < 0`).**

Three separately falsifiable claims. The thesis needs all three; each is measured separately.

## 2. Why this is worth measuring even though the family has failed

Nine flow theses are refuted. But every one of them tested PREDICTION. `lift-results.md` §8a is the
key precedent: the footprint was found to be real, persistent, and worthless for forecasting.

So "is the iceberg visible?" and "does the iceberg pay?" are different questions and only the second
has been answered. H4 answers the first, cheaply, and its only job is to decide whether the paid
bar-level fetch is worth buying. This is the `vpin.md` V0 pattern: validate the measurement before
spending on the hypothesis.

## 3. Data and its limits — stated before any number

29 full second-resolution tapes in `data/tape/*/*.json.gz`, ~250k prints, 20 symbols, 2026-02..08.
Fields per print: `time` (second resolution), `price`, `volume` (SHARES), `buyer`, `seller`,
`buyer_dom`, `seller_dom`, `type` (the true aggressor flag), `board`. Broker codes are unmasked
because these are closed sessions.

**The sample is SELECTED and this invalidates any edge claim from it.** These tapes were pulled for
BREN/PTRO surge case studies, not sampled at random. Every line of output is labelled descriptive.
A mechanism can be demonstrated on a selected sample; a payoff cannot.

**Two mandatory guards, both from prior silent failures:**

1. **Truncation.** `running_trade_all()` breaks its page loop on any falsy payload and caches the
   partial tape, where it is indistinguishable from a complete session forever after. Three such
   tapes once scored maximally toxic (TR 0.975/0.780/0.794 vs a clean median of 0.398) and dragged a
   correlation from +0.559 to +0.210. Guard: print count against the gross panel's
   `Σ buy_freq` for that (symbol, date), using `vpin_validate.truncated()`'s 0.5x rule.
2. **Corporate actions.** RAJA 2026-02-27 sits on opposite sides of a 1:5 split in the tape and m5
   stores; an unguarded join books a -80% error. Guard: session VWAP from the tape against the m5
   store's proxy VWAP for the same (symbol, date). Median disagreement across these tapes is
   **3.7bp** and p90 is 53bp, so a session outside +/-20% is a store mismatch, not a market move.

## 4. Definitions — fixed now

**Bars.** Prints floored to 5-minute buckets by START time. Continuous session only:
`09:00 <= hhmm < 16:00`. The pre-open auction (08:55) is excluded because a single uncontested print
anchors everything; the close auction is excluded because it is a matching-engine artifact — and
note the source thesis's stated window (15:50-16:15) does not exist in this feed, where MOC lands
entirely in the 16:00 bucket.

Per bar, computed EXACTLY from prints (not proxied): `volume`, `value = Σ price x volume`,
`freq = print count`, and O/H/L/C.

**Bucket alignment is verified, not assumed.** Tape-derived bar volumes are compared against the m5
store's bars for the same (symbol, date); the agreement rate is reported. A convention mismatch
(bar labelled by start vs close) would show up as a systematic one-bucket offset.

**Deseasonalised relative volume.** `rvol_c = bar_volume / median(volume in the SAME hhmm bucket over
the symbol's previous 20 sessions)`, from the m5 cache. Same-bucket is mandatory: IDX intraday volume
is U-shaped, so the thesis's literal denominator ("average intraday candle volume") makes the open
and close buckets clear 3.0 on a perfectly average day by construction. The thesis patches the close
artifact and not the open one, then names the open artifact as a separate phenomenon ("Morning
Flush").

**Narrowness.** Tick-scaled: `(H-L) / tick_size(C) <= 3`. The literal `(H-L)/C < 0.005` is computed
alongside for comparison only — it fires on 48.5% of all cached 5m bars and, because IDX ticks are
price-banded, it is unsatisfiable at Rp500-2000 for anything but a zero-range bar.

**Trigger bar.** `rvol_c > 3.0` AND tick-narrow AND `freq >= 10`. The frequency floor exists because
a dominance share and a `slice_z` computed over three prints are not measurements.

**The three measured quantities**, on the BUY side (the thesis's claim is about the bid):

- `dom_share` = largest single broker's share of the bar's buy VALUE
- `passive_share` = of that broker's buy value, the fraction whose aggressor flag is `SELL` — i.e.
  someone hit its resting bid. `aggressor == BUY` would mean it lifted the offer.
- `slice_z` = `ln((its buy print share) / (its buy value share))`; negative = large clips

**Controls — a bare percentage is not a measurement.** Two, from the SAME sessions:

- **C1 — high-volume WIDE bars** (`rvol_c > 3`, not tick-narrow). Isolates the NARROWNESS leg: if
  trigger bars and C1 look alike, narrowness is contributing nothing and the trigger is just a volume
  filter.
- **C2 — ordinary bars** (`rvol_c <= 1.5`). The unconditional shape of the tape.

## 5. Pass bar — declared in advance

1. **>= 60%** of trigger bars satisfy all three jointly: `dom_share > 0.40` AND
   `passive_share > 0.60` AND `slice_z < 0`.
2. The trigger rate on that joint condition exceeds **C1 by >= 15 percentage points**. This is the
   load-bearing one: it is what separates "the iceberg is real" from "high volume looks like this".
3. Each leg individually is higher on trigger bars than on C2.
4. Bucket alignment against the m5 store >= 90%, and >= 20 tapes survive both guards.

## 6. Refutation

- Below 60%, or within 15pp of C1, the mechanism is not visible even with perfect data — true
  aggressor flags, unmasked brokers, second resolution. **The paid bar-level fetch is then cancelled**,
  because no cheaper instrument could show what this one cannot.
- If `dom_share` passes but `passive_share` fails, the bars are SWEEPS, not absorption — one broker
  crossing the spread aggressively. That is a specific and interesting refutation: it would mean the
  thesis has the participant right and the direction backwards, and it is consistent with the sweeps
  already on record here (437-print one-second buy sweeps by PD, 331 by AK on a markup day).
- If the literal narrowness definition and the tick-scaled one select materially different bars, the
  thesis's own threshold is measuring the tick ladder. Reported either way.

## 7. What ships

Nothing, in any outcome. This study decides only whether to spend requests. The momentum board is
untouched and `momentum_board.json` must diff byte-identical.

## 8. Result — **VERDICT: FAIL. The mechanism is visible and it is the OPPOSITE one.** (2026-08-26)

`scripts/tape_vsa.py`; payload in `data/panel/tape_vsa.json`. **0 API requests.**
25 of 29 tapes survived both guards; 185 trigger bars, 58 wide controls, 1,029 ordinary controls.

```
population                              n   joint%    dom%   pass%    blk%
TRIGGER  rvol_c>3 & tick-narrow       185    13.5%   54.6%   36.8%   93.0%
C1       rvol_c>3 & WIDE               58     1.7%   41.4%   22.4%   87.9%
C2       rvol_c<=1.5 ordinary        1029    22.9%   60.5%   47.2%   84.6%

medians    TRIGGER  dom 0.421  passive 0.288  slice_z -0.459
           C1       dom 0.354  passive 0.148  slice_z -0.525
           C2       dom 0.446  passive 0.516  slice_z -0.416
```

| # | check | result | |
|---|---|---|---|
| 1 | joint >= 60% of trigger bars | **13.5%** | FAIL |
| 2 | exceeds C1 by >= 15pp | **+11.8pp** | FAIL |
| 3 | every leg above C2 | **every leg BELOW C2** | FAIL |
| 4 | alignment >= 90%, >= 20 tapes | 98.4%, 25 tapes | PASS |

### 8.1 The refutation is specific: these are SWEEPS, not absorption

§6 pre-declared the diagnostic — "if `dom_share` passes but `passive_share` fails, the bars are
SWEEPS, not absorption ... the thesis has the participant right and the direction backwards." **That
is exactly what fired.**

The dominant buyer on a high-volume narrow-range bar is **passive 28.8% of the time** (median share).
On an ORDINARY bar it is **passive 51.6%**. So the trigger does not select absorption — it selects
bars where the dominant buyer is **crossing the spread**, and it selects them *worse than random*.

The mechanism reading is straightforward once measured: **a bar reaches 3x its seasonal volume
because somebody is lifting offers.** Passive resting size does not create volume; aggression does.
The thesis's premise — that heavy volume with a still price is a whale absorbing on the bid — has the
causality inverted at bar resolution. Quiet bars are where passive fills happen, and quiet bars are
by definition not high-volume ones.

### 8.2 The trigger is ANTI-selective for its own mechanism

The joint condition fires on **13.5%** of trigger bars against **22.9%** of ordinary bars. Every
individual leg is also lower on trigger bars than on ordinary ones (dominance 54.6 vs 60.5, passivity
36.8 vs 47.2). It does beat the WIDE control by +11.8pp, so narrowness is doing *something* relative
to width — but both sit far below the unconditional rate, so what narrowness recovers is a fraction
of what high volume destroyed.

This is the `lift.md` §3d lesson arriving again from a new direction: C1 was the natural-looking
comparison and, read alone, "trigger beats wide bars by +11.8pp" would have looked like support. Only
the unconditional control C2 shows the sign is wrong. **Compute the unconditional baseline first.**

### 8.3 The `slice_z` leg was a VOID TEST — it could never have failed

`slice_z < 0` fires on 93.0% / 87.9% / 84.6% across the three populations — near-constant, and it
looked like the thesis's one corroborated leg confirming itself. It is an artifact of the estimator.

Measured directly on 704 bars from 12 tapes:

```
DOMINANT-by-value broker : slice_z < 0 on 89.5%   median -0.501
RANDOM broker, same bars : slice_z < 0 on 29.7%   median +0.647
```

Selecting the largest broker BY VALUE mechanically selects a high value share, which forces
`ln(freq_share / value_share)` negative. The statistic's shape was **guaranteed before any data were
seen** — precisely the objection `lift.md` §3e raises, and it retired a planned kill-switch there for
the same reason.

**Correction to the pass bar, for any future run:** the block leg must be *"is the dominant broker's
`slice_z` more negative than a same-bar random broker's?"*, never *"is it negative?"*. As written,
pass-bar leg 3 was unfalsifiable and contributed nothing. The other two legs are unaffected — and
they are the ones that failed, so the verdict stands.

**This does NOT overturn the broker-day finding** in `accumulation.md` §7b (accumulators -1.05,
retail +1.04). That compares *named brokers to each other* across a whole session, which is a
different estimator with no selection-on-value step. What dies is the bar-level version of the test.

### 8.4 The two narrowness definitions are not interchangeable

```
both 636 bars · tick-narrow only 748 · literal-narrow only 0
```

At 5-minute resolution on these 20 mostly-liquid names, `(H-L)/C < 0.005` is a **strict subset** of
`(H-L)/tick_size(C) <= 3` — it never fires alone. Contrast the daily-panel measurement, where the
literal form fires on 48.5% of all 775,583 cached bars across 112 symbols including cheap ones. Both
observations have the same cause: the literal form tracks the price band. Which one is "narrow"
depends entirely on what a tick is worth at that price, which is the confound `effort.md` §3 named
before any of this was run.

### 8.5 The guards both earned their place

- **RAJA 2026-02-27 dropped**, m5/tape session VWAP ratio **0.200** — exactly 1/5, the unhandled 1:5
  split. Caught by an empirical store-consistency check rather than by parsing the corporate-actions
  file, which is the more robust construction because it tests the join that is actually being made.
- Three February tapes (INCO 02-18, SSIA 02-20, PGAS 02-23) dropped for having only 3-6 prior m5
  sessions — the seasonal baseline needs history the cache does not have at its own start date.
- No tape was truncated: the truncation guard found none, so the three known-bad sessions are not in
  this set.
- **Bucket alignment 98.4%** across 1,552 shared buckets. The tape-derived bars reconcile with the m5
  store, so the 5-minute convention is verified rather than assumed.

### 8.6 Consequence

**The paid bar-level fetch is CANCELLED**, per §6. The mechanism is not visible with true aggressor
flags, unmasked broker codes and second-resolution timestamps — the best microstructure instrument
available. `intraday-inventory-chart` at 5-minute buckets with no aggressor flag could not show what
this could not. That is ~200-670 requests not spent.

The iceberg-on-the-bid hypothesis is refuted **as a bar-level trigger**. It is not refuted as a
description of what accumulators do across a whole session, where this repo has already measured
it directly (TP passive at 3,310, hit 1,332 times, `cost_gap` -0.01% vs VWAP). Those two facts sit
together comfortably: **the whale is passive over a day, but the bars that light up a VSA screen are
made by whoever is aggressive in that five minutes.**
