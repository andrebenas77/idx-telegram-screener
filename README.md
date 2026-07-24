# IDX Telegram Ticker-Mention Screener

A [Claude Code Skill](https://platform.claude.com/docs/en/docs/agents-and-tools/agent-skills/overview)
that reads your Telegram channels each day, counts how often each Indonesian (IDX/JCI) ticker is
mentioned, and publishes a ranked **crowdedness** board — so you can see which stocks the crowd is
piling into (potentially consensus/late) and which liquid names are being ignored (contrarian).

**Live site (after setup):** `https://andrebenas77.github.io/idx-telegram-screener/`

It measures **attention/chatter, not sentiment or direction.** Not investment advice.

## What each run does
1. `fetch_mentions.py` reads the last ~30h of messages from your channels (via your own Telegram
   account, Telethon) and counts ticker mentions using a curated dictionary + nicknames.
2. `build_screener.py` scores **crowdedness** — recency-weighted (4-session half-life) so short-lived
   IDX themes fade on their own — and renders a dark HTML board with six buckets:
   **Most Crowded Now · Heating Up · Aging/Late · Cooling/Fading · Expired · Quiet (contrarian).**
3. Today's counts are appended to `data/history.csv` (the trend memory) and the site + archive update.

## One-time setup

### A. Telegram API keys
1. Go to **https://my.telegram.org** → log in with your phone (Telegram sends a code).
2. Open **API development tools** → create an app (title `screener`, platform **Desktop**).
3. Copy the **api_id** (number) and **api_hash** (long string).
4. Copy `secrets/.env.example` → `secrets/.env` and fill in `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
   `TELEGRAM_PHONE` (in `+62…` form). **`secrets/` is git-ignored — your keys never get pushed.**

### B. Install Telethon
```bash
py -m pip install telethon
```

### C. List your channels
Copy `reference/channels.example.txt` → `reference/channels.txt` and list your channels, one per
line: `@name` or `t.me/name` (public), `name: My Channel Title` (private — matched by display name),
`t.me/c/123…`, or a numeric id. For **private** channels your account must already be a member.
`channels.txt` is git-ignored, so your list (including any private channel names) is never published.

### D. Log in once (you must run this yourself)
In a normal terminal (PowerShell), run:
```bash
py scripts/tg_login.py
```
Telegram texts you a login code — type it in (and your 2FA password if you have one). This creates
`secrets/screener.session` so every future run is automatic. (Claude can't do this step for you
because the code must be typed live.)

### E. GitHub + Pages (once, by hand — `gh` CLI isn't installed)
1. On github.com create an **empty public** repo `idx-telegram-screener` (no README/.gitignore/license).
2. From this folder:
   ```bash
   git init
   git add .
   git commit -m "Initial commit: IDX Telegram ticker-mention screener skill"
   git branch -M main
   git remote add origin https://github.com/andrebenas77/idx-telegram-screener.git
   git push -u origin main
   ```
3. Repo **Settings → Pages → Deploy from a branch → `main` / `/docs`** → Save.

> `secrets/` and `*.session` are in `.gitignore` and must **never** be committed — they grant access
> to your Telegram account.

## Daily use
In Claude Code just say **"run telegram screener"** or `/idx-telegram-screener`. Claude fetches,
builds, verifies the page, shows a summary, and (after you confirm) commits & pushes.

Manual run:
```bash
py scripts/fetch_mentions.py        # read channels -> update data/history.csv
py scripts/build_screener.py        # score + render docs/index.html
```

Preview locally: serve the `docs/` folder (e.g. `py -m http.server 8788 --directory docs`) and open
`http://localhost:8788`.

## How it works
Small standard-library + Telethon Python scripts do the deterministic counting and rendering; Claude
does the judgment (setup, sanity-checking matches, summarizing, publishing). The crowdedness
framework and all tunable knobs are documented in `reference/scoring.md` + `reference/config.json`.

```
telegram (Telethon) -> fetch_mentions.py -> data/history.csv
   -> build_screener.py -> docs/index.html + archive + history.csv -> git push -> GitHub Pages
```

## Notes
- **Tune it:** the ticker list (`reference/tickers.csv`) and scoring knobs (`reference/config.json`)
  are yours to edit. Flag any false-positive ticker with `ambiguous=1` (cashtag-only matching).
- **Day 1** shows only Most Crowded + Quiet; the trend-based buckets fill in over ~1–2 weeks.
- Not investment advice — an attention/chatter screen from public/your-own channels.
