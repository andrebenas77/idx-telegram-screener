# Crowdedness scoring — the framework (your IP)

This screener measures **attention/crowdedness** — how loudly a ticker is being talked about across
your Telegram channels — **not** sentiment or direction. Everything below is deterministic and lives
in code (`scripts/build_screener.py`); the tunable numbers live in `reference/config.json`.

One **session** = one daily run (a trading day). Weekends produce no session, so all "ages" are
counted in **sessions**, not calendar days.

## Per-ticker signals (each day)

| Signal | Meaning |
|---|---|
| `posts` | Number of **messages** mentioning the ticker today (message-level, so one spammy post can't inflate it). |
| `channels` | Distinct channels mentioning it today — **breadth**. 8 channels beats 1 channel × 20 posts. |
| `share` | `posts` ÷ all ticker-posts today = % of the day's chatter. |
| `decayed_posts` | Recency-weighted posts over the validity window: each session's posts × `0.5^(age/halflife)`. Old chatter fades automatically. |
| `heat (z)` | Today's posts vs this ticker's own trailing average over the validity window (standardised). Is it unusually loud *for itself*? |
| `theme_age` | "Day N" — how many sessions the ticker's current unbroken run of chatter has lasted (resets after a quiet gap). Fresh (Day 2) vs late (Day 12). |

## The crowd score

```
crowd_now = 100 × ( 0.55 · norm(decayed_posts) + 0.30 · norm(channels) + 0.15 · norm(share) )
```
`norm(x)` = x ÷ the day's maximum across active tickers. Weights are in `config.json → weights`.

## Buckets (what the page shows)

| Bucket | Rule |
|---|---|
| **Most Crowded Now** | Top `top_n_crowded` by `crowd_now`, excluding expired names. Where the herd is *today*. |
| **Heating Up** | `heat (z) ≥ heating_min_z`. Tagged **NEW** if `theme_age ≤ heating_new_max_age` (fresh theme). |
| **Aging / Late** | In the crowded set **and** `theme_age ≥ aging_min_theme_age`, and *not* cooling. Consensus getting old. |
| **Cooling / Fading** | `posts_today < cooling_drop_ratio × peak(last 5 sessions)`. Takes precedence over Aging. |
| **Expired** | No mentions for `stale_after_quiet_sessions`+ sessions → drops off the active board. Enforces the ~2-week validity. |
| **Quiet — Under the Radar** | `liquid` names with ~no chatter (`recent5_sum ≤ quiet_max_recent_posts`) and nothing in the window. Your **contrarian watchlist**. |

## Tuning knobs (`reference/config.json`)

| Knob | Default | Effect |
|---|---|---|
| `fetch_lookback_hours` | 30 | How far back each run reads (24h + 6h overlap, de-duped). |
| `validity_window_days` | 10 | Max horizon (~2 weeks of sessions) treated as "current." |
| `decay_halflife_days` | 4 | Recency weight. Lower = old chatter fades faster. |
| `stale_after_quiet_sessions` | 4 | Quiet this long → **Expired**. |
| `theme_reset_gap` | 5 | Quiet gap that resets `theme_age` (a returning theme counts as new). |
| `weights` | .55/.30/.15 | Blend of decayed_posts / channels / share in `crowd_now`. |
| `top_n_crowded` | 20 | Rows in the Most Crowded table. |
| `heating_min_z` | 1.0 | Sensitivity of Heating Up. |
| `aging_min_theme_age` | 10 | When a crowded theme is called "aging." |
| `cooling_drop_ratio` | 0.5 | Today below this × recent peak ⇒ Cooling. |
| `quiet_max_recent_posts` | 1 | Liquid names at/below this over 5 sessions ⇒ Quiet. |

**How to tune for a faster/slower market:** if IDX themes feel *shorter* than 2 weeks, lower
`decay_halflife_days` (e.g. 3) and `validity_window_days` (e.g. 7) and `stale_after_quiet_sessions`
(e.g. 3). If they run *longer*, raise them. Nothing else needs to change.

## Matching rules (in `scripts/fetch_mentions.py`)

- A ticker is counted once per message (dedup within a post).
- Match on: `$CODE` cashtag (any case), bare **UPPERCASE** `CODE` as a standalone token, or a company
  **alias** from `tickers.csv` (case-insensitive).
- Tickers flagged `ambiguous=1` (words like `CUAN`=profit, `RAJA`=king, `FILM`, `WIFI`, `GOOD`,
  `BEST`, `BANK`, `AUTO`) match **only** via `$CASHTAG` to avoid false positives. After your first
  real run, review the leaderboard and flag any new offenders in `tickers.csv`.

## Price, volume & news (v2)

Each crowded ticker is paired with market data so you can see **whether the chatter is already
moving the stock, and whether the news is out** — the setup for a "sell on news" fade.

**Price/volume** (`scripts/fetch_prices.py`, Yahoo v8 `.JK`, the **last completed session** —
for a morning run that is yesterday's close):
- `Δ1d` = last close vs prior close (green/red).
- `Δ5d` = last close vs 5 sessions ago (short momentum).
- `RVOL` = last session volume ÷ its `vol_avg_window` (20-day) average. **Bold when ≥ `rvol_hot`** —
  a mention spike *with* a volume spike is real interest; without it, it's just talk.

**News** (`build/news-<date>.json`, top `news_top_n` crowded, context-only) — latest headline + link
per ticker; shown as a `news (N)` badge in the table and full cards in the **In the News** section.

**Signal** (derived, thresholds in `config.json`) — priority order:
| Signal | Rule | Read |
|---|---|---|
| **Distribution** | `Δ1d ≤ down_pct` and `RVOL ≥ rvol_hot` | Crowded but sold on heavy volume — the crowd is exiting. |
| **Confirmed / Late** | `Δ1d ≥ up_pct` and (news out **or** `RVOL ≥ rvol_hot`) | Crowded + up + catalyst → classic **sell-on-news** fade risk. |
| **Extended** | `Δ5d ≥ ext_pct` | Already had a big multi-day run — late to chase. |
| **Anticipatory** | small `\|Δ1d\|` and **no news yet** | Crowded, price quiet, catalyst not out — watch for the pop. |

Defaults: `up_pct 3`, `down_pct -3`, `rvol_hot 1.8`, `ext_pct 15`. All in `config.json`. If price or
news is missing for a ticker, its cells show "–" and it simply gets no Signal (never blocks a build).

## Broker-cohort flow (v3)

Chatter measures **retail attention**. Flow answers the question that actually matters: *who is on
the other side of it?* That pairing is the point — a crowded name with institutions buying is a
different trade from a crowded name with institutions selling.

v3 answers it at **broker level**. `scripts/fetch_brokers.py` makes one `/v2/broker-summary/` call
per crowded ticker (1 credit, up to 14 days) and gets every broker's buy/sell/net/lots/frequency for
each day. Everything below is derived from that single payload → `build/brokers-<date>.json`.

### Who counts as what

Groups are **behavioural**, defined in `reference/brokers.csv`:

| Group | Codes |
|---|---|
| `institutional` | BK JP Morgan · AK UBS · CC Mandiri · BB Verdhana · KZ CLSA · **RX Macquarie** |
| `hnw` | YU CGS · CP KB Valbury · SQ BCA · HP Henan Putihrai |
| `scalper` | MG Semesta Indovest |
| `retail` | XL Stockbit · XC Ajaib · YP Mirae Asset |

Sectors' own `cohort` field is **deliberately ignored** — it is a licensing category and labels YP
(the largest retail book on IDX) and MG (a scalper desk) as "institutional". `is_foreign` *is* taken
from the registry, cached to `reference/broker-registry.json`.

> Note `RX`, not `MQ`. `MQ` is not a valid IDX member code.

### Derived net foreign flow

Summing `nval` over foreign brokers reproduces the official `/v2/foreign-flow/` figure **exactly** —
verified on BBCA 2026-08-04 at Rp398,344,810,000 against the published board. So v3 gets both the
foreign number and the cohort split from one call, which is why it costs *less* than v2.

Broker codes absent from the registry are counted as domestic and listed in `unknown_brokers`, so a
large unclassified flow can't silently distort the foreign figure.

### Ticket size — the "stupid money" test

`value_per_trade = (bval + sval) / (bfreq + sfreq)`, then divided by **that stock's own median**
across brokers with `freq >= min_freq_for_median`.

Relative, not absolute, on purpose: a fixed rupiah threshold breaks across price levels, because the
same rupiah ticket is 128x the lots on a Rp50 stock as on a Rp6,400 one. On BBCA (median ~Rp38.5m):
XL 0.43x, XC ~0.38x — retail lands near **0.4x**, institutions near **1.1x**, MG highest at 1.9x.

This is a **diagnostic, not a classifier** — it cannot reproduce the four groups on its own (HNW
overlaps institutional; MG has the largest tickets of all). Broker identity does the classifying;
the ratio flags a broker acting out of character.

### Flow signals — these take precedence over the price/news ladder

| Signal | Rule | Read |
|---|---|---|
| **Retail trap** | `inst < 0` **and** `retail > 0` | Institutions selling while retail buys — the crowd is the exit liquidity |
| **Smart money** | `inst > 0` **and** `retail < 0` | Institutions accumulating while retail sells |
| **Quiet accumulation** | institutional run ≥ `quiet_run_sessions` in, `abs(Δ1d) < up_pct` | Building a position before the price moves |
| **Distribution (flow)** | institutional run `out` ≥ `flow_trend_sessions` | Sustained institutional selling under cover of attention |
| **Scalper churn** | scalper gross ≥ `scalper_min_gross_idr` and `abs(net)/gross < scalper_flat_ratio` | Heavy fast money that nets out flat — activity, not positioning |

**HNW is shown but not required to agree.** It is only four brokers and is noisy; gating on it would
have suppressed the largest institutional accumulation on the board (BBCA: inst +Rp322bn vs retail
−Rp139bn, with HNW *selling* Rp81bn). The Flow cell shows all three legs so the disagreement stays
visible even when the signal fires.

**Scalper churn is evaluated last, on purpose.** "Churn" is the *absence* of a read, so it must
never pre-empt one — evaluated earlier it masked MDKA's inst +Rp50.7bn on a −Rp0.1bn scalper net.

Flow signals are evaluated **before** Distribution / Confirmed / Extended / Anticipatory. A ticker
with no flow data falls through to the price ladder exactly as in v2, and its Flow cell shows "–".

### Cost
`config.json → brokers`: `top_n` tickers get broker detail (1 credit each), `credit_ceiling` hard-
stops the run rather than overrunning. Prices (`price_source: sectors`, `price_sectors_n`) cost 1
credit per ticker for the top names; everything else falls back to Yahoo for free. Typical run
~35 credits, against ~46–61 for v2. Calls are cached per trading session in a directory shared with
the morning brief, so whichever runs second pays only for what the first did not fetch.
