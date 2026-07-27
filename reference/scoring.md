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

## Foreign & institutional flow (v3)

Chatter measures **retail attention**. Flow answers the question that actually matters: *who is on
the other side of it?* That pairing is the point — a crowded name with institutions buying is a
different trade from a crowded name with institutions selling.

Data comes from `scripts/fetch_flows.py` (Sectors API) into `build/flows-<date>.json`:

| Field | Meaning |
|---|---|
| `latest_net` | **Exact** net foreign flow in IDR for the session (`/v2/foreign-flow/`) |
| `run_sessions` / `run_direction` | Consecutive sessions of same-direction flow, e.g. `8 in` |
| `sum_3` | Net over the last 3 sessions — catches a one-day reversal inside a longer trend |
| `inst_net` / `retail_net` | Cohort split, summed across ~all brokers in each cohort |

IDX is a closed market: foreign and domestic net always sum to zero per (symbol, date), so domestic
flow is simply `-latest_net`. No second call is needed.

### Flow signals — these take precedence over the price/news ladder

| Signal | Rule | Read |
|---|---|---|
| **Retail trap** | `latest_net < 0` **and** `inst_net < 0` **and** `retail_net > 0` | Foreigners and institutions selling while retail buys — the crowd is the exit liquidity |
| **Smart money** | `latest_net > 0` **and** `inst_net > 0` | Foreign and institutional accumulation agree with the chatter |
| **Distribution (flow)** | `run_direction == "out"` for ≥ `flow_trend_sessions` | Sustained foreign selling under cover of attention |

Evaluated **before** Distribution / Confirmed / Extended / Anticipatory. A ticker with no flow data
falls through to the price ladder exactly as in v2, and its Foreign cell shows "–".

**"Quiet accumulation" deliberately lives elsewhere.** Flow is only fetched for the *crowded*
tickers, so a quiet name has no flow data here by construction. That bucket is produced by the
morning brief's radar, which measures a wider shortlist — see
`indonesia-morning-news-brief/scripts/build_radar.py`.

### Cost
`config.json → sectors`: `tier` (`lean` ~32 / `standard` ~50 / `deep` ~91 credits),
`flow_top_n` tickers measured exactly, `cohort_top_n` also split by cohort. Calls are cached per
trading session in a directory shared with the morning brief, so whichever runs second pays only
for tickers the first did not fetch.
