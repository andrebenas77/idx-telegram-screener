# Invezgo — measured behaviour and the upgrade decision

All numbers below were measured on **2026-08-06** by `scripts/invezgo_probe.py` (22 calls,
9 billable) plus follow-ups. Raw output: `build/invezgo-probe-2026-08-06.json`. Re-run the
probe before trusting any of it after a plan change.

## The headline: published docs are wrong about the budget

The SDK READMEs say *"Basic: 100 requests/hari"*. The live meter disagrees:

```json
{"usage": 9, "remaining": 29991, "limit": 30000,
 "isBlocked": false, "expire": "2026-09-05T02:33:46.855Z"}
```

**30,000 requests per ~30-day period**, not 100/day — roughly 1,000/day equivalent, ~300x
the documented figure. `GET /usage/api` costs **0** and is the only place MCP spend is
visible, so check it rather than reasoning from the docs.

## Latency: the "~45 s server time" claim was WRONG — it was our own IPv6 fault

**Superseded 2026-08-06 (second probe).** This section previously claimed every request took
"**~42–47 s** of server time, remarkably consistent across endpoints," and concluded that
Invezgo was a scheduled-only, never-interactive source. That was a misdiagnosis of a *local
network fault* as a vendor property.

Re-measured on the same box, dual-stack, no patches:

| Endpoint | Time |
|---|---|
| `/usage/api` | 0.13–0.57 s |
| `/analysis/order-book/` | 0.46 s |
| `/analysis/running-trade/` (150 rows) | 1.86–2.06 s |
| `/analysis/running-trade/` (history) | 4.26 s |

**0.5–4 s, not 42–47 s** — off by 20–25x. The "remarkably consistent ~43 s" was the IPv6
blackhole described in `dewa-orderbook-lab/scripts/ob_net.py`: this box resolved the
Cloudflare AAAA record first, hung until timeout, then fell back to IPv4. The DEWA lab
diagnosed that correctly and pinned `AF_INET`; this file recorded the identical measurement
as "server time" and built a design rule on it. Two files in one repo, same number, opposite
explanations — the lab's was right.

The internal tell was there all along: `ob_logger.py` polls **every 6 s** and works. That is
impossible if a request costs 45 s.

As of this probe the blackhole no longer reproduces at all — dual-stack 0.47 s vs forced-IPv4
0.38 s, statistically identical, so the route was fixed or was transient. `ob_net.py`'s
`AF_INET` pin is now belt-and-braces rather than load-bearing; harmless, keep it.

Consequences: Invezgo **is** usable interactively. Threading in `capture_tape.py`
(`--workers`, default 4) is still fine but is no longer the difference between feasible and
infeasible. Do not cite a latency budget to justify a design without re-measuring first.

## The finding that dictates the whole design

**IDX masks broker codes during the session.** Identical query, two dates:

| Session | `buyer` | `seller` | `buyer_dom` |
|---|---|---|---|
| Today, in-session (09:31) | `"--"` | `"--"` | `""` |
| Yesterday, closed | `"CC"` | `"YU"` | `"F"` |

This is exchange policy (IDX hides broker codes intraday to suppress herding), not a vendor
limit or a plan restriction. **The entire value of this feed exists only after the close.**
`capture_tape.py` defaults to the last closed session and refuses to archive a >50% masked
pull unless `--allow-masked` is passed.

## Endpoint reality

| Endpoint | Result |
|---|---|
| `/analysis/running-trade/{code}` | Works. Paginated, `limit` **caps at 150**. Full BBCA session = **139 pages**. |
| `orderby=VOLUME&sort=DESC` | **Works** — day's largest tickets in 1 request. Top print 340,000 sh vs 4,700 in time order. |
| `minimum=<shares>` | **Works**, server-side. `minimum=100000` cut a 23-page result to **1 page, 6 rows**. |
| `market=NG` | **Works on closed sessions.** 2 pages/day. This is the crossing board — see below. |
| `/analysis/order-book/{code}` | Works, and is **genuinely live** (book changed across a 60 s gap). Depth is **38 bid / 52 offer levels** — far deeper than the usual 10. |
| `/analysis/intraday/{code}` | 1-minute bars with `open/high/low/close/volume/**freq**/**value**`. Richer than Yahoo 1m, which has no trade count or turnover. |
| `/batch/order-book/...`, `/batch/intraday-data/...` | **402 "Minimum max role required" — not available on this plan.** Costs 0 when refused. |
| `/analysis/order-queue/...` | 404. The MCP advertises an `order-queue` tool but this path does not exist. |
| `/analysis/summary/stock/`, `/inventory-chart/`, `/stalker/broker/` | Deliberately unused — EOD aggregates that duplicate Sectors. Do not pay twice. |

⚠️ Intraday bar timestamps are labelled `Z` (UTC) but carry **WIB wall-clock values**
(`08:58:00.000Z` is the 08:58 WIB pre-open). Treat as WIB; do not convert.

## Crossings — a capability the v4 plan wanted and had no source for

The NG board exposes same-broker-both-sides prints. BBCA, 2026-08-05:

| Time | Board | Volume | Value | Broker |
|---|---|---|---|---|
| 16:02:03 | NG | 19,764,100 | **Rp 127.5 bn** | CC → CC (F) |
| 13:32:32 | NG | 3,297,700 | Rp 21.3 bn | YU → YU (F) |
| 16:03:04 | NG | 2,373,200 | Rp 15.3 bn | ZP → ZP (F) |

`capture_tape.py` flags these with an `is_crossing` column. A single 8-name run found **120
crossings**. The v4 plan deferred the "crossing/block detector" for want of a source; it
costs 1 request per name per day.

## Validation against Sectors

BBCA, 2026-08-05. Sectors reports **lots** (`nlot`), Invezgo reports **shares** (÷100 to
compare). The Invezgo capture is the top-150 tickets per board — a deliberately biased
subset — so magnitudes must not match; direction must.

**Direction agreement on the top 10 tape brokers: 10/10. Brokers absent from Sectors' book: none.**

| Broker | tape net (sh) | Sectors net (sh) |
|---|---|---|
| YU | −4,751,400 | −7,856,300 |
| CC | +3,240,600 | +13,802,300 |
| KZ | −2,596,400 | −4,151,300 |
| ZP | +2,386,100 | +11,863,700 |

Top tickets capture roughly 30–60% of each broker's full-day net — exactly what capturing
only the large prints should produce. Both sources are corroborated.

## Decision: stay on Basic

Do **not** upgrade yet.

- The nightly design costs ~2 requests/name. 25 names × 2 boards × ~20 sessions ≈ **1,000
  requests/month against a 30,000 allowance** — under 4% utilisation.
- The only things a higher tier buys are the **batch endpoints** (402 today), which mainly
  help *live* polling — and live data has no broker codes, so it is the least valuable mode.
- Full-tape archiving (139 req/name/session) is affordable *occasionally* — ~215 ticker-days
  per month if it were the only spend — but the top-tickets subset already reproduces
  direction perfectly, so paying for every retail one-lot buys little.

**Revisit if** any of these change: you want full tape for a research backtest across many
names; you want live order-book polling across a watchlist (needs batch → higher role); or
`/usage/api` shows utilisation climbing past ~50%.

## Operational rules

1. **Run after 17:00 WIB.** Before that, broker codes are masked and the archive is worthless.
2. **Never publish raw tape.** `data/tape/` is git-ignored — the repo is public and this is a
   licensed feed. Publish derived signals only.
3. **The MCP shares this quota with no ledger.** Check `/usage/api` (free) before and after
   ad-hoc querying, and never leave an MCP call in an unbounded loop. Cost, not speed, is the
   reason: requests are fast (0.5–4 s, see above), which makes an accidental loop *more*
   dangerous, not less — it drains quota quickly and silently.
