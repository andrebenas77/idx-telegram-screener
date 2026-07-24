# Output & data formats

## `data/history.csv` (committed — the trend memory)
Appended once per session; re-running the same date overwrites that date's rows (idempotent).

```
date,ticker,posts,channels
2026-07-24,BBRI,11,6
2026-07-24,GOTO,9,5
```
- `date` — session date, `YYYY-MM-DD`, Asia/Jakarta (WIB).
- `posts` — messages mentioning the ticker in the lookback window.
- `channels` — distinct channels that mentioned it.

This file is the single source of truth for scoring. Keep it in git so trends survive across days
and machines. A public copy is written to `docs/history.csv` for download.

## `build/mentions-YYYY-MM-DD.json` (git-ignored — spot-check detail)
Full per-run detail, used for verification and for the page header stats.
```json
{
  "date": "2026-07-24",
  "generated_at": "2026-07-24T18:00:00+07:00",
  "lookback_hours": 30,
  "messages_scanned": 642,
  "total_ticker_posts": 57,
  "channels_scanned": ["@ch1", "@ch2"],
  "channels_failed": [],
  "tickers": [
    {"ticker":"BBRI","company":"Bank Rakyat Indonesia","posts":11,"channels":6,
     "channel_list":["@ch1","@ch2"],"share":0.19}
  ]
}
```

## Published site (`docs/`, served by GitHub Pages)
- `docs/index.html` — today's screener (6 sections: Most Crowded / Heating / Aging / Cooling /
  Expired / Quiet).
- `docs/archive/YYYY-MM-DD.html` — dated snapshot per run.
- `docs/archive.html` — index of all archived days.
- `docs/history.csv` — downloadable copy of the running history.

The look lives entirely in `assets/template.html` (self-contained dark HTML with `{{placeholders}}`).
Never hand-edit the HTML in `docs/` — always regenerate via `scripts/build_screener.py`.
