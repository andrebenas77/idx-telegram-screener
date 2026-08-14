# The accumulation framework

What the third board measures, why each rule exists, what would prove it wrong, and what has
already been refuted. Written **before** the code, so the pass bar cannot be moved after seeing
the numbers. Same posture as `scoring.md` and `invezgo.md`.

Companion to:
- `scoring.md` — the crowded board (attention)
- `momentum_setup.py` docstring — the momentum board (price)

---

## 1. The hypothesis

> A whale accumulating a liquid IDX name leaves a footprint in **executed broker flow** several
> sessions before price moves. The footprint is not net size — it is the **absence of a sell
> side**, sustained, in a name whose price is going nowhere.

The motivating case, 2026-08-12, when BREN (+13.3%), CUAN (+20.8%), PTRO (+11.6%) and DSSA all
ran. Neither existing board could have caught them, for structural reasons:

- The crowded board ranks Telegram chatter. These campaigns were silent.
- The momentum board requires `rvol5 ∈ [1.5,3.0]`, `rsi ≥ 55`, `dd60 ≥ −10%`. On 2026-08-11 BREN
  was **down 1.8%** — it failed every gate the day before the move.
- DSSA was not in `tickers.csv` at all, so no board could have scored it at any value.

Yet on 2026-08-11 TP bought 166,809 lots of BREN at an average of 3309.60 across 1,332 trades
while selling 666 lots, absorbing ZP's 86,001-lot dump, on a **red** day. That is the event this
board exists to catch.

This is the missing stage of the funnel:

```
Crowd (attention) → ACCUMULATION (footprint) → Momentum (price) → Trade plan
```

---

## 2. Why net value cannot work

The single most important observation in this document. BREN, 2026-08-05 → 08-12:

| broker | net (bn) | buy (bn) | sell (bn) | buy share |
|---|---|---|---|---|
| TP | +205.2 | 213.8 | 8.6 | **96%** |
| IF | +171.6 | 178.9 | 7.3 | **96%** |
| DX | +55.5 | 56.4 | 0.9 | **98%** |
| CC | +33.7 | 132.1 | 98.5 | **57%** |
| DP | −146.9 | 0.0 | 146.9 | **0%** |

CC has a **larger gross footprint than DX** and would outrank it on the momentum board's
`adtv_pct_total`. CC is market-making churn; DX is a real accumulator with essentially no sell
side. Net value cannot tell them apart. A ratio of grosses can.

`data/panel/flows-*.csv.gz` stores `net_value` only, so **the gross partition is mandatory, not
optional.** No amount of tuning recovers a ratio from a difference.

---

## 3. Features

Notation: `b` broker, `s` symbol, `t` signal day (panel index), `W` window in sessions.
Primitives `BV, SV, BF, SF, Bavg, Savg` are per broker-day from Sectors `broker_summary`
(`bval/sval/bfreq/sfreq/bavg_per_share/savg_per_share`) or Invezgo `summary-stock` with
`from == to`.

All features are computed from data **≤ day t**, matching `overlay_test.features()`.

### 3.1 One-sidedness — the discriminator

```
osr(b,s,W,t)   = ΣBV / (ΣBV + ΣSV)     over j ∈ (t−W+1 … t)
gross(b,s,W,t) = ΣBV + ΣSV
```

**Definedness guard (structural, not tunable):** `osr` is `None` — and the broker is **excluded
from scoring, not scored neutral** — unless `gross ≥ max(Rp5bn, 0.5·ADTV)`. A ratio of two small
numbers is noise, and admitting it as 1.0 would let a broker who bought Rp40m and sold nothing
outrank TP.

Buy threshold `osr ≥ 0.80` *(guess — tune)*. Mirror `osr ≤ 0.20` for distribution.

### 3.2 Persistence

```
run_buy(b,s,t)   = max R with NET(b,s,j) > 0 for all j ∈ (t−R+1 … t)
softrun(b,s,W,t) = #{ j ∈ W : NET(b,s,j) > 0 } / W
```

`softrun` is primary, `run_buy` is displayed. One flat day must not reset a three-week campaign.
`softrun_20 ≥ 0.60` *(guess)*.

`broker_alpha.build_events` uses a 3-day lookback. TP's BREN campaign ran 8 days. A 3-day window
cannot separate a single client order from a campaign — this is the same idiom generalised, so
the two compose.

### 3.3 Size vs liquidity

```
adtv_pct(b,s,W,t) = Σ NET(b,s,j) / ADTV(s,t)
```

Reuses `p.adtv` (trailing-20 sessions, strictly before `t`) — already leak-safe. For `W > 1` this
exceeds 100% and is **left uncapped**: that is the reading, not an error.

### 3.4 Average trade size — the order-slicing detector

```
ats_buy(b,s,W)   = ΣBV / ΣBF                            IDR per buy trade
value_share(b)   = ΣBV(b) / Σ_all ΣBV
freq_share(b)    = ΣBF(b) / Σ_all ΣBF
slice_z(b)       = ln( freq_share(b) / value_share(b) )
```

`slice_z > 0` means the broker is spending more *trades* than its rupiah share implies —
deliberate fragmentation. On BREN 2026-08-12, AK ran `buy_freq` 6,107 against `sell_freq` 1,167
for 344,210 lots ≈ 56 lots/print ≈ Rp20m tickets carrying Rp94.9bn of net. Retail-sized tickets,
institutional size.

**Relative, never absolute** — the same rupiah ticket is 128× the lots on a Rp50 stock as on a
Rp6,400 one, the reason `scoring.md` already gives for normalising everything cross-sectionally.

The panel carries **no frequency data at all** today. This is a genuinely new dimension, not a
re-expression of an existing one.

### 3.5 Cost versus VWAP — absorbing, or paying up?

```
cost_gap(b,s,t) = Bavg(b,s,t) / vwap_day(s,t) − 1
```

`vwap_day` from `data/intraday/m5-*.csv.gz`; fallback Invezgo `intraday-data.avg`.

- `cost_gap ≤ −0.15%` → sitting on the bid, absorbing supply — the stealth signature.
- `cost_gap ≥ +0.25%` → paying up, markup underway, you are late.

*(both guesses — tune)*

This is the broker-level fix for a limitation the codebase has already written down:
`overlay_test.structure()`'s own docstring concedes CLV and CMF20 *infer* accumulation from where
the **stock's** close sat in its range and are "not a true volume-at-price profile". `cost_gap`
measures where a **specific broker's fills** sat relative to the market. They routinely disagree —
a stock can close on its low (CLV 0) while the accumulator's average sits below VWAP.

### 3.6 Absorption on weakness — the entry

```
xr(s,t)             = raw_close return(t) − IHSG return(t)
absorb(b,s,t)       = NET ≥ max(Rp500m, 5%·ADTV)  AND  xr(s,t) ≤ +0.005
absorb_score(b,W,t) = Σ_{j∈W, xr(j)≤0} NET(b,s,j) / Σ_{j∈W} max(NET(b,s,j), 0)
absorb_pair(s,t)    = Σ_{b∈A} NET(b,s,t) / |Σ_{b:NET<0} NET(b,s,t)|
```

`absorb_pair` names **who was absorbed** — TP soaking ZP's 86,001 lots.

`is_momentum` requires `rsi ≥ 55` **and** `dd60 ≥ −10%`; `absorb` requires the opposite sign on
the day. The two rules are near-disjoint **by construction**. That is the mechanical reason
2026-08-11 was invisible to the momentum board, and it is why this board is not a re-skin of it.

### 3.7 Routing anomaly — accumulation through low-profile brokers

The BREN accumulators were TP (OCBC), IF and DX — not the desks the market watches (BK JPMorgan,
AK UBS, ZP Maybank, RX Macquarie, KZ CLSA). Routing size through a broker nobody tracks *is* the
disguise. Expressed as a ratio so it never depends on a hand-maintained list of "obscure" brokers:

```
prominence(b,t)    = b's market-wide gross / all-broker gross      [top_brokers, 1 date]
concentration(b,s) = b's gross in s        / s's total gross
routing_anomaly    = concentration(b,s) / prominence(b,t)
stealth_router     = ln(routing_anomaly) × osr20 × max(0, slice_z)
```

A broker with 0.2% market share doing 20% of one name scores 100×.

```
breadth(b,t) = # candidate names where b qualifies today
```

DX ran ~98% one-way in **both** BREN and CUAN; BK was +73.1bn in BREN and +164.4bn in DSSA.
`breadth ≥ 2` means the **theme** is the tradable object, not the ticker.

### 3.8 Tape microstructure — second-resolution

From `running-trade` on a **closed** session: unmasked broker codes, second-granular timestamps,
and a per-print client-domicile flag. Verified BREN 2026-08-12:

```
10:30:08  3520  ZP←AK 2300 · NI←AK 200 · XL←AK 300 · AK←AK 1900 · XC←AK 200 · ZP←AK 2700
          3520  AK←AK 8500 · YP←AK 100 · ZP←AK 3100 · AK←AK 10500 · CC←AK 7300
          3520  AK←AK 20500 · ZP←AK 2200 · TP←AK 142600
```

Sixteen prints in **one second**, all at 3520, AK on the sell side of every one, including four
AK→AK self-crosses. At 10:30:41, twelve prints in one second, all DP selling.

| Feature | Definition |
|---|---|
| `burst_max_1s` / `burst_max_10s` | max prints by `b` in any 1s / 10s window |
| `p_burst` | share of `b`'s prints in a second where `b` has ≥4 prints |
| `ipi_sub1s` | fraction of consecutive inter-print intervals < 1s |
| `clip_uniformity` | share of prints at the modal lot size; entropy of the lot-size distribution |
| `price_pin` | share of prints at the single modal price |
| `self_cross_pct` | prints where buyer broker == seller broker |
| `passive_pct` | share of `b`'s prints where the OTHER side crossed the spread |
| `client_dom_split` | share of `b`'s buy value with `buyer_dom == "F"` |

#### A sweep is not a slice — the distinction that decides the reading

This was got wrong in the first draft of this document and is corrected here, because
reading one as the other inverts the conclusion.

The `aggressor` field names the side that crossed the spread. For a broker's **buy**
prints, `aggressor == "SELL"` means *someone hit the broker's bid* — the broker was
resting and passive.

| | **Sweep** | **Slice** |
|---|---|---|
| shape | one second, many prints, **many counterparties** | many prints spread **across time** |
| clips | whatever was resting | uniform, repeated |
| price | one or two levels consumed | pinned to one level |
| aggression | **aggressive** — crossing the spread | usually **passive** |
| meaning | impatience: capitulation, a stop-run, or a desk clearing a level in one order | patience: one participant working a large order quietly |

**The prints in a sweep are the victims' resting orders, not the sweeper slicing its
own.** A 327-print second is one large market order consuming a whole level, and
counting those prints as the aggressor's "fragmentation" flags the most impatient
participant on the tape as the most patient one.

`tape_lib.classify_behaviour()` returns one of `sweep / slice / block / passive-block /
passive-absorb / mixed / thin` per (broker, side), and the board must print the label
rather than a raw burst count, for exactly this reason.

**`buyer_dom`/`seller_dom` is the CLIENT's domicile, not the broker's.** Verified: adjacent prints
read `ZP,DP,D,F` then `ZP,DP,F,F` — same buying broker, same selling broker, different flag. So
every print carries a foreign/domestic **client** label. This is a *direct measurement* of
institution-vs-retail participation and is strictly better than Sectors' broker `cohort` field,
which is a licensing label that classifies YP as institutional (see `brokers.csv`).

### 3.9 Broker quality — tilt, never gate

Reuse `broker_alpha.score_brokers(by="mean")` and the exact `build_momentum_board.rank_score`
constant:

```
tilt = 1.2 − 0.4 × (best_rank − 1) / max(1, n_brokers − 1)
```

Importing the same function means the two boards cannot silently disagree about what "a good
broker" means. Ranking brokers by hit-rate instead of mean excess is **already refuted**
(−2.3%/−2.8% vs +2.3%/+2.1%) — do not revisit it.

**Caveat that must be tested, not assumed:** that ranking was fitted on net-value accumulation
events across 112 names. Applying it to one-sidedness events is out-of-domain transfer. §6
requires reporting the board with and without tilt. **If the tilt does not help out of sample,
drop it.**

---

## 4. Score, windows, buckets

### 4.1 Fixed caps, not the day's maximum

The crowded board normalises `x / day_max`. That is right for a chatter board, where only today's
ranking matters. It is **wrong here**: it makes scores non-comparable across days and hostage to
one outlier, and a board whose job is "fire early" must let you see that today's 62 is weaker than
last Tuesday's 81.

```
n_osr    = clamp((osr20 − 0.60) / 0.35,        0, 1)
n_size   = clamp(adtv_pct_20 / 150.0,          0, 1)
n_pers   = clamp(softrun_20 / 0.80,            0, 1)
n_absorb = clamp(absorb_score_20 / 0.60,       0, 1)
n_cost   = clamp((0.0025 − cost_gap_20)/0.0050,0, 1)
n_slice  = clamp(slice_z / 0.70,               0, 1)

stealth = 100 × (0.28·n_osr + 0.22·n_size + 0.16·n_pers
                 + 0.18·n_absorb + 0.10·n_cost + 0.06·n_slice) × tilt × window_factor
```

**Weights are a declared guess and must not be continuously fitted.** Five candidate vectors are
declared here, before any result is seen, and walk-forward picks among them:

| # | Vector |
|---|---|
| V1 | equal weight across the six terms |
| V2 | osr-heavy — `0.45 osr, 0.20 size, 0.15 pers, 0.10 absorb, 0.05 cost, 0.05 slice` |
| V3 | absorb-heavy — `0.20 osr, 0.15 size, 0.15 pers, 0.40 absorb, 0.05 cost, 0.05 slice` |
| V4 | the vector above |
| V5 | V4 with `tilt = 1.0` (no broker-quality tilt) |

Fitting continuous weights turns a rule into a model wearing a rule's clothes, and this repo's
credibility rests on that distinction.

### 4.2 Multi-window is an agreement gate, not an average

**Window choice flips the sign.** BREN top buyers over 8 days: TP +205.2bn, IF +171.6, BK +73.1.
Over 90 days BK is **−413.4bn**, a net *seller*, and KZ flips the other way. A single-window score
is a lookback artifact.

Windows **5 / 20 / 60**, each justified rather than round:

- **5** — the campaign-detection floor. Below it, one client order dominates the ratio. The
  2026-08-11 BREN entry is visible here.
- **20** — matches `p.adtv`'s trailing-20 definition exactly, so `adtv_pct` at `W=20` reuses the
  panel's existing normaliser instead of introducing a second one.
- **60** — the regime check, the window at which BK flips, and it matches the existing `dd60`.
  (90 is Sectors' natural maximum but straddles two quarters and two index rebalances.)

```
window_factor = 0.60  if sign(net_5) ≠ sign(net_60)
                1.15  if sign(net_5) == sign(net_20) == sign(net_60)
                1.00  otherwise
```

Score base is `W=20`. A conflicting broker is penalised **and** carries a visible badge on the
page — `BK: 5d +73bn (83%) / 60d −413bn (31%) ⚠`. The renderer always prints all three windows for
the lead broker, so the requirement is enforced by the page rather than by discipline.

**Jitter guard.** Recompute each `osr` at `W ± 2` sessions. Movement > 0.10 flags the row
`unstable` and caps its score at the day's 60th percentile. A real 8-day campaign is insensitive
to where you start counting; a single block trade is not.

**Rejected, with reasons:**

- *Average the three `osr`s.* Averaging BK's 0.83 and 0.31 gives 0.57, which reads as "churn" —
  wrong in a new way. The information lives in the disagreement; averaging destroys exactly that.
- *Longest window only.* Too slow. BREN's 60d nets were dominated by OD and RX; it would have
  flagged weeks late or never.
- *Shortest window only.* That is the BK trap by construction.
- *EWMA into a single number.* Still has a lookback, but hides it — worse than one you can see.

### 4.3 Buckets

Evaluated in order, **first match wins**. Churn is last on purpose: `scoring.md` already
establishes that churn is the *absence* of a read and must never pre-empt one.

| # | Bucket | Predicate | Read |
|---|---|---|---|
| 1 | **Distribution — retail trap** | leads had `osr20(t−1) ≥ 0.80`; today `osr1d ≤ 0.35`, `xr ≥ +5%`, `rvol5 ≥ 2.5` | Whale handing over. AVOID / EXIT. |
| 2 | **Markup underway** | `stealth ≥ 40`, `xr_5d ≥ +8%`, `cost_gap_5d ≥ +0.25%` | Late but live. Momentum board's territory. |
| 3 | **Absorption on weakness** ★ | `osr5 ≥ 0.85`, `net_5d ≥ max(Rp10bn, 30%·ADTV)`, `absorb_score_5 ≥ 0.60`, `xr ≤ 0`, `dd20 ≤ −2%` | **THE ENTRY. BREN, 2026-08-11.** |
| 4 | **Stealth accumulation** | `stealth ≥ 35`, `abs(xr_20d) ≤ 8%`, `rvol5 ≤ 1.3`, `softrun_20 ≥ 0.55` | Campaign running, price asleep. Watchlist. |
| 5 | **Churn — fake** | lead `osr20 ∈ [0.40, 0.60]`, `gross_20 ≥ 3·ADTV` | Big footprint, no positioning. Shown so it is visibly *rejected*. |
| 6 | **Cooling** | in bucket 3/4 within 10 sessions, now `softrun_5 ≤ 0.20` or `osr5 ≤ 0.50` | Whale stopped. Stand down. |

**Hard gates before any scoring (structural):** universe membership; the §3.1 definedness guard
per broker; at least one broker with `osr20 ≥ 0.80` **and** `Σnet ≥ max(Rp10bn, 20%·ADTV)`.

**Only buckets 3 and 4 ask you to buy.** Everything else exists to be looked at and not traded,
exactly like the momentum board's Exhaustion section.

Sizing hands off unchanged to `trade_lib`: `atr()` → `stop_price(k_atr=1.5, struct5d low)` →
`size_position(risk 1.5%, adtv_cap 5%, max_pos 30%)` → `admit()`. Exits E1/E2 as validated
(n=915, 3/4 folds, +0.82pp). **No new risk logic** — the accumulation board decides *what*, the
existing trade layer decides *how much*.

---

## 5. The retail-trap metric

Measured on surge day **D**. Let `A` = accumulators **frozen at D−1**: `osr20(D−1) ≥ 0.80` and
`net_20(D−1) ≥ max(Rp10bn, 20%·ADTV)`.

**Freezing `A` at D−1 is structural.** Selecting the accumulator set using D's own data would make
the metric circular and guarantee a large answer.

```
M1  trap_rate     = Σ_{b∈A} SV(b,D) ÷ Σ_{b∈A} position_value(b, D−1)
    position_value(b,D−1) = Σ_{j=D−W…D−1} NET(b,s,j),  W = run_buy(b,D−1) floored at 5

M2  retail_absorb = net_buy_retail(D) ÷ |Σ_{b: NET(b,D)<0} NET(b,D)|

M3  handover_hhmm = argmax_k Σ_{b∈A} y_k          [intraday-inventory]
    turn(b,D)     = (max_k y_k − y_K) / max_k y_k
    retail_after  = Σ_{b∈R} (y_K − y_handover) ÷ Σ_{b∈R} y_K
```

**M1 in words: "the whale sold X% of the position it had just built, on the day it marked it
up."** Numerator and denominator both printed in rupiah beside the percentage. The denominator is
structural, not tunable — changing it changes what the number means.

`turn ≥ 0.30` on a green day means the "accumulator" peaked mid-session and sold a third back —
**distribution wearing accumulation's daily net**, invisible to every daily-bar feature in this
repo.

`R` (retail) is taken **both** from Sectors `cohort="retail"` **and** from the behavioural list in
`brokers.csv` (XL, XC, YP, KK, XA). Report both; if they diverge > 25%, **flag rather than pick**.
Cross-check against `client_dom_split` from §3.8, which is a direct measurement rather than a
label.

Composite tag *(thresholds are guesses — tune)*:

| Condition | Tag |
|---|---|
| `trap_rate ≥ 0.35` **and** `retail_absorb ≥ 0.30` | **RETAIL TRAP (confirmed)** |
| one of the two | **PARTIAL DISTRIBUTION** |
| neither, `A` still net buying | **MARKUP, WHALE STILL LONG** — the only variant where holding is defensible |

**Measurement floor.** `market=NG` crossings are additive and absent from Sectors. A whale can
distribute through a negotiated cross that never touches RG. Compute M1 on RG, then again on
RG+NG via Invezgo `summary-stock market=NG`; **if the NG leg exceeds 10% of day value, the RG-only
figure is a floor and must be labelled as one on the page.**

---

## 6. Validation

Non-negotiable, and it gates the trade-plan half of the output. The board may ship in observation
mode before this passes; **no entry, stop or size may be emitted until it does.**

### 6.0 Check 0 — is the window a regime where a known-good rule still works?

**Runs before anything else, and a failure voids every result that follows.**

Measure `is_momentum` (the only accumulation rule in this repo that has survived a
walk-forward) over the candidate test window and over the full panel. If its lift in the
window is not clearly positive, **the window cannot refute anything** — a new rule failing
there is indistinguishable from the quarter being hostile to the whole family.

This check was added on 2026-08-13 after it caught a wrong verdict. The 59-session gross
window returned +2.26pp for the momentum rule over the full 2 years but **−1.39pp inside
the window**, a ~3.8pp swing, and one-sidedness had already been written up as "refuted"
on the strength of a −0.83pp reading from that same stretch. Against the shared top-20
baseline the momentum rule was actually the *worse* of the two there.

Pass condition: momentum lift in-window ≥ +0.5pp against the same matched baseline the
test will use. Below that, extend the window before reading any result.

### 6.1 Events

**Level 1 — broker-day** (large n; exists so a Level 2 failure can be *diagnosed*, not merely
observed):

```
osr20(b,s,t) ≥ θ_osr                            grid: 0.70 / 0.80 / 0.90
net_20 ≥ max(Rp10bn, θ_adtv · ADTV(s,t))        grid: 10 / 20 / 40 %
gross_20 ≥ max(Rp5bn, 0.5·ADTV)                 structural
s ∈ universe(t)                                 structural
```

**Level 2 — board rows**: the (stock, day) rows the board would actually print in bucket 3 or 4,
after broker collapse and the window penalty. **Level 2 is what must pass.**

**Declared grid additions (2026-08-13), both from underspecification found while building:**

| axis | values | why it is on the grid |
|---|---|---|
| `absorb_mode` | `today` \| `window` | §3.6 defines **two** absorption measures — a same-day boolean and a 5-session share — and §4.3 wired the window form with no evidence behind the choice. Not a tuning knob: on BREN 2026-08-11 TP cleared every other entry condition (osr5 97%, net5 Rp186.6bn against a Rp45bn floor, xr −0.25%, dd20 −10.8%) and was blocked *only* by `absorb_score_5` = 0.30, because just two of those five sessions were down. The two forms decide **when** you act, not whether the name is seen — under `window` the same row falls through to *stealth* instead of disappearing. |
| aggregation | `single` only | Coalition **tested and rejected** (§7b). Kept on the grid solely so the rejection stays reproducible. |

**Conditions are evaluated JOINTLY PER BROKER**, and the stock then takes the best-priority
broker's bucket. Testing them against whichever broker happens to have the highest `osr20`
is a weaker and different question, and it demonstrably loses the signal: on BREN
2026-08-11 the top-`osr20` broker was IF, whose buying had landed on up-days, while TP — the
desk that actually absorbed 72% of the day's distribution — ranked second and was never
evaluated.

Bucket priority for that choice is `distribution > absorption > markup > stealth > cooling >
churn > none`. **Churn sits second-to-last, never mid-table.** Ranked any higher, a stock's
label is decided by whichever market maker was busiest — XL took over every row on the first
run — and a genuine accumulator two lines down is never surfaced.

**The definedness floor must scale with the window.** `gross ≥ 0.5 × ADTV` is a different
level of strictness at every window length, because ADTV is a *daily* average. Applied to a
5-session window it returned `None` for every broker on every name, including TP on BREN, and
the board rendered empty with no error — the worst way for a threshold to be wrong. The floor
is now a share of the window's own two-sided gross: `max(Rp5bn, 0.02 × 2 × w × ADTV)`.

### 6.2 Forward return

`p.excess_return(sym, i, k, entry_lag=1)`, `k ∈ (3, 5, 10)`.

**The one-session entry lag is mandatory.** Broker data publishes ~18:00 WIB; the earliest
possible fill is the next session. 3 and 5 for comparability with `broker_alpha.HORIZONS`.
**10 is added and is the horizon that decides this** — the entire premise is that this board fires
*earlier*, and an edge at 3d but not 10d means it is not early, only noisier.

### 6.3 Folds

Primary test is `momentum_setup.py`'s period-stability form: 4 equal stretches, lift positive in
each. (`broker_alpha.walk_forward(train=250, test=60, step=60)` yielded only 3 folds on 474
sessions — too thin to be primary.)

Walk-forward is used **only to pick among the 5 declared weight vectors**: train = best vector on
`[t−250, t)`, test on `[t, t+60)`.

**Declared before running: if the walk-forward winner differs from the in-sample winner in more
than one fold, ship V1 (equal weight) and use no weights at all.**

### 6.4 Three null controls

One is not enough here, and the reason is specific:

1. **Broker-label shuffle** — the existing `null_control()`. Tests "is it broker identity?"
2. **Date shuffle within symbol** — *the important one*. Keep each `(broker, symbol)` series
   intact but circularly shift its date index by a random per-symbol offset, then rebuild events.
   This destroys flow↔price alignment while preserving each broker's autocorrelation and each
   stock's return distribution. **One-sidedness is highly autocorrelated, so a broken harness
   would pass null #1 and fail only this one.**
3. **Universe-only** — same (stock, day) universe membership, candidates drawn at random within
   it. Tests whether the board beats simply owning whatever was most-traded, which among IDX's top
   names is a real route to a fake edge.

Every null must land within **±0.3pp** of its matched baseline. A positive edge under null #2
means the harness leaks and no result is believable.

### 6.5 Sample size

- Level 1: **n ≥ 1,000** per horizon after filters, **≥150 per fold**.
- Level 2: **n ≥ 250** board rows, **≥40 per sub-period**.

Level 2 binds. 20 names × 474 sessions = 9,480 stock-days; at a 3–5% firing rate that is 280–470
rows. **If the realised rate is under 2%, widen the universe to top-40 for validation only and say
so on the page — do not lower the bar.**

### 6.6 Pass bar — declared in advance, all must hold

1. Level 2 **5d mean excess ≥ +1.2pp** over the *matched* baseline: all accumulation events in the
   same top-20 universe on the same days — **not** zero, **not** the 112-name panel baseline.
   (Momentum cleared +1.40pp at 5d; +1.2 makes the two comparable.)
2. Positive 5d lift in **≥3 of 4** sub-periods; non-negative 10d lift in ≥3 of 4.
3. **10d lift ≥ 3d lift** — the "is it actually early?" test.
4. All three nulls within **±0.3pp**.
5. Graceful degradation: 5d lift positive in **≥7 of 9** cells of the `θ_osr × θ_adtv` grid.
6. **≤35% overlap** with `is_momentum(f, 1.5, −0.10, 55.0, 3.0)` on the same `(s,t)`.

### 6.7 What refutes it — the conditions under which we ship nothing

- **Level 2 5d lift < +0.5pp**, or negative in 2+ of 4 sub-periods → the stealth thesis fails the
  way "quiet accumulation" already failed (−0.04% / +0.01%). Write it up here beside the existing
  refutation and **stop**.
- **3d lift > 2× the 10d lift** → the signal is late, not early. Do not ship a third board; add
  `osr20` as a tilt column on the momentum board.
- **Momentum overlap > 60%** → same conclusion: a column, not a board.
- **Null #2 shows > 0.3pp** → harness leak. No result until clean.
- **The `osr20` quintile gradient is flat or inverted** (run `overlay_test.py`'s existing
  `GRADIENT CHECK` block on `osr20`) → the discriminator itself is wrong and the BREN read was one
  anecdote. **This is the cheapest refutation available and it runs FIRST, before any paid
  backfill.**
- **n cannot reach 250 Level-2 rows even at top-40** → underpowered. Report the point estimate and
  explicitly do not ship.

### 6.8 Anchoring check — sanity, never evidence

Does the rule fire on BREN 2026-08-11, and on CUAN/PTRO/DSSA in the days before 08-12? A rule that
passes every statistic but misses the four names that motivated it has been fitted to something
else.

**Reported clearly labelled as in-sample motivation. It is never counted as confirmation.**

---

## 7. Deliberately absent

Read-outs that are computed and displayed but **gate nothing**, and screens that were considered
and rejected. Same register as the momentum board's "what did not validate" section.

| Item | Status | Why |
|---|---|---|
| **Net foreign flow as a screen** | **Refuted for this purpose** | It was **negative** on both decisive BREN days (−8.65bn on 08-11, −1.52bn on 08-12) while foreign *brokers* were the accumulators. The buying was foreign-broker-booked but nets out at country level. A national foreign-flow screen vetoes the best signal of the month. Carried as a read-out only. |
| **Broker ranking by hit-rate** | Refuted (existing) | −2.3%/−2.8% vs +2.3%/+2.1% for mean excess. Settled in `broker_alpha.py`; do not revisit. |
| **`top-change`** | Unusable | The `date` parameter is silently ignored — it returns a live snapshot. Any backtest built on it measures today. |
| **`screener` for history** | Unusable | No date parameter; `change` is denominated in **rupiah, not percent**; `value` and `frequency` are not valid fields. |
| **`momentum` at `range=1`** | Rejected by the API | 5 minutes is the floor. Sub-5-minute frequency must come from the tape. |
| **Frequency as a universe *selector*** | **Not available** | Neither API has a market-wide, date-honouring frequency ranking. `fetch-most-traded-stocks` has no `freq` field; `screener` has no `frequency`; `momentum scope=freq` is per-code. The universe can be ranked by **value and volume only**. Frequency survives as a per-name attribute. This is a partial no on the original requirement and is stated rather than papered over. |
| **`intraday-data` closing L1 for spoof detection** | Won't work | `freq` is always 0 — the one field that matters is dead — and a single snapshot at the close is the least informative moment of the session. |
| **`sankey` for spoofing** | Won't work | It is *matched trades*, so by definition it contains zero cancelled orders. Kept for self-cross and trap confirmation only. Parsing trap: node names are space-padded and the padding encodes the side (`" TP "` buy vs `" AK  "` sell); the same broker appears as two nodes and a naive parse double-counts. |
| **Cancellation-based spoofing, historically** | **Impossible on this tier** | No historical feed carries order placements or cancellations. `batch-order-book` *does* accept `date` and `time` — true book replay — but returns 402. See §8. |

---

## 7b. Calibration findings — 2026-08-13

What the declared rules actually did on the four motivating names. Thresholds in §3–§5 were
written **before** this ran and are **not** edited to improve these numbers. Where a rule
missed, the miss is recorded and the candidate fix is added to the walk-forward grid as a
*declared alternative*, to be settled by §6 rather than by inspection.

### The gate fired on 2 of 4

| name | surge | entry day | accumulators qualifying | verdict |
|---|---|---|---|---|
| **BREN** | 08-12 +13.3% | 08-11 **−1.8%** | **TP** 95% osr20, Rp188.8bn; **IF** 95%, Rp171.5bn | ✅ caught |
| **PTRO** | 08-12 +11.6% | 08-11 **−1.9%** | **AI** (UOB Kay Hian) 83% osr20, Rp114.2bn | ✅ caught |
| **CUAN** | 08-12 +20.8% | 08-11 +1.4% | none | ❌ missed |
| **DSSA** | **08-06** +12.1% | 08-05 +3.6% | none | ❌ missed |

**BREN, the reference case, works exactly as designed.** On 2026-08-11 with the stock
**down** 1.8%, TP bought Rp55.2bn against Rp217m sold — `osr` 100% over 1,332 prints at an
average of 3,310 versus a session VWAP of 3,310.8, i.e. `cost_gap` −0.01%: it sat on the bid
and was hit. It absorbed 72% of the day's entire net distribution. `classify_bucket` returns
**absorption**. The next session was +13.3%.

**Diagnosis of the two misses — the ADTV-relative floor scales badly.** `net20 ≥ 20%·ADTV`
means a single broker must clear Rp84–112bn on DSSA (ADTV Rp421–560bn) versus Rp31bn on BREN
(ADTV Rp154bn). The campaigns were there; they were just **diffuse across several brokers**.
DSSA on 2026-08-05 had four one-sided buyers — SS 100% Rp27.3bn, RF 94% Rp14.3bn, AZ 78%
Rp11.7bn, BK 100% Rp5.9bn — summing to Rp59.2bn, and not one of them clears the bar alone.

> **Declared alternative:** score the **coalition** — `Σ net over all brokers with
> osr ≥ θ_osr` — instead of requiring one broker to clear the floor alone. A campaign can
> be run through several accounts, so this is a different model of the same hypothesis and
> had to earn its place out of sample.

### The coalition form was tested and REJECTED — 2026-08-13

`accum_test.py --mode coalition`, run on the rebuilt 159-name / 475-session panel.
n = 8,675 matched baseline stock-days (every top-20 name, every session). COCO excluded:
the panel's adjustment check flagged an unhandled corporate action (+248.8% in one day).

Because `osr` needs the gross partition and that was not yet backfilled market-wide, the
broker filter here is **`softrun20 ≥ 0.75`** — the share of the last 20 sessions a broker
was net positive. That is a *proxy*, and a permissive one, but it is **the same proxy on
both sides of the A/B**, so it cannot bias a comparison that differs only in whether the
size floor is applied per-broker or to the coalition sum.

| θ_adtv | single | coalition | coalition-only | 5d lift, coalition-only |
|---|---|---|---|---|
| 10% | 5,648 | 5,707 | 59 | **+0.23pp** |
| 20% | 4,936 | 5,194 | 258 | **−0.63pp** |
| 40% | 3,684 | 4,241 | 557 | **+0.44pp** |

**The marginal events flip sign across the grid.** That is noise, not a finding. At every
threshold the coalition's own lift is equal to or *worse* than the single-broker form
(−0.09 vs −0.06pp at the design point). **Rejected. Ship the single-broker form.**

### A larger negative result, recorded in full

The same run says something more important than the coalition answer:

- Matched baseline, k=5: **+0.61pp**. Top-20-by-value names drift up on their own.
- Single-broker gate, k=5: **−0.06pp lift**. Against a declared bar of **+1.2pp**.
- Sub-periods: **2 of 4** positive, against a declared bar of 3 of 4.
- Null controls: broker-label shuffle **−0.06pp**, date-shift **−0.03pp** — i.e. the
  nulls reproduce the real result to within 0.03pp. The events carry **no information
  beyond universe membership**.

By §6.7 this meets the declared refutation condition. But read the diagnosis before
concluding anything about one-sidedness: **the gate fired on 56–65% of the universe**
(4,845–5,648 of 8,675 stock-days). A rule selecting three-fifths of the universe is not a
screen, and it necessarily reproduces the baseline. So what is refuted is
**net-persistence plus a size floor** — the free proxy — and not `osr`, which was never
tested here.

**Consequence:** the gross partition is no longer optional or a refinement. It is the only
untested path left, and `osr` is now the single load-bearing claim of the whole board.
Until `accum_test.py --mode gate` runs on it, the board ships in observation mode and
emits no trade signal.

**Also recorded:** the four names did not share a surge date. DSSA's move was 2026-08-06, not
08-12. A case study anchored on one date analyses the wrong session for some names.

### The slicing sign is INVERTED — the most important correction here

§3.4 predicted that a whale slicing a large order shows up as `slice_z > 0` (more trades than
its rupiah share implies). **The tape says the opposite.**

| name / day | broker | role | prints | avg ticket | `slice_z` |
|---|---|---|---|---|---|
| BREN 08-11 | **TP** | accumulator | 1,332 | Rp41m | **−1.05** |
| BREN 08-12 | **TP** | accumulator | 655 | Rp31m | **−0.68** |
| BREN 08-12 | **IF** | accumulator | 47 | Rp31m | **−0.68** |
| BREN 08-12 | **XL** | retail | 3,145 buy / 5,531 sell | Rp5m | **+1.04 / +0.52** |
| PTRO 08-11 | **AI** | accumulator | 174 | Rp46m | **−1.07** |
| PTRO 08-11 | **XL** | retail | 4,430 | Rp7m | **+0.85** |
| DSSA 08-11 | **XL** | retail | **25,337** | Rp5m | strongly + |

The accumulators consistently run **negative** `slice_z`: TP did 14% of the day's prints and
40% of its value. The consistently **positive** `slice_z` belongs to XL, YP and CC — the
retail brokers in `brokers.csv`.

**High frequency relative to value is the crowd, not the whale.** The whale takes size in
large clips; retail is what generates thousands of Rp5m tickets. The feature is real and
useful — with the sign reversed, and pointed at a different question.

**Consequences, both declared for the walk-forward rather than assumed:**
1. The score's `n_slice` term as written in §4.1 **rewards retail crowding**. Its weight is
   0.06, so the damage is small, but the sign is wrong. The declared alternatives are
   `block_z = −slice_z` (reward large-clip accumulation) and dropping the term entirely.
   V1–V5 already include a no-slice variant via equal weighting; a sixth vector `V6_block`
   carries `block_z`.
2. `slice_z` on the **retail cohort** becomes a direct retail-participation measure and feeds
   §5's `retail_absorb` leg, where it is a *measurement* rather than a broker label.

### Foreign flow: the per-print client flag sees what the standard metric cannot

`buyer_dom`/`seller_dom` travels with the **order**, not the broker (§3.8). On BREN:

| | country-level foreign flow | tape, client domicile |
|---|---|---|
| 2026-08-11 | −Rp8.65bn | **F −Rp15.2bn / D +Rp15.2bn** |
| 2026-08-12 | −Rp1.52bn | **F +Rp138.5bn / D −Rp138.5bn** |

On the surge day foreign *clients* were net buyers of Rp138.5bn while the headline foreign
flow read −Rp1.52bn. The two measure different things — the standard metric aggregates
foreign-*broker* net, so foreign clients trading through domestic brokers are invisible to
it. This is a much sharper statement of §7's "do not use foreign flow as a screen": it is not
merely noisy, it can be **two orders of magnitude off and pointing the wrong way**.

### The retail trap mostly did not happen

| name | `trap_rate` | tag |
|---|---|---|
| BREN | **1.1%** | MARKUP, WHALE STILL LONG (TP 92% osr on the surge day — *still buying*) |
| PTRO | **22.1%** | PARTIAL DISTRIBUTION (AI sold Rp13.9bn of Rp62.8bn, went net −Rp3.4bn) |

BREN's accumulators kept their position through a +13.3% day. The Rp229.5bn of net
distribution came from **DP (−Rp84.0bn, `osr` 0%), XL (−Rp34.2bn) and KZ (−Rp17.8bn)** — not
from the accumulators. On PTRO the biggest sellers into the surge were **XL (−Rp74.0bn) and
YP (−Rp23.7bn)**, both retail brokers.

**So on these names retail was selling into the markup, not being trapped by it.** The
prior — "the whale distributes to retail on the surge day" — is not what the tape shows. The
`distribution` bucket is still correctly specified; it simply did not fire here, and a board
that claimed it had would be wrong.

### Sweeps are the sellers' signature, not the buyers'

Largest one-second clusters found:

| when | broker | side | prints | counterparties | self-crosses |
|---|---|---|---|---|---|
| BREN 08-11 10:57:47 | AK | sell | **327** | 33 | — |
| BREN 08-12 13:59:44 | PD | buy | **437** | 41 | **29** |
| BREN 08-12 10:51:57 | AK | buy | 331 | 35 | 8 |
| PTRO 08-11 13:40:39 | SS | sell | 144 | 25 | — |
| PTRO 08-11 13:45:52 | CC | sell | 126 | 22 | **21** |

On the accumulation day the sweeps are **sells** — capitulation into the accumulator's bid.
On the markup day they flip to **buys** — chasing. Self-crossing is common and material
(PD crossed 29 of 437 prints in one second), which inflates a broker's gross on both sides
and drags `osr` toward 0.5; any desk with a high `self_cross_pct` needs its `osr` read with
that in mind.

---

## 7c. THE GATE TEST — REFUTED, on a window verified usable (2026-08-13)

> **FINAL STATUS, after extending coverage.** This section went through three states in a
> day: "refuted" → "inconclusive" → **refuted properly**. The measurements below are all
> real; only the window they were read from changed. Read §7d first — it is the verdict
> that counts. What follows is the record of how it was reached.

---

### The intermediate correction (kept, because the reasoning is the reusable part)

> This section first read "one-sidedness is refuted" off a 59-session window. That
> verdict was wrong, and the error was mine: the test window cannot support a refutation.
>
> **The 59-session gross window is a regime in which the VALIDATED momentum rule also
> inverts.** Measured on the same rebuilt panel, `is_momentum` at k=5 returns:
>
> | period | mean excess vs IHSG | n |
> |---|---|---|
> | full 2 years | **+2.26pp** | 2,862 |
> | before 2026-05-18 | **+2.39pp** | 2,765 |
> | **2026-05-18 → 08-12 (the gross window)** | **−1.39pp** | **97** |
>
> A ~3.8pp swing. Against the same top-20 baseline that the gate used (+0.58pp at k=5 in
> this window), the momentum rule's lift is about **−1.97pp** — i.e. **worse than
> one-sidedness's −0.83pp**. Over this stretch the entire "accumulation into strength"
> family is negative, and one-sidedness was the *less* bad of the two.
>
> So the numbers below stand as measurements and are void as a verdict. **The correct
> status is INCONCLUSIVE, and the correct response is to extend coverage** (~340 Sectors
> credits per additional quarter), not to abandon the thesis. Everything below is retained
> because the measurements are real; only the conclusion changed.
>
> The general lesson is worth more than this result: **a validation window must be shown
> to be a regime where a known-good rule still works, before a new rule's failure in it
> means anything.** That check is now mandatory and is added to §6 as check 0.

### The original section (measurements valid, verdict withdrawn)

`accum_test.py --mode gate`, against the real gross partition: 52 symbols (COCO excluded
for an unadjusted corporate action), 59 sessions, 2026-05-18 → 2026-08-12, 161,313
broker-days, 343 Sectors credits. Sectors↔Invezgo reconciliation exact: 0 of 51,697 rows
differ by more than 1%, cross-vendor buy-value gap 0.00%.

**Matched baseline** (top-20 universe, same sessions): +0.18pp / +0.58pp / +2.11pp at
k=3/5/10.

### Design point — osr20 ≥ 0.80, net ≥ 20% ADTV, absorb_mode=today

| | n | mean excess | lift vs baseline |
|---|---|---|---|
| k=3 | 162 | −0.16pp | **−0.35pp** |
| k=5 | 162 | −0.25pp | **−0.83pp** |
| k=10 | 162 | +0.16pp | **−1.95pp** |

Sub-periods at k=5: `+0.58 / −2.02 / −0.66 / n.a.` → **1 of 4 positive**.

### Negative in all 18 grid cells

| θ_osr | absorb=today | absorb=window |
|---|---|---|
| 0.70 | −0.81 to −0.85pp | −0.72 to −0.74pp |
| 0.80 | −0.83 to −0.84pp | −0.68 to −0.69pp |
| 0.90 | −0.96pp | −0.83 to −0.84pp |

Tightening one-sidedness makes it **worse**, monotonically. There is no corner of the
declared grid where this works.

### Verdict against §6.6

| check | result |
|---|---|
| 1. 5d lift ≥ +1.2pp | **FAIL** −0.83pp |
| 2. ≥3 of 4 sub-periods positive | **FAIL** 1/4 |
| 3. 10d lift ≥ 3d lift | **FAIL** −1.95 vs −0.35 |
| 5. ≥7 of 9 grid cells positive | **FAIL** 0/9 |
| n. Level 2 n ≥ 250 | **FAIL** n=162 |

**GATE NOT PASSED, so the board stays observation-mode and emits no entry, stop or size.**
But NOT passed is not the same as refuted — see the correction at the head of this
section. `trade_plan.py` integration is **deferred pending a longer window**, not
cancelled.

The measurements meet the §6.7 numeric refutation condition. They do not *establish* a
refutation, because §6 check 0 (below) fails: the window is one in which the known-good
momentum rule also inverts, so a negative reading here is not attributable to the rule.

Standing tally, with confidence levels attached rather than a flat list: quiet
accumulation **refuted** (−0.04/+0.01pp, full-history); net-persistence **refuted**
(−0.06pp, n=8,675, full-history, nulls matching to 0.03pp); coalition **rejected**
(sign-flipping across the grid, full-history); one-sidedness **inconclusive** (−0.83pp,
n=162, 59 sessions, anomalous regime). The first three were measured over the whole
panel and stand. The fourth was not.

### What this result does NOT license

- **Null 1 is vacuous here, not a leak.** The broker-label shuffle returned an identical
  n and mean, because broker identity enters the score only through the quality tilt and
  `broker_alpha.json` supplied no ranks in this run (tilt on == tilt off, −0.25pp both).
  Shuffling labels therefore changes nothing *by construction*. It is an uninformative
  control in this configuration, and the harness's ">±0.3pp = leak" flag misfires on it.
  Null 2 (date shift) did separate: −1.49pp against the real −0.83pp.
- **Coverage is 59 sessions in ONE regime** (mid-May to mid-August 2026). n=162 at the
  design point is below the declared 250. By §6.7 that is itself a "report the point
  estimate and explicitly do not ship" condition. A longer gross history could move the
  number; it would cost roughly 340 Sectors credits per additional quarter, and nothing
  here suggests the sign would flip.
- **BREN, PTRO, CUAN and DSSA are all excluded from the test by design** — they moved
  inside the last two weeks, so no 10-day forward return exists yet. The anchoring check
  reports that exclusion rather than a miss. The four names remain the *motivation* for
  the board and are not evidence for or against it.

### What survives, and is worth keeping

1. **The universe fix.** Independent of any scoring rule, and the largest real defect
   found: the panel reproduced Sectors' top-10-by-value on 4 of 49 days, and DSSA had
   been top-10 since 2026-06-03 while invisible to every board. Now value-derived and
   dated; DSSA appears on 36 of 40 sessions.
2. **The gross partition.** One-sidedness fails as a *predictor*; it remains the only
   thing that tells an accumulator from a market maker *descriptively*, which is what the
   board displays.
3. **The tape module.** Second-resolution microstructure, the sweep-vs-slice distinction,
   and the per-print client-domicile flag — all independent of this gate.
4. **The measurement corrections**, which would have silently corrupted anything built
   later: the window-scaling definedness floor, same-day-rank excluding the entry day,
   per-broker joint evaluation, churn priority, and `inventory_chart` day-1 flow.

---

## 7d. THE VERDICT — one-sidedness does nothing (2026-08-13, extended window)

Coverage doubled to **75 symbols × 109 sessions, 2026-02-18 → 2026-08-12**, 163,717 extra
broker-days, 427 further Sectors credits (770 total). Reconciliation exact again: 0 of
53,572 rows differ by more than 1%.

**The 23 names that were top-20 in Feb–May but are not in today's hot list were backfilled
too.** Testing the earlier quarter with only the current 53 would have been survivorship
bias, and it would have biased the result upward.

### Check 0 passes — this window can carry a verdict

| | mean excess, k=5 | n |
|---|---|---|
| momentum rule, full panel | +2.26pp | 2,862 |
| **momentum rule, this window** | **+0.93pp → lift +0.84pp** | 314 |

Pass condition was ≥ +0.50pp. Unlike the 59-session window, a known-good rule still works
here, so a failure by one-sidedness means something.

### The gradient runs the WRONG WAY — the decisive result

5d lift by one-sidedness threshold:

| `θ_osr` | absorb=today | absorb=window | L2 n |
|---|---|---|---|
| **≥ 0.70** | **+0.70pp** | **+0.90pp** | 630 |
| ≥ 0.80 | +0.16pp | +0.34pp | 341 |
| **≥ 0.90** | **−0.39pp** | +0.10pp | 186 |

**More one-sidedness is monotonically worse.** If the thesis were right this table would
slope the other way. §6.7 names exactly this — "the `osr20` gradient is flat or inverted →
the discriminator itself is wrong" — as the cheapest available refutation, and it fires.
Whatever small edge exists sits at the *loosest* threshold, which is closer to "a broker
bought persistently" than to one-sidedness at all.

### Design point, and the null that kills it

`osr20 ≥ 0.80`, net ≥ 20% ADTV, absorb=today, **n=341**:

| | lift |
|---|---|
| k=3 | −0.05pp |
| k=5 | **+0.16pp** |
| k=10 | +0.41pp |
| sub-periods (k=5) | −0.86 / **+3.15** / −0.71 / −0.99 → **1 of 4** |

One fold carries the entire average. And the null:

| control | lift | gap vs real |
|---|---|---|
| **date shift (strict)** | −0.06pp | **0.22pp — inside the ±0.3pp tolerance** |
| universe-only random | −0.24pp | 0.40pp |
| broker-label shuffle | +0.16pp | 0.00pp — *vacuous, see below* |

**The real result is not distinguishable from its date-shifted null.** +0.16pp against
−0.06pp is a 0.22pp gap where the declared tolerance is ±0.3pp. The events carry no
information beyond what random date alignment produces.

### Verdict

| check | result |
|---|---|
| 1. 5d lift ≥ +1.2pp | **FAIL** +0.16pp |
| 2. ≥3 of 4 sub-periods | **FAIL** 1/4 |
| 3. 10d lift ≥ 3d lift | PASS (+0.41 vs −0.05) |
| 5. ≥7 of 9 grid cells positive | **FAIL** 6/9 |
| n ≥ 250 | PASS (341) |
| **check 0 — window usable** | **PASS (+0.84pp)** |

**GATE FAILED on a window that was verified usable. One-sidedness is refuted.**

The correct characterisation is **"it does nothing"**, not "it inverts" — the −0.83pp from
the 59-session window was a regime artifact, and the honest number is **+0.16pp,
indistinguishable from zero and from its own null**. Trade-plan integration stays
cancelled. The board remains descriptive.

### Two things worth carrying forward

1. **`absorb_mode=window` beats `absorb_mode=today` at every single grid point** (+0.90 vs
   +0.70, +0.34 vs +0.16, +0.10 vs −0.39). The instinct in §6.1 — that the same-day form
   was the better one because it let BREN through — was wrong. Neither is good enough to
   ship, but if this is ever revisited, start from `window`.
2. **Null 1 remains vacuous** and this is now a harness defect worth fixing rather than a
   caveat worth repeating: broker identity enters the score only through the quality tilt,
   `broker_alpha.json` supplied no ranks in either run (tilt on == tilt off, +0.25pp
   both), so shuffling broker labels changes nothing *by construction*. Either wire the
   broker-alpha ranks in or drop the control — as it stands it always agrees with the real
   result and can never fail.

---

## 8. Slicing is not spoofing

A distinction that decides what is buildable, and it is worth being precise about because the two
get used interchangeably.

**Order slicing** — breaking one large order into many small ones to disguise size. Leaves its
fingerprint in **executed** trades (§3.4, §3.8). Fully historical, fully validatable, ships in
this board.

**Spoofing** — displaying size with intent to cancel. Requires order placements and cancellations.
**No feed on this tier carries them historically.**

**One historical instrument does speak to displayed orders, weakly.** `price-table` returns
volume-at-price split by aggressor for a **past** date — `{price, buy_volume, sell_volume,
buy_freq, sell_freq}`:

```
absorb_at(p) = buy_volume(p) / (buy_volume(p) + sell_volume(p))
ticket_at(p) = buy_volume(p) / buy_freq(p)        average clip working that level
```

Heavy volume at the prior day's high with a high `absorb_at` says the offer was real and got
eaten; price passing through a level on thin volume says it was pulled or never there. **That
is consistent with a pulled order but does not observe one**, and must be labelled weak
wherever it is shown.

Two API traps, both re-verified 2026-08-13: `date` is **required** (422 without it), and the
bundled MCP tool declares `code` only, so the MCP `price-table` tool can never succeed — call
REST directly. Do not read the MCP schema as evidence that the endpoint is code-only; it is
not.

What is available live, and only live: `order-book` per-level `freq` (the order *count* at a
price — the only field that separates one whale order from 800 retail orders) and `order-queue`.
Both degrade to nothing outside session hours.

Phase 2 therefore captures forward and **must not enter the score**:

```
pull_event = wall present at snapshot k, absent at k+1, with NO trade printing
             at that price — joined against running-trade after the close
```

That join is the actual spoof definition, and it is why the module can only ever be evaluated
forward. Persisted to git-ignored `data/book/` — the repo is public and the feed is licensed, same
rule as `data/tape/`.

**The bridge.** Once Phase 2 has ~40 sessions, `pull_event` becomes a **label** and the §3.8 tape
features become **predictors that are available historically**. Fit label ← features on forward
data, then apply the result backwards over the tape we already own. That is the only honest route
from here to retrospective spoof inference — short of the tier upgrade that unlocks
`/batch/order-book` with `date` + `time`, which would solve it outright and is worth pricing
before committing to the 40-session wait.
