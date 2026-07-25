# News sources (for the top-crowded news scan)

For the **top `news_top_n` crowded tickers** (default 5), find the latest real news, last ~48h.
Context-only: capture headline + link, don't editorialize. **Never fabricate** — only include a
headline/link you actually retrieved this run; if nothing fresh, record none (the card shows
"No fresh news found").

## Where to look (per ticker, by company name + code)
| Outlet | URL | Best for |
|---|---|---|
| Emitennews | `https://emitennews.com/` (+ site search) | Emiten / corporate actions — the primary source |
| Kontan | `https://insight.kontan.co.id/` · `https://investasi.kontan.co.id/` | Stock/market news |
| CNBC Indonesia | `https://www.cnbcindonesia.com/market` · `/market-data/quote/{CODE}.JK` | Market news + per-ticker quote/news |
| Bloomberg Technoz | `https://www.bloombergtechnoz.com/` | Market/economy |
| Bareksa / Investor Daily / Investortrust | via search | Analyst calls, dividends, earnings |

## How (each run)
1. For each top ticker, `WebSearch` e.g. `"<Company> <CODE> saham berita terbaru"` (Bahasa) — this
   surfaces the outlets above with dated items.
2. Optionally `WebFetch` the most relevant article to confirm the link resolves and refine the
   one-line English summary. Keep the original headline verbatim.
3. Prefer **corporate-action / catalyst** items (earnings, dividends, IPO of a subsidiary, insider
   buys, contracts) — those are what drive the "sell on news" reaction the screener watches for.
4. Write `build/news-<date>.json` (schema below). Missing tickers are fine — they render "–".

## `build/news-<date>.json` schema
```json
{
  "date": "2026-07-24",
  "generated_at": "2026-07-25T08:40:00+07:00",
  "news": {
    "BBCA": [
      {"headline": "…", "url": "https://…", "outlet": "Investor Daily",
       "date": "2026-07-24", "summary": "One factual English line."}
    ]
  }
}
```
- Up to ~3 items per ticker (the page shows the first 3). `date`/`summary` optional but preferred.
- This file is git-ignored (lives in `build/`); the rendered headlines land in `docs/`.
