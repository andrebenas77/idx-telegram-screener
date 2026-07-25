---
name: idx-telegram-screener
description: Daily Telegram ticker-mention screener for Indonesian (IDX/JCI) stocks. Reads your Telegram channels via Telethon, counts how often each ticker is mentioned, and publishes a ranked dark HTML "crowdedness" board (Most Crowded / Heating Up / Aging / Cooling / Expired / Quiet-contrarian) plus a running CSV. Each crowded name also shows price move (Δ1d/Δ5d), volume (RVOL), latest news, and a sell-on-news Signal. Recency-weighted so stale ~2-week-old themes drop off automatically. Triggers on "telegram screener", "mention screener", "run telegram screener", "crowded stocks", "which tickers are crowded", "telegram ticker mentions".
---

# IDX Telegram Ticker-Mention Screener

Each run: read recent messages from the user's Telegram channels, count mentions of IDX tickers,
score **crowdedness** (recency-weighted so short-lived IDX themes expire on their own), render a
dark HTML board, append to a running CSV, and — after showing a summary — commit & push to GitHub
Pages. This measures **attention/crowding, not sentiment**. It is not investment advice.

## Absolute rules (MUST)
1. **Never fabricate mentions or counts.** Every number comes from `scripts/fetch_mentions.py`
   reading real messages. If a channel fails, note it and continue — never invent data.
2. **Secrets never leave the machine.** `secrets/` and `*.session` are git-ignored. Never print the
   `api_hash`, never commit `secrets/`, never ask the user to paste keys into chat.
3. **Read-only on Telegram.** The scripts only read messages — never post, forward, join, or delete.
4. **Ask before pushing.** Show the summary first; commit & push only after the user confirms.
5. **The scripts do the counting.** Don't hand-count or hand-edit `docs/` HTML — always regenerate.

## First-time setup (once)
If `secrets/.env` or `secrets/screener.session` is missing, walk the user through `README.md`:
get API keys at my.telegram.org → fill `secrets/.env` → `py -m pip install telethon` →
list channels in `reference/channels.txt` → the user runs `py scripts/tg_login.py` themselves in a
terminal (interactive code entry — you cannot do this step for them). Then create the GitHub repo +
enable Pages (README "One-time GitHub setup").

## Daily workflow
Copy this checklist into your reply and check items off:

```
IDX Telegram Screener — progress
- [ ] 1. Preflight: secrets/.env + secrets/screener.session + reference/channels.txt exist
- [ ] 2. Fetch mentions:  py scripts/fetch_mentions.py
- [ ] 3. Sanity-check the printed Top-15 (counts plausible? any junk match?)
- [ ] 4. Fetch prices:  py scripts/fetch_prices.py   (Yahoo .JK: last close + Δ1d/Δ5d/RVOL)
- [ ] 5. News scan (top-5 crowded) -> build/news-<date>.json   (see reference/news-sources.md)
- [ ] 6. Build:  py scripts/build_screener.py
- [ ] 7. Verify docs/index.html in the browser preview
- [ ] 8. Summarize for the user (crowded · who's moving · signals · news)
- [ ] 9. Commit & push (ask first); confirm the Pages URL
```

### Step 1 — Preflight
Confirm `secrets/.env`, `secrets/screener.session`, and a non-empty `reference/channels.txt` exist.
If `screener.session` is missing, stop and have the user run `py scripts/tg_login.py` first.
On Windows use the `py` launcher (or the full path
`C:/Users/ASUS/AppData/Local/Python/bin/python.exe`).

### Step 2 — Fetch mentions
```bash
py scripts/fetch_mentions.py
```
Reads the last `fetch_lookback_hours` (config) of messages from every channel, matches tickers, and
updates `data/history.csv` + `build/mentions-<date>.json`. Use `--since 48h` to widen the window or
`--date YYYY-MM-DD` to set the session label. Note any `[!!] FAILED` channels for the user.

### Step 3 — Sanity check
Look at the printed Top-15. If a common word is topping the list (a false positive), open the
offending ticker in `reference/tickers.csv` and set `ambiguous=1` (cashtag-only), then re-fetch.

### Step 4 — Fetch prices/volume
```bash
py scripts/fetch_prices.py
```
Pulls the **last completed session** (morning run = yesterday's close) for every mentioned ticker
from Yahoo `.JK` — `Δ1d`, `Δ5d`, and `RVOL` (volume ÷ 20-day avg) → `build/prices-<date>.json`.
Free, no key. Missing/failed tickers degrade to "–" (never blocks the build).

### Step 5 — News scan (top-5 crowded)
For the top `news_top_n` crowded tickers, find the **latest real news** (last ~48h) and write
`build/news-<date>.json`. Follow [reference/news-sources.md](reference/news-sources.md) — Emitennews
first, then Kontan/CNBC/Technoz. **Context only, never fabricate** (only links you actually fetched;
if nothing fresh, leave that ticker out). This feeds the sell-on-news **Signal**.

### Step 6 — Build the board
```bash
py scripts/build_screener.py
```
Scores crowdedness from `data/history.csv`, folds in `prices-<date>.json` + `news-<date>.json`, and
writes `docs/index.html`, the dated archive, and `docs/history.csv`. Scoring, the price/volume
metrics, and the **Signal** logic are documented in [reference/scoring.md](reference/scoring.md);
knobs live in [reference/config.json](reference/config.json).

### Step 7 — Verify
Open `docs/index.html` in the browser preview (serve `docs/` on localhost — see README). Confirm the
table renders with Δ1d/Δ5d/RVOL/Signal/News columns, the "In the News" cards populate, dark theme
looks right, and the Archive link works. On **Day 1** only Most Crowded + Quiet + prices/news are
meaningful; Heating/Aging/Cooling/Expired fill in as history accrues over ~1–2 weeks (expected).

### Step 8 — Summarize
Give the user a short read: top ~5 Most Crowded, **who's moving** (Δ1d/RVOL), the **Signals**
(Distribution / Confirmed / Extended / Anticipatory), any fresh **news**, and 2–3 Quiet contrarian
names. Attention + factual price/news framing only — no buy/sell views.

### Step 9 — Publish
Show counts + top names, **ask before pushing**, then commit & push. Confirm the live URL
`https://andrebenas77.github.io/idx-telegram-screener/`. See [README.md](README.md) for git/Pages.
(If the git push is network-blocked, publish via the manual upload path in the README.)

## Layout
```
SKILL.md              # this workflow
README.md             # setup (Telegram API + login + GitHub/Pages)
reference/            # channels.txt · tickers.csv · config.json · scoring.md · output-format.md · news-sources.md
scripts/              # tg_login.py · fetch_mentions.py · fetch_prices.py · build_screener.py
assets/template.html  # dark self-contained HTML shell
data/history.csv      # accumulating daily counts (committed)
docs/                 # published site (GitHub Pages)
secrets/              # .env + session (git-ignored, never pushed)
examples/ , evals/    # sample output + test scenarios
```
