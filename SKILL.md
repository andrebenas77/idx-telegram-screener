---
name: idx-telegram-screener
description: Daily Telegram ticker-mention screener for Indonesian (IDX/JCI) stocks. Reads your Telegram channels via Telethon, counts how often each ticker is mentioned, and publishes a ranked dark HTML "crowdedness" board (Most Crowded / Heating Up / Aging / Cooling / Expired / Quiet-contrarian) plus a running CSV. Recency-weighted so stale ~2-week-old themes drop off automatically. Triggers on "telegram screener", "mention screener", "run telegram screener", "crowded stocks", "which tickers are crowded", "telegram ticker mentions".
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
- [ ] 2. Fetch:  py scripts/fetch_mentions.py
- [ ] 3. Sanity-check the printed Top-15 (counts look plausible? any junk match?)
- [ ] 4. Build:  py scripts/build_screener.py
- [ ] 5. Verify docs/index.html in the browser preview (all 6 sections render)
- [ ] 6. Summarize for the user (top crowded / heating / cooling / quiet)
- [ ] 7. Commit & push (ask first); confirm the Pages URL
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

### Step 4 — Build the board
```bash
py scripts/build_screener.py
```
Scores crowdedness from `data/history.csv` and writes `docs/index.html`, the dated archive, and
`docs/history.csv`. Scoring + buckets are documented in [reference/scoring.md](reference/scoring.md);
knobs live in [reference/config.json](reference/config.json).

### Step 5 — Verify
Open `docs/index.html` in the browser preview (serve `docs/` on localhost — see README). Confirm the
6 sections render, the leaderboard is populated, dark theme looks right, and the Archive link works.
On **Day 1** only Most Crowded + Quiet are meaningful; Heating/Aging/Cooling/Expired fill in as
history accrues over ~1–2 weeks. That is expected — do not treat empty sections as a bug.

### Step 6 — Summarize
Give the user a short read: the top ~5 Most Crowded, anything NEW/Heating, what's Cooling/Expired,
and 2–3 notable Quiet contrarian names. Attention framing only — no buy/sell views.

### Step 7 — Publish
Show counts + top names, **ask before pushing**, then commit & push. Confirm the live URL
`https://andrebenas77.github.io/idx-telegram-screener/`. See [README.md](README.md) for git/Pages.

## Layout
```
SKILL.md              # this workflow
README.md             # setup (Telegram API + login + GitHub/Pages)
reference/            # channels.txt · tickers.csv · config.json · scoring.md · output-format.md
scripts/              # tg_login.py · fetch_mentions.py · build_screener.py
assets/template.html  # dark self-contained HTML shell
data/history.csv      # accumulating daily counts (committed)
docs/                 # published site (GitHub Pages)
secrets/              # .env + session (git-ignored, never pushed)
examples/ , evals/    # sample output + test scenarios
```
