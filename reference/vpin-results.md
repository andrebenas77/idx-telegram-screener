# V0 RESULTS — run 2026-08-19. Verdict: KILL. Bar-based VPIN is a volatility proxy on IDX.

Appendix to `vpin.md`. n = 26 ticker-days, 23 symbols, 2026-02-18 → 2026-07-16, stratified by
|daily return| from 0.00% to 7.75%. Produced by `vpin_validate.py`; result JSON carries the panel
fingerprint. **Total spend 3,082 requests (10.3% of the monthly quota) to close the question** — above the ~2,000 estimated, because the rerun refetched the truncated sessions and retried two that never returned usable prints.

## 1. The result

Spearman rank correlations across ticker-days, with Fisher-z 95% intervals:

| | ρ | 95% CI | |
|---|---|---|---|
| **BV-VPIN (session σ) vs TR-VPIN** | **−0.028** | [−0.411, +0.363] | indistinguishable from zero |
| **BV-VPIN (rolling σ) vs TR-VPIN** | **−0.124** | [−0.488, +0.277] | no better |
| tick-VPIN vs TR-VPIN | +0.559 | [+0.219, +0.778] | clear of zero |
| **BV-VPIN (session σ) vs realized vol** | **+0.433** | [+0.055, +0.703] | clear of zero |
| **BV-VPIN (rolling σ) vs realized vol** | **+0.569** | [+0.232, +0.783] | clear of zero |
| **TR-VPIN vs realized vol** | **−0.483** | [−0.733, −0.118] | clear of zero, **negative** |
| tick-VPIN vs realized vol | −0.296 | [−0.613, +0.103] | |

Both pre-registered KILL conditions fire, on **both** σ arms:

1. `Spearman(BV, TR) < 0.60` — it is −0.028, i.e. the cheap measure has *no* relationship to the
   quantity it claims to measure. Not weak. Zero.
2. BV-VPIN tracks realized volatility (+0.433) far more closely than it tracks true toxicity
   (−0.028).

## 2. Andersen–Bondarenko reproduced on IDX — all three claims

IDX gives what most markets do not: the vendor stamps the real aggressor side on every print of a
closed session. So the critique is directly testable here rather than argued.

1. **BVC-VPIN is a volatility proxy.** +0.433 with volatility, −0.028 with truth.
2. **BVC is inferior to a plain tick rule.** −0.028 versus +0.559 — the tick rule is not merely
   better, it is the difference between nothing and something.
3. **BV and TR move in opposite directions.** Against volatility their signs are literally
   opposite: **+0.433 vs −0.483**, both bands clear of zero.

## 3. The σ fork behaved exactly as pre-registered

`vpin.md` §4a, written before any data was seen, predicted that **session σ is the charitable
implementation** (it standardises ΔP within the day, partly neutralising the misclassification
mechanism) and **rolling σ is the faithful reproduction** of the estimator under attack (an
out-of-sample σ cannot adapt, so a violent day divided by a calm window's σ saturates Φ).

Moving from the charitable to the faithful estimator:

| | session σ | rolling σ | Δ |
|---|---|---|---|
| tracks realized volatility | +0.433 | **+0.569** | **+0.135** |
| tracks true toxicity | −0.028 | **−0.124** | **−0.096** |

**Both moved in the predicted direction.** The two σ choices correlate +0.816 with each other, so
this is not two different measures — it is the same measure with the A&B mechanism dialled down or
up. That is the mechanism measured as a dose-response, on a prediction registered in advance.

The same effect is reproducible in isolation, and is pinned in `vpin_lib.selftest`: a violent
bucket (ΔP = 10σ_calm) scores imbalance 0.9999 under rolling σ and 0.68 under session σ.

## 4. The finding worth keeping

**True order-flow toxicity on IDX is NEGATIVELY correlated with realized volatility: −0.483
[−0.733, −0.118].**

The sessions where informed flow is most one-sided are the *calm* ones. This is coherent with the
original whale-accumulation intuition — an informed buyer works quietly, and a violent session is
a crowd rather than an insider — and it is the exact opposite of what any volatility-based
toxicity proxy would report. That inversion is *why* the cheap arm fails: it is not a noisy
version of the right answer, it has the wrong sign.

Note this is measured on true aggressor data, not inferred. It is the first quantitative statement
in this repo about *who is informed* that does not depend on a classification rule.

## 5. What is NOT claimed

- **The tick rule is not rescued.** +0.559 has a CI of [+0.219, +0.778], which **includes 0.60**,
  so it cannot be said to clear the pre-registered bar. It was also a robustness arm, not the
  registered primary — treating it as a pass would be exactly the post-hoc promotion this repo's
  discipline exists to prevent. What can be said: *if* a cheap toxicity proxy is ever wanted, it
  should be the tick rule, never BVC.
- **VPIN itself is not refuted** — only VPIN computed from bars. TR-VPIN on true aggressor data
  looks sane and behaves interestingly (§4). It is simply unaffordable at panel scale: 65–300
  pages per stock-day against 30,000 requests/month.

## 6. A silent failure caught, and the guard added

Three sessions returned **truncated tapes that were cached as if complete**. `running_trade_all()`
breaks its page loop on any falsy payload, and the partial result is then written to the
per-(symbol,date) store, where it is indistinguishable from a complete session on reread.

This is not harmless. A truncated tape covers only the opening minutes, where flow is most
one-sided, so it scores as maximally toxic: TR = 0.975 / 0.780 / 0.794 against a clean-sample
median of 0.398. Including them dragged Spearman(tick, TR) from **+0.559 to +0.210** — enough to
change what one would conclude about the tick rule.

ADRO 2026-07-14 was the clearest case: 150 prints, every one stamped 08:58:00. On refetch it
returned 300 prints against ~14,241 expected, so the truncation is **systematic for these
sessions, not transient** — a refetch does not fix it.

`vpin_validate.truncated()` now checks the print count against the gross panel's own `buy_freq`
and **deletes the cache rather than reusing it**. The guard fired correctly on the rerun.

> **General rule for this repo: any paginated fetch that caches its result must validate
> completeness before writing.** A partial page-loop that exits cleanly produces data that looks
> finished forever after. Sits with the other silent failures in
> `reference-validation-discipline-idx`.

## 7. Standing conclusion

**Thesis #8 stops at V0.** The measurement does not survive, so the hypothesis was never tested —
which is the correct order and the reason V0 existed. Total cost 10.3% of one month's quota and
about a day, against a plan that would otherwise have run three more stages on an instrument that
measures volatility.

V1–V3 are **not run**. The HMM stays deferred: it was contingent on VPIN surviving V1, and VPIN
did not survive V0.

Reusable output: `vpin_lib.py` (50-check selftest — volume clock with boundary splitting, BVC via
`math.erfc`, tick rule with sign threading, both σ estimators, and a unit test that reproduces the
A&B mechanism deterministically), the costed tape sampler, the truncation guard, and 26 ticker-days
of true-aggressor tape now cached on disk.
