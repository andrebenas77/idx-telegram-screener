# The chaser framework

Who is on each side of a trade, whether they had arrived yet, and whether that predicts
anything. Written **before** the code, so the pass bar cannot move after the numbers land.
Same posture as `accumulation.md` and `scoring.md`.

The strategy in one line: **buy the name the chasers have not found, and let them be your
exit.**

---

## 1. Why this is not a fifth run at the same idea

Four theses have now failed the +1.2pp gate:

| thesis | result |
|---|---|
| quiet accumulation | −0.04 / +0.01pp |
| net-persistence + size floor | −0.06pp, n=8,675, nulls matched it to 0.03pp |
| coalition size floor | marginal events flipped sign across the grid |
| one-sidedness (`osr`) | +0.16pp, null-equal, **gradient ran backwards** |

Every one asked the same question: *is a broker's net flow directionally predictive?* The
answer is no, consistently, across two years and multiple parameterisations.

This asks a different question — **not how much was bought, but by whom, and whether they
were early or late.** The distinction matters because the underlying quantity is different:
those four were all functions of `net_value`; this is a function of *broker identity*
crossed with *timing*. It is also the first hypothesis here with a strong prior in its
favour, from §2.

---

## 2. The one strong prior: broker personality is real

Ranking brokers by whether their flow aligns with price moves, then splitting the panel in
half by date:

| | value |
|---|---|
| Spearman rank correlation, year 1 vs year 2 | **0.821** |
| Pearson on raw scores | 0.869 |
| top-10 chasers still top-15 a year later | **8 / 10** |
| bottom-10 contrarians still bottom-15 | **8 / 10** |

AK ranked 1st in both halves (+92.5bp then +100.7bp). ZP 7th in both. DP 8th in both.
CC 9th then 10th. BK 4th then 6th.

**This is the most stable measurement in the whole project**, and it costs nothing — the net
panel alone, no gross partition. It is the load-bearing assumption of everything below: if
broker behaviour were not persistent, a trailing estimate could not be used to classify a
future event, and the design collapses at step 3.

### The chasers are foreign institutions

| broker | | same-day score | t | n |
|---|---|---|---|---|
| AK | UBS | **+97.0bp** | 29.4 | 45,531 |
| BB | Verdhana | +84.1bp | 19.5 | 22,244 |
| RX | Macquarie | +84.0bp | 15.7 | 12,422 |
| BK | J.P. Morgan | +54.7bp | 17.2 | 39,572 |
| KZ | CLSA | +53.8bp | 11.9 | 17,134 |

Only seven brokers score positive at all.

### The retail books are the opposite

The hypothesis that started this was that XL buys high and sells low. It is contradicted,
and not marginally:

| broker | score | t | n | rank of 63 |
|---|---|---|---|---|
| XL | **−114.6bp** | −33.3 | 60,912 | 27 |
| XC | **−173.7bp** | −40.3 | 42,003 | 46 |
| YP | −156.1bp | −47.3 | 60,270 | 41 |
| KK | −172.4bp | −33.8 | 30,628 | 45 |
| XA | −132.3bp | −12.2 | 10,960 | 31 |

XL's mean excess return is **−0.33%** on its net-buy days and **+0.82%** on its net-sell
days. It is negative in all 14 of the names this study was aimed at, and *most* negative in
exactly the PP/Bakrie names the claim was about (PTRO −349bp, BRPT −347, AMMN −345,
BUVA −340). Only 37.8% of its net-buy days were positive-excess days. It buys weakness.

**All five retail books sit in the contrarian half. The folk model is inverted.**

---

## 3. Defining a chaser

### 3.1 Why the same-day measure is the wrong primary

```
chase_score(b) = mean excess return on net-BUY days − mean excess return on net-SELL days
```

This **cannot separate "chose to buy strength" from "crossed the spread"**. A broker resting
on the bid gets filled as price falls — that is an accounting identity of passive execution,
not a view. Retail books are overwhelmingly passive, so this measure may be doing nothing
but re-discovering execution style, and would classify every passive book as contrarian by
construction.

It is retained as a robustness check, not as the primary.

### 3.2 Primary — lateness

```
lateness(b, t) = corr( net_flow_b(s, i) , trailing 5-day excess return of s BEFORE day i )
```

Positive ⇒ the broker tends to arrive *after* the move has already happened.

This measures **timing**, not execution. Whether you cross the spread has no bearing on
whether you show up before or after a five-day run, so the passivity confound does not
apply. It is also the literal operationalisation of "not in town yet".

### 3.3 The simplicity check — `is_foreign`

Every one of the five chasers is a foreign institution. If the registry flag
(`sectors_client.brokers()` → `is_foreign`) sorts outcomes as well as an estimated score,
**use the flag**: zero estimation error, no trailing window, nothing to re-fit, and it
cannot drift. This is a live possibility, not a courtesy comparison.

### 3.4 Point-in-time estimation — non-negotiable

```
score(b, t) estimated on the trailing 250 sessions, STRICTLY < t, min 250 observations
re-estimated monthly
```

Classifying a mid-panel event with a full-sample score is lookahead and would manufacture an
edge out of nothing. §2's 0.82 persistence is what makes a trailing estimate viable.

**Gate:** report the correlation between the trailing estimate and the full-sample estimate.
If it collapses, the design fails here and nothing downstream is worth running.

**This must be asserted in code, not by inspection** — no event may consume a score whose
estimation window touches or postdates its own date.

---

## 4. The signal

```
chaser_presence(s, i, W) = Σ_{b ∈ chaser cohort} net_flow_b(s, i, W) / ADTV(s, i)
```

Low or negative ⇒ the chasers have not arrived. High ⇒ they are here.

> **A truncation that happens to help.** The net panel carries only the **top-20 brokers per
> symbol**, so a chaser missing from that list has *unknown* flow, not zero. For this signal
> that is the correct behaviour rather than a defect: "not among the twenty most active
> brokers in this name" *is* what not-in-town means operationally. It still makes
> `chaser_presence` coarser than it looks, and that must be stated on the page.

---

## 5. The overlay test

Momentum events have chasers present almost by definition — `rvol5 ∈ [1.5, 3)` and
`rsi ≥ 55` select names already moving, which is exactly where chasers show up. So the
question sharpens to:

> **Among names that already qualify on momentum, do those the chasers have NOT yet found
> outperform those they already hold?**

Two hypotheses, **both declared now** so neither can be selected after the fact. They are
two ends of one tercile spread, not two independent attempts:

- **H1** — low `chaser_presence` momentum events outperform. You are early; they are your
  exit liquidity.
- **H2** — high `chaser_presence` outperforms. Their arrival confirms institutional
  conviction.

### 5.1 E3 — chaser arrival as an exit

The most actionable half. Added to the validated pair:

| | rule |
|---|---|
| E1 | stop touched |
| E2 | close below the 5-session low |
| **E3** | **chaser cohort net buying spikes above a threshold** |

Tested head-to-head through `trade_backtest.py` on one event set: E1/E2 against E1/E2/E3.
**E3 must improve the validated baseline** (n=915, 2y, 3 of 4 folds, +0.82pp), not merely be
positive alone.

---

## 6. Group lead-lag

Groups verified against a random-basket baseline of **0.262** mean pairwise return
correlation (300 draws of 20 names, sd 0.035), over 120 sessions:

| group | members | corr | verdict |
|---|---|---|---|
| **PP** | PTRO, BRPT, TPIA, CUAN | **0.649** | real, >4σ |
| **Bakrie** | DEWA, BUMI, AMMN | **0.685** | real, but DEWA–BUMI is 0.906 and AMMN attaches at only ~0.57 |
| **Trio** | BUVA, RAJA (+RATU pending) | **0.791** | real |
| ~~Specs~~ | ENRG, VKTR, BULL, BNBR, KOTA, JGLE | 0.342 | **not a group** — 84th percentile of the null. Dropped. |

```
group_ex_flow(s, i, W) = Σ_{m ∈ G, m ≠ s} net_flow(m, i, W) / ADTV(m)
```

**Measured against the group-relative return, never the raw one:**

```
resid_return(s, i, k) = excess_return(s, i, k) − mean over G of excess_return(m, i, k)
```

Members correlate 0.65–0.79, so testing raw returns would only rediscover "the complex went
up, so its members went up". The residual is the sole formulation that tests **rotation
within** the complex, which is what a lead-lag claim actually asserts.

> **Do not read a shared top-buyer list as a syndicate signal.** XL is the top net buyer in
> nearly every small and mid-cap in the panel, so its appearance across a group's members
> carries no group-specific information — it is a base-rate artifact of XL being the largest
> retail book and the market's default absorber. The genuinely group-specific overlaps are
> **ZP across PP** and **LG across Bakrie**.

---

## 7. Confound controls, declared in advance

**C1 — passivity.** Correlate each definition against `cost_gap` (`buy_avg / VWAP − 1`) from
the 109-session gross window, and against the tape's true aggressor split on a few names.
**If the primary definition correlates > 0.8 with passivity it is a re-description of
execution style**, and every result must be reported as such rather than as broker skill.
This is precisely why lateness is primary and same-day alignment is not.

**C2 — zero-sum.** Net flow across all brokers sums to zero each day, so value-weighted
scores must too. Chasers and contrarians are two sides of one coin: only *who is on which
side* can carry information, never the aggregate. Any result implying an aggregate edge is
a bug, not a finding.

**C3 — size.** The chasers are the large foreign desks and the contrarians mostly small
local books, so the score may be proxying broker size or client type rather than behaviour.
Re-run with brokers stratified into size terciles by total gross value.

---

## 8. Validation — the bar, declared before running

Baseline is **the momentum board's own event set** (+2.26pp at k=5 over the full panel), not
the top-20 universe. This is an overlay: it must improve a rule that already works.

**Check 0 first** (`accum_test.py` §6.0, reused): the window must be a regime where
`is_momentum` still earns ≥ +0.5pp lift. A failure voids everything after it.

Terciles by `chaser_presence`; forward excess at k = 3 / 5 / 10 with `entry_lag=1`.

**Pass requires all of:**

1. top-vs-bottom tercile spread **≥ +1.5pp** at k=5, **in the direction H1 declares**
2. the favoured tercile **≥ momentum unconditional mean + 0.5pp** — the overlay must *add*,
   not merely re-sort what is already there
3. spread positive in **≥ 3 of 4** sub-periods
4. both nulls within **±0.3pp**
5. **n ≥ 300** momentum events per tercile

**Nulls.** Shuffle scores across broker identities — this preserves the score distribution
while destroying the identity↔score link — plus the standard circular date shift.

> The broker-label shuffle was **vacuous** in the accumulation gate: identity entered the
> score only through a quality tilt that had no ranks loaded, so the null agreed with the
> real result by construction and could never fail. Here **identity is the feature**, so this
> control finally bites. That is a reason to trust this test more than the last one.

**Refutation — ship nothing:**

- spread **< +0.5pp**, or the **wrong sign**, or negative in **2+ sub-periods**
- **C1 shows the feature is just execution style**
- the trailing score does not track the full-sample score (§3.4)

In any of those cases, write it up beside the other four refutations and **stop. Do not
lower the bar and do not run a sixth variant.** Five failures on the same underlying data
is itself the finding: broker flow, however sliced, does not predict IDX returns, and the
only rule here that has ever survived a walk-forward is the momentum board's — which waits
for price confirmation.

---

## 8b. RESULTS — 2026-08-14

### What held up

**Broker personality is real and persistent.** Disjoint-halves Spearman, at the
definedness floor set in §8c:

| definition | Spearman | top-10 persisting |
|---|---|---|
| lateness (primary) | **+0.793** | 7/10 |
| same-day (robustness) | **+0.944** | 10/10 |

They agree in sign (+0.585 between definitions), so the primary is not contradicted by its
own check. Point-in-time assertions passed at all 11 as-of dates.

**C1 passed, with a real caveat.** Neither definition is *purely* execution style, but
passivity explains a meaningful share:

| | vs spread capture | vs buy-vs-mid |
|---|---|---|
| lateness | −0.513 | +0.496 |
| same-day | −0.457 | +0.396 |

All below the 0.80 refutation threshold, and directionally coherent — chasers capture less
spread and buy above the session midpoint, i.e. they pay up. Notably **XL is among the more
*aggressive* books** (−0.06% spread capture), so its contrarian score is *not* a passivity
artifact. That strengthens the reading that its contrarianism is real behaviour.

### The gate FAILED

| tercile by flow composition | n | k=3 | k=5 | k=10 |
|---|---|---|---|---|
| LOW — chasers not in town | 527 | +0.56 | **+0.78** | +2.44 |
| MID | 523 | +2.27 | +3.18 | +4.68 |
| HIGH — chasers already here | 529 | +2.30 | **+4.14** | +5.78 |
| baseline (all momentum events) | 1,579 | +1.71 | +2.71 | +4.27 |

| check | result |
|---|---|
| 1. \|spread\| ≥ 1.5pp | PASS −3.36pp |
| 2. favoured tercile ≥ baseline +0.5pp | PASS +4.14 vs +3.21 |
| 3. spread positive in ≥3/4 of available folds | **FAIL** 1/2 (+4.91, −1.01) |
| 4. identity-shuffle null within ±0.3pp | **FAIL +1.98pp** |
| 5. n ≥ 300 per tercile | PASS |

**Two conclusions, and the second matters more than the first.**

**H2, not H1.** The spread runs the opposite way to the hypothesis that motivated this
work. Momentum names the chasers had *already* found returned +4.14pp against +0.78pp for
names they had not. "Buy when the chaser is not in town" is contradicted on this data.

**But that result is not trustworthy either, because null 4 fails.** A cohort built from
*randomly shuffled* scores still produces a ±1.98pp tercile spread. Since identity is the
entire feature here, a null that large means the split is not isolating identity — it is
picking up something structural, most likely flow concentration (names where one broker
dominates versus names with diffuse flow), which would sort returns regardless of who that
broker is. Combined with 1 of 2 available folds, **the honest status is: no supported
signal in either direction.**

Per §8, **stopping here.** Two parameterisations have been run — a magnitude form and the
declared composition form — and a third would be exactly the fishing the rule forbids.

### The null earned its keep

The first implementation used raw `chaser_net / ADTV`, a MAGNITUDE measure, deviating from
the composition formula declared in §4. The identity shuffle caught it immediately
(−1.28pp on a random cohort), because cohort-net-over-ADTV is largely a proxy for total net
flow — the very quantity four earlier theses had already shown is not predictive.
Restoring the declared formula flipped the headline from H1 (+0.99pp) to H2 (−3.36pp),
which is a useful measure of how little either number was worth.

This is the control that was **vacuous** in the accumulation gate. Here identity is the
feature, so it bit — twice.

## 8b-ii. HIT RATE — the sixth failure (2026-08-14)

Everything above measured **mean excess return**. Hit rate — the probability the stock is
simply *up* — is a different statistic and had **never been tested here**. A signal can
have one without the other, so it was worth asking.

> Related but distinct: `broker_alpha.py` already refuted ranking **brokers** by hit rate
> (−2.3%/−2.8% against +2.3%/+2.1% for mean). That is a different question from whether
> composition sorts a **stock's** probability of rising, which is what follows.

### No gradient — the shape is an inverted U

Deciles by composition, k=5 hit rate: 39.9, 38.2, 63.1, 49.3, 66.7, 61.4, 60.8, 64.3,
62.3, **38.7**. Both extremes are the worst cells and the middle is best. Terciles agree:
MID beats HIGH on hit rate at every horizon while HIGH beats MID on mean — the two
statistics are being driven by different things (frequency vs tail size).

**Top-vs-bottom decile: −1.2pp at k=5, −8.0pp at k=3, −7.5pp at k=10** — the *wrong* sign
against the mean story. **Wilson bounds do not separate at any horizon.**

### The null is worse on hit rate than it was on the mean

300 identity shuffles:

| | real spread | null mean | null sd | exceedance |
|---|---|---|---|---|
| HIGH−LOW **hit rate**, k=5 | +6.85pp | **+3.69pp** | **6.30pp** | **30.7%** |
| HIGH−LOW **mean**, k=5 | +3.36pp | ~0 | 1.64pp | — |

**31% of random-identity shuffles produce a hit-rate spread at least as large as the real
one**, which sits at roughly the 69th percentile of its own null. Signal-to-null is ~1.1
against the mean's ~2.0. And the null is **not centred on zero** — a randomly relabelled
cohort averages +3.69pp — so even the sign is partly mechanical: sorting on any
gross-normalised weighted flow ratio sorts on flow structure regardless of who the brokers
are.

### It does not exist outside momentum names

Across **4,284** top-20 stock-days (2.7× the momentum sample), HIGH−LOW is +1.3pp on hit
and **−0.23pp on mean** — reversed and flat. D10−D1 is −1.3pp / −0.75pp. Baselines:
broad universe +0.77pp / 49.1% versus momentum events +2.71pp / 54.5%. **The momentum
filter, not the composition sort, is what carries the return.**

### Direction of arrival does nothing

Chasers arriving vs leaving (5-session change in composition): RISING − FALLING is
**−1.7pp hit, −0.56pp mean**, and the middle group is worst on hit while best on mean —
the signature of noise. The two interesting-looking 2×2 corners are both n<100 and are not
findings.

### Complexes cannot be tested at all

PP generates **21** momentum events, Bakrie **30**, Trio **15** — 66 in total, with tercile
cells of 2–22. Trio-HIGH prints +47.88pp on **n=2**. Flagged underpowered, reported as
nothing. No realistic amount of extra data fixes this inside Invezgo's 2-year horizon, so
the complexes stay on the board as **description only**.

### Verdict

**Sixth failure. Stopping.** After six searches on one dataset, a seventh is more likely to
surface something that survived the search than something real. The complexes, the broker
personalities and the universe fix remain worth publishing descriptively; none of it is a
signal.

## 8c. The definedness floor, and a gate that was the wrong test

**MIN_OBS was raised from 250 to 3,000**, and this nearly killed the primary definition
before it was diagnosed. Pooled over all 68 brokers, lateness persisted at only +0.123 —
because the top of its ranking was brokers with a few hundred observations (BR 928, IH 385,
TS 734), where a difference-of-means is pure noise. Sweeping the floor on disjoint halves:

| min_obs | brokers | lateness | same-day |
|---|---|---|---|
| 120 | 68 | +0.123 | +0.816 |
| 500 | 48 | +0.532 | +0.903 |
| 1,000 | 42 | +0.651 | +0.895 |
| **3,000** | **35** | **+0.793** | +0.944 |
| 6,000 | 28 | +0.788 | +0.936 |
| 8,000 | 25 | +0.774 | +0.960 |

It rises steeply and then plateaus from ~3,000, so the level is read off the knee rather
than chosen to clear a bar, and it is not knife-edge. This is the same definedness
principle as `accumulation.md` §3.1 — too few observations means *unscored*, never scored
neutral.

**§3.4's declared gate was the wrong test and was replaced.** "Does the trailing estimate
track the full-sample estimate" is inflated by overlap: the trailing window is a *subset*
of the full sample. It returned median Spearman **+0.845** for a feature whose genuine
out-of-sample persistence was **+0.12**. It would have waved a useless feature straight
through. The gate is now disjoint-halves persistence.

**The sub-period test is structurally limited.** A 250-session burn-in on a 476-session
panel leaves only 2 of 4 folds with any events. Empty folds are reported as unavailable,
never counted as failures — failing a rule for having been careful about lookahead would be
its own kind of error.

## 9. Known limitations, to be stated on the page

- **Top-20 truncation.** Chaser scores for small brokers are estimated only on the names
  where they were active enough to make the per-symbol top-20 cut — a selection effect.
- **RAJA carries a 5:1 split on 2026-07-16.** Raw prices show −80%; adjusted show +13.9%.
  Any output showing RAJA down ~80% is reading raw prices.
- **AMMN attaches weakly** to DEWA–BUMI (~0.57 vs 0.906). Its inclusion in the Bakrie group
  is tested explicitly, not assumed.
- **JGLE has only 59 of 109 gross sessions**, thinning any gross-dependent control for it.
- **RATU and VKTR** were absent from the panel entirely and are being added; RATU may be a
  recent listing with too little history to profile.
