# Evals — manual test scenarios

Quick checks to confirm the skill behaves. Run after changing scripts, the ticker list, or config.

## Eval 1 — Rendering & buckets (no Telegram needed)
```bash
cp examples/sample-history.csv data/history.csv
py scripts/build_screener.py --date 2026-07-24
```
**Pass if:** `docs/index.html` builds; Most Crowded lists BBRI/GOTO/AMMN near the top; PTRO & BREN
carry a **NEW** tag; Cooling shows ADRO/MDKA; Expired shows RAJA/WIFI; Quiet lists liquid names with
"0 posts / 5 sessions". Open it in a browser and confirm the dark theme + sparklines render.
Clean up: `rm data/history.csv`.

## Eval 2 — Idempotent history
Run a fetch twice for the same date (or build twice). **Pass if:** `data/history.csv` has exactly one
row per (date, ticker) — re-running a date overwrites, never duplicates.

## Eval 3 — False-positive control
Post/imagine a message containing the words "cuan", "raja", "auto", "good". **Pass if:** none are
counted as tickers (they're `ambiguous=1`, cashtag-only), while `$CUAN` or uppercase `BBRI` are.

## Eval 4 — Recency/expiry
With `sample-history.csv`, confirm a ticker silent for ≥4 sessions (RAJA) appears under **Expired**,
not **Most Crowded**, and that a fresh spike (BREN, Day 3) ranks high with a high heat pill. This is
the "IDX themes only last ~2 weeks" behavior — old chatter must decay out.

## Eval 5 — Live smoke test (needs setup)
After login: `py scripts/fetch_mentions.py --since 48h`. **Pass if:** it prints a Top-15 with
plausible counts, lists channels scanned, and writes `data/history.csv`. Spot-check one ticker's
count against the actual channel.
