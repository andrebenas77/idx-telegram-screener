# Examples

- **`sample-output.html`** — what the daily board looks like with ~2 weeks of data. **Synthetic
  data** (made-up tickers/counts) purely to show the layout and all six buckets populated:
  Most Crowded, Heating Up (with NEW tags), Aging/Late, Cooling/Fading, Expired, and Quiet.
- **`sample-history.csv`** — the synthetic `data/history.csv` that produced it. To reproduce:
  ```bash
  cp examples/sample-history.csv data/history.csv
  py scripts/build_screener.py --date 2026-07-24
  ```
  (Delete `data/history.csv` afterwards so your real runs start clean.)

These are illustrations only — not real Telegram data and not investment advice.
