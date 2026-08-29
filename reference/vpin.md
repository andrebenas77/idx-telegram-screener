# VPIN — order-flow toxicity on IDX. Framework.

**Written 2026-08-19, BEFORE the code.** Thesis #8. Seven have failed — see `accumulation.md`,
`chaser.md`, `lift.md`/`lift-results.md`. The pass bar is declared here so it cannot move.

Inference discipline is inherited wholesale from `lift.md` §7 (30-day date blocks, 10th/90th
bands, `n_blocks` beside every interval, check 0) and is not restated.

## 1. Why this one is different from the previous seven

All seven prior theses re-sliced **the same daily broker panel**. That is the single best reason
to expect an eighth failure. VPIN is the first proposal that reaches for a **different dataset**:
intraday volume on a volume clock. Whether that new data carries information is exactly what
stages V0–V1 decide.

## 2. The measure

Volume clock: partition the session's traded volume into buckets of equal size `V`, ignoring
calendar time, so a busy hour gets the same attention as a quiet one. Within each bucket classify
volume as buy- or sell-initiated. Then

    imbalance_j = |V_buy,j - V_sell,j| / V
    VPIN        = mean(imbalance_j) over the trailing n buckets

Three classifications are computed, deliberately:

| arm | how | cost |
|---|---|---|
| **TR-VPIN** | the vendor's real `type` aggressor flag on every print | 65-300 pages/stock-day |
| **BV-VPIN** | Bulk Volume Classification: `V_buy = V·Φ(ΔP/σ_ΔP)` | free (bars on disk) |
| **tick-VPIN** | plain tick rule on bar closes | free |

`Φ` is `0.5·erfc(-x/√2)` from `math` — no scipy, and none is available (repo is stdlib-only).

## 3. The prior, declared: this is probably a volatility proxy

Andersen & Bondarenko's critique targets exactly the cheap arm, and it is mechanistic rather than
statistical:

- **BVC misclassifies more as volatility rises, which mechanically inflates the imbalance — and
  the imbalance IS VPIN.** So BV-VPIN predicts volatility *by construction*.
- BVC is **inferior to a plain tick rule** benchmarked against true aggressor data.
- **BV-VPIN and TR-VPIN can move in opposite directions.** The classification choice flips the
  sign — the same species of fork that killed earlier theses.

So the null here is not "VPIN does nothing". It is **"VPIN is realized volatility with extra
steps"**, and the design must be able to tell those apart before anything else happens.

## 4. Data, and the resolution/history tradeoff

**The repo's documented "~114 session cap" on `multi_time_chart` is wrong** — it was inferred from
`timeframe=5` alone. Retention is tiered by timeframe (measured on BBCA, 2026-08-19):

| `timeframe` | sessions | true bars/session | ~buckets/day | 30-day blocks |
|---|---|---|---|---|
| 5 | 116 (~6mo) | 68 | ~50 (textbook VPIN) | 3 |
| **15** | **240 (~12mo)** | **24** | **~24** | **8** ← primary |
| 60 | 449 (~24mo) | 8 | ~8 | 14 ← power check |

There is no comfortable end of this tradeoff. `timeframe=1` makes it worse, not better: denser
bars but history below 6 months.

**Every bar is returned exactly twice, byte-identical** (BBCA 2026-05-25: 136 rows, 68 distinct
timestamps, volume ratio exactly 2.00). `intraday_lib.parse_payload()` already dedupes on
`(date,hhmm)`; that guard is load-bearing for the volume clock, because a doubled volume series
silently relocates every bucket boundary.

Exclude the **08:55 pre-opening auction bar** from the clock. It is frequently the session high or
low and is a single uncontested print, so it anchors a bucket on a price no continuous trading
occurred at. Include it only when reconciling against the daily panel.

### 4a. A declared fork: which sigma feeds BVC

BVC needs `sigma` for `Phi(dP/sigma)`. Two defensible choices, and they are NOT equivalent:

- **session sigma** (implemented): SD of bar price changes within the session being classified.
- **rolling sigma**: SD over a trailing multi-session window.

This matters more than it looks, and it is declared here rather than discovered afterwards.
Andersen-Bondarenko's mechanism is that *BVC error rises with volatility*. Session sigma
standardises `dP` within the session, which partly NEUTRALISES that mechanism — a violent day
gets a correspondingly large sigma, so `dP/sigma` does not blow up. Rolling sigma leaves the
mechanism intact, because a violent day is divided by a calm window's sigma.

So session sigma is the *charitable* implementation and rolling sigma is the *faithful
reproduction* of the estimator under attack. **Session sigma is primary** (it is the better
estimator and the one a screener would ship); rolling sigma is a declared robustness arm. If the
two disagree, that disagreement IS the Andersen-Bondarenko effect measured directly on IDX, and
it should be reported as the finding rather than resolved by picking the nicer number.

## 5. Pass bar

**V0 — MEASUREMENT. The kill switch, and the only fully-powered stage.**
V0 is a correlation between measures computed on the *same* ticker-days, so it does not depend on
the calendar-block count that limits everything downstream. That asymmetry is why it runs first.

~30 ticker-days spanning quiet and violent sessions, including BREN 2026-08-11/12 and PTRO
2026-08-11 (already on disk, already case-studied in `accumulation.md` §7b).

> **KILL** if `Spearman(BV-VPIN, TR-VPIN) < 0.6`, or if the two disagree in sign on the violent
> sessions.
> **KILL** if BV-VPIN correlates more strongly with realized volatility than with TR-VPIN. That
> is Andersen–Bondarenko reproduced on IDX, and it closes the question permanently.

**V1 — THE CONTROL, built before any result is read.** The central lesson of thesis #7: *a control
anti-matched to the treatment manufactures a pass.* There, the pre-registered price twin produced
Δπ = +0.067 against a +0.06 bar — a clean pass — and it was entirely an artifact of the twin
selecting bad days.

So, always reported side by side:
- **volatility/volume-matched control** — same calendar day, same realized-vol and dollar-volume
  decile, zero order-flow information. The `NOTTWIN` analogue, and precisely what the literature
  predicts VPIN collapses into.
- **unconditional baseline** — because "beats the control" is uninterpretable without knowing
  whether the control is any good.

> **KILL** if Δ vs the matched control straddles zero.

**V2 — THE TEST.** Same functional as #7 so results are comparable:
`pi = P(hit +2.0 ATR before -1.5 ATR, enter next open, max hold 30d)`, counted from empirical
paths on **adjusted** bars, never `(I-Q)^-1 R`. Driftless null 0.4286.
Test VPIN as a **level** (top decile) *and* as a state **age** — #7 found persistence and
predictiveness are inverted, so duration must not be assumed to help.

> **KILL** if the edge sits inside round-trip cost. In #7 the best cell was +28bp against 30-60bp.

**V3 — board.** Observation mode only, and a row whose Δ band straddles zero **greys out**.

## 6. Deliberately absent

- **The HMM.** Baum-Welch maximises emission likelihood; nothing in that objective makes a state
  "Accumulation". On price and volume a Gaussian HMM recovers volatility regimes, and naming the
  low-vol state "accumulation" is vocabulary, not discovery. It adds no information — it is a
  smoother over observables already refuted seven times. Revisit only if VPIN survives V1, and
  then only with **filtered** probabilities (forward-backward smoothing uses future data and is
  look-ahead), explicit label-switching handling, and a benchmark against a 4-line rule classifier.
- **PIN** (the original Easley-O'Hara MLE). Numerically fragile, needs an optimiser this repo has
  no dependency for, and the volume-clock version is the whole point.
- **Anything requiring order placements or cancellations.** No feed on this plan carries them
  historically; `batch-order-book` accepts `date`/`time` but returns 402.

---

**RESULTS: see [vpin-results.md](vpin-results.md)** — run 2026-08-19, n=26. Verdict: **KILL at V0**. BV-VPIN vs true toxicity -0.028; vs realized volatility +0.433. Andersen-Bondarenko reproduced on IDX. V1-V3 not run; HMM stays deferred.
