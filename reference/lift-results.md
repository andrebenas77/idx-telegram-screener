# RESULTS — run 2026-08-19. Verdict: UNDERPOWERED, and the gradient runs BACKWARDS.

Appendix to `lift.md`. Panel 159 symbols × 475 sessions (2024-08-15 → 2026-08-14). Zero API
calls. Produced by `lift_probe.py` (K1) and `lift_test.py` (K2/K3/K5). Result JSONs carry the
panel fingerprint.

## 1. The structural limit, found first and binding on everything

The 250-session cohort burn-in consumes 251 of 475 sessions, leaving 224 usable. At the
pre-registered 30-day block length that is **8 independent calendar blocks**, against a declared
inferential floor of 15. **Nothing in this study is inferential. Every band below is
descriptive.** This is a property of the panel, not of the thesis, and more computation cannot
fix it: Invezgo `inventory_chart` has a hard 2-year horizon, so the panel is already at maximum
depth.

## 2. K1 — occupancy PASSES its floor, but the continuation ladder is the finding

JOINT base rate **22.6%** (7,710 of 34,081 ticker-days), 5,555 onsets, median run 1, **max run
7**. The independence product of the two marginals is 24.8%, so the zero-sum identity produces
only mild negative dependence — the state is well occupied and the feared base-rate collapse did
not happen. k≥3 draws on 144 distinct tickers and 8 blocks, clearing the pre-registered floor.

P(reach k+1 | reached k):

| state | k=1 | k=2 | k=3 | k=4 | k=5 | max run |
|---|---|---|---|---|---|---|
| **JOINT** | **27.7%** | **27.8%** | **30.8%** | **29.5%** | **33.3%** | **7** |
| A_ONLY | 35.0% | 40.7% | 43.6% | 47.3% | 55.1% | 22 |
| C_ONLY | 29.6% | 33.7% | 37.4% | 48.1% | 55.4% | 16 |
| TWIN | 34.7% | 36.5% | 41.9% | 38.5% | 48.8% | 19 |

**JOINT is flat; every other state rises.** This matters more than it looks, because of §3a of
the framework: a pooled mixture across heterogeneous tickers is *guaranteed* to produce a rising
continuation rate even when every individual ticker is memoryless. The rise in A_ONLY / C_ONLY /
TWIN is therefore partly or wholly that mathematical artifact. JOINT alone fails to rise
**despite** the upward-biasing force, which implies the within-ticker joint-lift process is if
anything anti-persistent. **Joint lifting does not beget joint lifting.** Its longest run in two
years is 7 sessions, against 22 for accumulators alone.

## 3. K2 — first passage. Unconditional pi = 0.443 (n=30,139); driftless null 0.4286

pi(k) = P(+2.0 ATR before −1.5 ATR, enter next open, max hold 30d), on adjusted bars.

| state | k=1 | k=2 | k=3+ |
|---|---|---|---|
| JOINT | 0.468 (n=4,941) | 0.458 (n=1,366) | 0.435 (n=536) |
| A_ONLY | 0.447 | 0.439 | 0.423 |
| C_ONLY | 0.448 | 0.428 | 0.402 |
| ANY | 0.446 | 0.448 | 0.447 |
| TWIN | 0.434 | 0.410 | 0.411 |

### 3a. The benchmark trap, and how it was caught

Against the **price-only twin**, JOINT looked like a pass: Delta_pi = +0.041 / **+0.067** /
+0.021, and the k=2 figure clears the pre-registered +0.06 bar with a band excluding zero.

**That was a false positive.** The twin is itself **−0.019 / −0.046 / −0.049 against
unconditional** — it selects BAD days, so everything beats it. The twin is a *redundancy*
control (does broker data duplicate price?) and was mistakenly read as a *performance*
benchmark. Conflating those is how a null becomes a finding.

Worse, JOINT and the twin are **anti-matched**, not matched: P(twin | JOINT) = 29.5% against a
37.5% base rate. Joint-lift days close WEAK. So "JOINT beats the twin" is partly just
short-horizon reversal — strong-close days mean-revert — which needs no broker data at all.

### 3b. The correct controls

Delta_pi paired on calendar day, 30-day moving-block bootstrap, 80% bands:

| comparison | k=1 | k=2 | k=3+ |
|---|---|---|---|
| **JOINT vs unconditional** | **+0.022** [+0.011,+0.034] | **+0.016** [+0.003,+0.029] | **−0.027** [−0.041,−0.012] |
| NOTTWIN (close BELOW midpoint, zero broker data) | +0.006 [+0.000,+0.011] | +0.013 [+0.008,+0.019] | +0.010 [−0.002,+0.022] |
| **JOINT net of NOTTWIN** | **+0.016** | **+0.003** | **−0.037** |

NOTTWIN is the price-matched control: it isolates the reversal effect available from OHLCV
alone. Net of it, the broker state is worth **+0.016 at age 1, +0.003 at age 2, and −0.037 at
age 3+**.

**The age gradient runs backwards, and does so more sharply against the correct control.** This
is the second time a gradient has run backwards in this repo — one-sidedness did the same
(+0.70 at θ=0.70, +0.16 at 0.80, −0.39 at 0.90). More of the thing is worse.

## 4. K3 — decomposition. "Both cohorts" is decorative

ANY (either cohort net buying, identity ignored) returns +0.005 / +0.006 / +0.001 against
unconditional — flat and near zero at every age, but crucially **never negative**. JOINT is
better at k=1 and materially WORSE at k=3+ (−0.027 vs +0.001). The pre-registered kill
condition — joint indistinguishable from flow-only — is met at k=2 (+0.016 vs +0.006, inside
the bands). **The "both cohorts take the offer" framing adds nothing that "some institutional
cohort is buying" does not, and at long ages it actively subtracts.**

## 5. K5 — cost

ATR mix is comparable across arms (JOINT mean ATR% 6.04% vs 5.78% unconditional), so the cost
hurdle is not distorted by a volatility mix — this was raised as an objection and is answered.
On a ~5% ATR name a 30–60bp round trip is a 0.06–0.12 ATR hurdle. The *incremental* edge at
k=1, net of the price-matched control, is 0.016 × 3.5 = **+0.056 ATR, roughly 28bp** — against
30–60bp of the very spread you pay to chase people lifting the offer. **Break-even at best, and
only at age 1, which is not the thesis.**

Note the trap in the raw numbers: unconditional pi = 0.443 already implies E = +0.05 ATR, so the
barrier system has mild positive expectancy on this universe by itself. Quoting a state's raw pi
against cost would credit the signal with the universe's own drift.

## 6. Independent red-team (DeepSeek v4-pro), and what it changed

Two objections were substantive and are adopted:

- **"Underpowered is not refuted."** Correct, and the verdict wording reflects it. With 8 blocks
  this study cannot confirm anything; it can only report that the point estimates run against
  the thesis at every age.
- **k≥3 may be a survivorship artifact.** Conditioning on a run surviving 3 days may select runs
  in which the move has NOT yet happened, since a large move could break the state. This is an
  unresolved alternative explanation for the k≥3 negative and is not dismissed. It does not
  explain the flat continuation ladder or the k=2 collapse.

One objection was tested and rejected: the claim that the twin is the correct control and that
JOINT beating it by +2 to +6pp saves the thesis. NOTTWIN was built to adjudicate exactly that.
The twin is anti-matched to JOINT, so beating it measures reversal, not flow. Against the
properly matched price control the k=2 edge collapses from +0.062 to +0.003.

## 7. Standing conclusion

**Seven theses have now failed.** The literal question — *when both cohorts keep taking the
offer day after day, what are the odds price goes up?* — has this answer on this panel: **the
odds do not improve with age; they decline, and past age 2 they fall below a random day in the
same universe.** The mechanism is consistent with the momentum board's own finding that
RVOL ≥ 3.0 inverts: when the patient money and the fast money are both already buying, there is
no one left to buy.

What is NEW and worth keeping, unlike the previous six:

1. **The joint-lift state is genuinely distinct from price** — P(twin | JOINT) = 29.5% against a
   37.5% base rate, agreement only 53.2%. This is not momentum relabelled, and it is the first
   flow feature here of which that can be said.
2. **It carries a small positive increment at age 1** (+0.016 net of the price-matched control,
   band clear of zero) — real, but inside trading cost.
3. **The benchmark lesson, which is the durable output.** A control that is ANTI-matched to the
   treatment manufactures a pass. Build the matched control and the unconditional baseline
   BEFORE reading any delta. Had this run stopped at the pre-registered twin comparison it would
   have reported +0.067 at k=2 as a clean pass of the +0.06 bar.

**Do not revisit joint lifting without a longer panel.** The 8-block ceiling is structural.

---

## 8. Addendum — the marginal cohort states, and the persistence/predictiveness inversion

Run 2026-08-19, same harness. "Accumulator state" has two readings and both were tested:
`A_ONLY` = (1,0), accumulators buying while chasers are NOT (conditional — it excludes every
joint day); `A_ANY` = accumulators buying regardless of chasers (the marginal, = A_ONLY + JOINT).

**Delta_pi net of the price-matched control (NOTTWIN), i.e. what the broker data adds:**

| state | k=1 | k=2 | k=3+ |
|---|---|---|---|
| JOINT | **+0.016** | +0.003 | −0.037 |
| A_ONLY | −0.002 | −0.012 | −0.027 |
| A_ANY | +0.004 | −0.006 | −0.008 |
| C_ONLY | −0.006 | −0.028 | **−0.064** |
| C_ANY | +0.002 | −0.003 | −0.018 |
| ANY | −0.001 | −0.007 | −0.009 |

**Net of price, there is exactly one positive cell in the entire table: JOINT at age 1.**
Every cohort state, on both readings, is flat at age 1 and negative thereafter. The
accumulator state — the one the whale-accumulation board was built on — adds nothing.

### 8a. The inversion, which is the real result

| | continuation ladder | max run | net-of-price edge |
|---|---|---|---|
| A_ONLY | 35.0 → 40.7 → 43.6 → 47.3 → 55.1% (**rising**) | **22** | −0.002 / −0.012 / −0.027 |
| C_ONLY | 29.6 → 33.7 → 37.4 → 48.1 → 55.4% (rising) | 16 | −0.006 / −0.028 / −0.064 |
| JOINT | 27.7 → 27.8 → 30.8 → 29.5 → 33.3% (**flat**) | **7** | **+0.016** / +0.003 / −0.037 |

**Persistence and predictiveness are inverted.** The accumulator state carries the textbook
order-splitting signature — long runs, rising continuation, a 22-session maximum — and has
**zero** forward edge. The joint state has no persistence at all and carries the only positive
increment in the study.

This closes out the accumulation board mechanically rather than merely recording its failure.
That board scored sustained one-sidedness (`osr`, `softrun`) — i.e. exactly the A_ONLY
persistence signature. **The footprint is real and measurable; it simply does not predict.**
A whale working a large order leaves a long, one-sided, persistent trail, and by the time the
trail is long enough to detect, the information in it is gone. "More of the thing is worse"
now has a mechanism, not just a gradient.

### 8b. Confirmation that the twin comparison was pure price

| comparison vs TWIN | k=1 | k=2 | k=3+ |
|---|---|---|---|
| **NOTTWIN** (zero broker data) | +0.025 | +0.060 | +0.063 |
| A_ANY | +0.029 | +0.055 | +0.051 |
| ANY | +0.024 | +0.054 | +0.052 |

Every broker state's "edge over the twin" is statistically indistinguishable from what
`NOTTWIN` — a single line of OHLCV arithmetic — delivers on its own. The entire twin column
was the short-horizon reversal effect and contained no broker information whatsoever. This is
the cleanest available demonstration of the anti-matched-control trap in §3a.

### 8c. One asymmetry worth keeping

`C_ONLY` at age 3+ is the worst cell in the study, **−0.064 net of price**. Sustained
chaser-only buying with the accumulators absent is actively harmful — consistent with the
chaser definition itself (they arrive after the move), so a long chaser-only run is late-stage
demand with no patient bid underneath it. That is the closest thing here to a usable
*negative* screen, and it is the one direction the study did not pre-register.
