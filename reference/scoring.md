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
