#!/usr/bin/env python3
"""Thin REST client for the Invezgo API (api.invezgo.com).

Deliberately mirrors `sectors_client.py` in shape so the two read the same way and
neither skill needs a vendor SDK on the VPS. Endpoint paths, parameter names and the
auth format below were taken from the official Go SDK source (client.go / analysis.go /
batch.go / others.go), not from the README — the README omits the base URL entirely and
labels several parameters wrongly.

Four things this module exists to get right:

1. **Auth.** Invezgo wants `Authorization: Bearer <key>`. Note this is the OPPOSITE of
   the Sectors REST API, which takes the raw key and 401s on a `Bearer` prefix. The two
   clients sit side by side, so the mistake is easy and worth stating twice.
2. **Quota, not credits.** Invezgo meters whole *requests* per day, not per-endpoint
   credits. Every call is counted so a run can be reconciled against `/usage/api`.
3. **Range endpoints are the budget.** `from`/`to` endpoints return many days per
   request, so callers should pull a wide window on a rotation rather than one day at a
   time. `broker_stalker_list` in particular returns a whole broker's book, which is why
   the panel is built broker-first.
4. **Never block a build.** Any failure returns None and logs. Callers degrade to "-".

The `market` parameter is the board segment and matters more than it looks:
    RG = Reguler (the lit order book)
    TN = Tunai (cash)
    NG = Negosiasi (negotiated — where block crosses and nominee transfers land)
Separating NG from RG is the whole basis of the crossing detector.

Usage:
    from invezgo_client import InvezgoClient
    c = InvezgoClient(date="2026-08-05")
    book = c.broker_summary_stock("BBCA", start="2026-07-22", end="2026-08-05")
    c.report()
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    print("[invezgo] ERROR: requests not installed. Run: py -m pip install requests",
          file=sys.stderr)
    raise

# Windows consoles default to cp1252; Indonesian company names and API error bodies
# carry characters it cannot encode. Without this a stray character raises
# UnicodeEncodeError mid-run and kills the daily build.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = "https://api.invezgo.com"

# Lives outside the skill repo so it is never published to GitHub Pages.
# Platform-split on purpose: a Windows path on Linux does not fail, it silently creates
# a directory literally named `C:\Users\...` in the working directory — which on the VPS
# means the cache never persists between runs and every call is paid for twice.
DEFAULT_CACHE = (Path(r"C:\Users\ASUS\Documents\claude code\.invezgo-cache")
                 if os.name == "nt" else Path.home() / ".cache" / "invezgo")
CACHE_DIR = Path(os.environ.get("INVEZGO_CACHE_DIR", str(DEFAULT_CACHE)))

# Tape lives in the repo but is GIT-IGNORED (`data/tape/`), same rule as capture_tape.py:
# the repo is public and the feed is licensed, so raw vendor prints never leave the
# machine. Derived statistics are publishable; the tape they came from is not.
TAPE_DIR = Path(__file__).resolve().parent.parent / "data" / "tape"

TIMEOUT = 30
RETRIES = 2

# Board segments. See the module docstring — NG is where crossings live.
MARKET_REGULAR = "RG"
MARKET_CASH = "TN"
MARKET_NEGOTIATED = "NG"


def strip_jk(symbol: str) -> str:
    """`BBCA.JK` / `bbca.jk` -> `BBCA`. Invezgo wants the bare code; Sectors accepts
    either. Both clients key their cache on the bare form so the two stay comparable."""
    return str(symbol or "").strip().upper().removesuffix(".JK")


def decode_sankey_node(name: str) -> tuple[str, str]:
    """`" TP "` -> ("TP", "buy");  `" AK  "` -> ("AK", "sell").

    Sankey node labels are space-padded and THE PADDING IS THE SIDE MARKER: the buy-side
    node carries one trailing space, the sell-side node two. The same broker therefore
    appears as two distinct nodes in the same graph, and `.strip()`-ing before grouping
    merges a desk's buying and selling into a single node — which reports a broker that
    was flat as a whale, and hides every self-cross.

    Side is inferred from trailing whitespace only; leading padding is not load-bearing.
    Unpadded names fall back to "buy" (they only appear as link sources).
    """
    raw = str(name or "")
    trailing = len(raw) - len(raw.rstrip(" "))
    return raw.strip().upper(), ("sell" if trailing >= 2 else "buy")


class InvezgoClient:
    def __init__(self, date: str | None = None, verbose: bool = True,
                 use_cache: bool = True, user_agent: str | None = None):
        self.key = os.environ.get("INVEZGO_API_KEY", "").strip()
        self.date = date or time.strftime("%Y-%m-%d")
        self.verbose = verbose
        self.use_cache = use_cache
        # Per-instance UA override. The multi-time endpoint sits behind a stricter
        # Cloudflare rule than the rest of the API and 1010s the default UA below;
        # it needs a browser string. This is an override rather than a change to the
        # module default on purpose — backfill_panel.py and the 07:00 build work with
        # the current header and must not be disturbed by an intraday concern.
        self.user_agent = user_agent
        self.requests_used = 0
        self.cache_hits = 0
        self.errors: list[str] = []
        self._cache: dict = {}
        # The cache is one JSON blob per session date and _save_cache() rewrites the
        # WHOLE file after every request. That is fine at panel scale (a few hundred
        # small payloads) and quadratic at tape scale: 900 running-trade pages at ~50KB
        # each grow the file to ~45MB, and rewriting it 900 times is ~20GB of writes.
        # Measured before this flag existed: ~3 pages per 20 seconds and slowing.
        # Batch callers set this, then save once at the end.
        self._defer_save = False
        self._cache_path = CACHE_DIR / f"{self.date}.json"
        if self.use_cache:
            self._load_cache()

    def batched(self):
        """Context manager: hold cache writes until the block exits.

        with c.batched():
            rows = c.running_trade_all("BREN", "2026-08-12")
        """
        client = self

        class _Batch:
            def __enter__(self):
                client._defer_save = True
                return client

            def __exit__(self, *exc):
                client._defer_save = False
                client._save_cache()
                return False

        return _Batch()

    def rekey(self, date: str) -> None:
        """Re-point the cache file at the real trading session.

        Callers often construct the client before knowing which session is current
        (weekends, holidays). Grouping the cache by session date — not by run date —
        is what lets repeated runs on the same session cost nothing.
        """
        if not date or date == self.date:
            return
        self._save_cache()
        self.date = date
        self._cache_path = CACHE_DIR / f"{date}.json"
        self._cache = {}
        if self.use_cache:
            self._load_cache()

    # ---------- availability ----------

    @property
    def enabled(self) -> bool:
        """False when no key is configured — callers should degrade, not crash."""
        return bool(self.key)

    # ---------- cache ----------

    def _load_cache(self) -> None:
        try:
            if self._cache_path.exists():
                self._cache = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception as e:
            self._log(f"cache read failed ({e}) — continuing without it")
            self._cache = {}

    def _save_cache(self) -> None:
        if not self.use_cache or self._defer_save:
            return
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            self._log(f"cache write failed ({e}) — results not persisted")

    @staticmethod
    def _key(path: str, params: dict | None) -> str:
        if not params:
            return path
        items = sorted((k, v) for k, v in params.items() if v is not None)
        return path + "?" + "&".join(f"{k}={v}" for k, v in items)

    # ---------- core ----------

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[invezgo] {msg}", file=sys.stderr)

    def get(self, path: str, params: dict | None = None, no_store: bool = False):
        """GET {BASE}{path}. Returns parsed JSON, or None on any failure.

        Every successful call counts as exactly one request against the daily quota —
        including batch calls, which is the point of them.

        `no_store` keeps a response out of the shared per-date cache. Used for tape
        pages, which are individually large and collectively enormous; they get their own
        per-(symbol, date) store instead so the shared cache stays small enough that
        every other script's startup read is cheap.
        """
        if not self.enabled:
            self.errors.append("INVEZGO_API_KEY not set")
            return None

        ck = self._key(path, params)
        if self.use_cache and not no_store and ck in self._cache:
            self.cache_hits += 1
            return self._cache[ck]

        clean = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{BASE}{path}"
        # Bearer — the OPPOSITE of sectors_client.py, which sends the raw key.
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            # Cloudflare rejects the default urllib/requests UA on some Indonesian
            # hosts (error 1010); send an explicit one.
            "User-Agent": self.user_agent or "idx-telegram-screener/4.0",
        }

        last = None
        for attempt in range(RETRIES + 1):
            try:
                r = requests.get(url, headers=headers, params=clean, timeout=TIMEOUT)
                if r.status_code == 200:
                    data = r.json()
                    self.requests_used += 1
                    if self.use_cache and not no_store:
                        self._cache[ck] = data
                        self._save_cache()
                    return data
                if r.status_code == 204:
                    # Documented as "data tidak tersedia" — a valid empty answer, not a
                    # failure. Cache it so we don't re-ask for a day that has no data.
                    self.requests_used += 1
                    if self.use_cache:
                        self._cache[ck] = None
                        self._save_cache()
                    return None
                if r.status_code in (401, 402, 403):
                    msg = (f"{r.status_code} auth/subscription failure on {path} — "
                           f"check INVEZGO_API_KEY and that the plan is active")
                    self._log(msg)
                    self.errors.append(msg)
                    return None  # retrying these is pointless
                if r.status_code == 429:
                    msg = f"429 daily quota exhausted on {path} — stopping"
                    self._log(msg)
                    self.errors.append(msg)
                    return None  # retrying burns what little is left
                if r.status_code == 422:
                    msg = f"422 bad params {path} {clean} -> {r.text[:160]}"
                    self._log(msg)
                    self.errors.append(msg)
                    return None
                last = f"HTTP {r.status_code} {r.text[:120]}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))

        msg = f"failed {path} after {RETRIES + 1} tries — {last}"
        self._log(msg)
        self.errors.append(msg)
        return None

    def report(self) -> None:
        bits = [f"requests used: {self.requests_used}", f"cache hits: {self.cache_hits}"]
        if self.errors:
            bits.append(f"errors: {len(self.errors)}")
        self._log(" | ".join(bits))

    # ---------- account ----------

    def api_usage(self):
        """Remaining/limit for the day. The ONLY trustworthy statement of quota.

        Three different tier vocabularies exist in Invezgo's own material — the website
        sells Free/Starter/Pro/Advance, the SDK README documents
        Basic/Standard/Professional/Enterprise, and batch.go's comments say MAX/ELITE.
        None of them reconcile, so budget against this endpoint and nothing else.
        """
        return self.get("/usage/api")

    def membership_scope(self):
        """What the subscription actually entitles the key to."""
        return self.get("/membership/scope")

    # ---------- price ----------

    def stock_chart(self, symbol: str, start: str | None = None,
                    end: str | None = None):
        """Daily OHLCV over an arbitrary range — the EOD source (replaces Yahoo).

        Returns a range per request, so refresh each name on a rotation with a wide
        window rather than daily: same cost, and it self-heals over missed runs.
        """
        return self.get(f"/analysis/chart/stock/{strip_jk(symbol)}",
                        {"from": start, "to": end})

    def indicator_chart(self, symbol: str, indicator: str,
                        start: str | None = None, end: str | None = None):
        """Pre-computed series: `bdm` (bandarmology), `foreign`, `ratio`, `retail`.

        Deferred from the v4 build; kept wired because it is a free cross-check on our
        own derived flow.
        """
        return self.get(f"/analysis/chart/stock/{strip_jk(symbol)}/{indicator}",
                        {"from": start, "to": end})

    def intraday(self, symbol: str, market: str = MARKET_REGULAR):
        """Intraday bars. Takes NO date range — today only.

        NOTE: the conclusion once drawn from this endpoint — "Invezgo intraday cannot
        be backfilled" — is true of THIS path only. See multi_time_chart() below,
        which does serve history.
        """
        return self.get(f"/analysis/intraday/{strip_jk(symbol)}", {"market": market})

    def multi_time_chart(self, symbol: str, start: str, end: str,
                         timeframe: str | int = 5):
        """HISTORICAL intraday OHLCV at `timeframe` minutes. timeframe: 1/5/15/30/60/D/W/M.

        This is the endpoint that makes intraday rules backtestable, and it behaves
        unlike everything else in this client. Verified 2026-08-11 against Yahoo on
        GGRM: exact daily O/H/L/C and volume match, session VWAP within 0.03%.

          - Returns a BARE JSON LIST, not {"data": [...]}. Callers must not .get("data").
          - Timestamps carry a `Z` suffix but are ALREADY WIB. Do NOT timezone-convert;
            09:00 is the open, and the 08:55 bar is the pre-open auction.
          - History caps at ~114 trading sessions regardless of how far back `from`
            reaches. Asking for two years returns the same ~6 months.
          - `from == to` returns []. Always pass a real range.
          - ONE request covers the whole range for a symbol, which is what makes a
            112-name backfill affordable against a 30,000/MONTH quota.
          - Needs a browser User-Agent: construct the client with
            InvezgoClient(user_agent=BROWSER_UA) or Cloudflare answers 1010.
        """
        return self.get(f"/analysis/chart/multi-time/{strip_jk(symbol)}",
                        {"from": start, "to": end, "timeframe": timeframe})

    # ---------- broker / bandarmology ----------

    def broker_summary_stock(self, symbol: str, start: str | None = None,
                             end: str | None = None, investor: str = "all",
                             market: str = MARKET_REGULAR):
        """Every broker's buy/sell on one stock over an arbitrary range.

        The Sectors equivalent caps at 14 days; this reportedly does not, which is what
        makes a historical backtest possible at all. Verify the real depth in the probe.
        """
        return self.get(f"/analysis/summary/stock/{strip_jk(symbol)}",
                        {"from": start, "to": end, "investor": investor,
                         "market": market})

    def broker_stalker_list(self, broker_code: str, start: str | None = None,
                            end: str | None = None, investor: str = "all",
                            scope: str = "value", market: str = MARKET_REGULAR):
        """Every stock one broker touched over a range — the panel is built on this.

        Broker-first, not ticker-first: ~100 requests covers the entire IDX broker x
        stock x day panel for a window, where pulling per ticker would cost ~950.
        """
        return self.get(f"/analysis/stalker/list/{broker_code.upper()}",
                        {"from": start, "to": end, "investor": investor,
                         "scope": scope, "market": market})

    def broker_stalker(self, broker_code: str, symbol: str, start: str | None = None,
                       end: str | None = None, investor: str = "all",
                       market: str = MARKET_REGULAR, scope: str = "value"):
        """One broker's activity on one stock. Drill-down, not bulk capture."""
        return self.get(
            f"/analysis/stalker/broker/{broker_code.upper()}/{strip_jk(symbol)}",
            {"from": start, "to": end, "investor": investor, "market": market,
             "scope": scope})

    def inventory_chart(self, symbol: str, start: str | None = None,
                        end: str | None = None, scope: str = "val",
                        investor: str = "all", market: str = MARKET_REGULAR,
                        limit: int | None = None):
        """Cumulative broker accumulation/distribution — position and average entry.

        The cost-basis input: distance of spot from a broker's average entry says where
        they are underwater and where they would defend.
        """
        return self.get(f"/analysis/inventory-chart/stock/{strip_jk(symbol)}",
                        {"from": start, "to": end, "scope": scope,
                         "investor": investor, "market": market, "limit": limit})

    def broker_summary_broker(self, broker_code: str, start: str | None = None,
                              end: str | None = None, investor: str = "all",
                              market: str = MARKET_REGULAR):
        """One broker's buy/sell across EVERY stock it touched — the transpose of
        broker_summary_stock().

        One request returns a whole desk's market-wide footprint (~318 rows), which is
        the cheap way to answer "what else is this broker loading" and the denominator
        for the routing-anomaly feature (accumulation.md 3.7).

        Like broker_summary_stock(), a from/to range is AGGREGATED over the window with
        no daily dimension. Pass from == to for a single session.
        """
        return self.get(f"/analysis/summary/broker/{broker_code.upper()}",
                        {"from": start, "to": end, "investor": investor,
                         "market": market})

    def inventory_chart_broker(self, broker_code: str, start: str | None = None,
                               end: str | None = None, scope: str = "val",
                               investor: str = "all", market: str = MARKET_REGULAR,
                               limit: int | None = None,
                               filter_: list[str] | None = None):
        """inventory_chart() keyed by broker instead of by stock."""
        params = {"from": start, "to": end, "scope": scope, "investor": investor,
                  "market": market, "limit": limit}
        if filter_:
            params["filter"] = ",".join(filter_)
        return self.get(f"/analysis/inventory-chart/broker/{broker_code.upper()}",
                        params)

    def intraday_inventory(self, symbol: str, date: str, range_: int = 5,
                           type_: str = "value", total: int = 4,
                           buyer: str = "ALL", seller: str = "ALL",
                           market: str = MARKET_REGULAR,
                           broker: list[str] | None = None):
        """HISTORICAL replay of one past session with each top broker's CUMULATIVE net
        at `range_`-minute resolution. One request per (stock, date).

        This is the only way to see *when in the day* a broker loaded, and it is what
        separates real accumulation from distribution wearing accumulation's daily net:
        a broker whose cumulative net peaks mid-session and closes a third lower
        (`turn >= 0.30` in accumulation.md 5) sold into its own markup, and no daily-bar
        feature in this repo can see that.

        Returns {"price": [{x,o,h,l,c,v}], "broker": [{code, name, data:[{x,y}]}]}.
          - `x` is a WIB "HH:MM" bucket label, NOT a timestamp.
          - `y` is CUMULATIVE net from the session open, so a daily flow is the LAST
            value and a per-bucket flow is a diff of consecutive values.
          - `v` in the price series is VALUE in IDR when type_="value", not volume.
          - `total=4` returned 8 brokers (top 4 each side), so `total` is per-side.
        """
        params = {"date": date, "range": range_, "type": type_, "total": total,
                  "buyer": buyer, "seller": seller, "market": market}
        if broker:
            params["broker"] = ",".join(b.upper() for b in broker)
        return self.get(f"/analysis/intraday-inventory-chart/{strip_jk(symbol)}", params)

    def momentum_chart(self, symbol: str, date: str, range_: int = 5,
                       scope: str = "value"):
        """HISTORICAL aggressor-split order flow for one past session, bucketed.

        Returns [{time, value, buy, sell}] where `value` is PER-BUCKET traded value but
        `buy`/`sell` are CUMULATIVE aggressor-classified totals. Mixing the two up makes
        the series look like it explodes at the close.

        `scope` IS "value" OR "volume" AND NOTHING ELSE. The MCP tool schema advertises
        `vol | val | freq`, which is wrong in a way that matters: the MCP layer silently
        rewrites `val`->`value` and `vol`->`volume`, has no mapping for `freq`, and passes
        it through to a backend that answers

            422 `scope`: Invalid enum value. Expected 'value' | 'volume', received 'freq'

        So FREQUENCY-BUCKETED INTRADAY FLOW DOES NOT EXIST on this API, and the
        `frag = freq_imbalance - val_imbalance` read designed on top of it is not
        computable. Trade counts are available exactly twice: per broker-day from
        summary-stock (`buy_freq`/`sell_freq`), and per print from the tape. The tape is
        second-resolution and therefore strictly better than a 5-minute bucket would have
        been — see tape_lib.burst_stats.

        `range_=1` is also rejected; five minutes is the floor.
        """
        return self.get(f"/analysis/momentum-chart/{strip_jk(symbol)}",
                        {"date": date, "range": range_, "scope": scope})

    def sankey_chart(self, symbol: str, date: str | None = None,
                     type_: str | None = None, buyer: str | None = None,
                     seller: str | None = None, market: str = MARKET_REGULAR):
        """Broker-to-broker matched flow for one stock-day — crossing confirmation.

        Costs one request per (stock, date), so it is called ONLY on candidates the
        panel has already flagged. Running it across the universe x history would be a
        six-figure request bill.

        PARSING TRAP: node names are space-padded and THE PADDING ENCODES THE SIDE —
        `" TP "` (one trailing space) is the buy side, `" AK  "` (two) is the sell side.
        The same broker appears as two distinct nodes. Strip for display but split on the
        padding first, or a naive parse merges a broker's buying and selling into one
        node and reports a desk that was flat as a whale. Use decode_sankey_node().

        Self-links are real and meaningful: `" AK "` -> `" AK  "` is an internal cross.
        """
        return self.get(f"/analysis/sankey-chart/{strip_jk(symbol)}",
                        {"date": date, "type": type_, "buyer": buyer,
                         "seller": seller, "market": market})

    # ---------- market-wide daily (one request each) ----------

    def top_foreign(self, date: str | None = None):
        return self.get("/analysis/top/foreign", {"date": date})

    def top_accumulation(self, date: str | None = None):
        return self.get("/analysis/top/accumulation", {"date": date})

    def top_ritel(self, date: str | None = None):
        return self.get("/analysis/top/ritel", {"date": date})

    def top_change(self, date: str | None = None):
        """UNUSABLE FOR HISTORY — the `date` parameter is accepted and silently ignored.

        Verified 2026-08-13: date=2026-08-05 and date=2026-08-12 returned byte-identical
        rows (CUAN 870 +20.83, DOOH 284 +16.39), and BREN printed 3750 / +13.29% for a
        request dated 08-05 when it actually closed 3330 that session. It always returns
        the live snapshot. Any backtest built on this is measuring today.
        """
        return self.get("/analysis/top/change", {"date": date})

    # ---------- batch ----------

    def intraday_batch(self, symbols: list[str], market: str = MARKET_REGULAR,
                       date: str | None = None):
        """Intraday for several symbols in ONE request.

        Symbols go in the PATH, pipe-separated (`BBCA|GOTO|HUMI`) — not as a query
        parameter. batch.go's comments cap this at 3 symbols on MAX and 10 on ELITE and
        do not enforce it client-side, so the probe measures the real ceiling before any
        caller batches blindly.
        """
        joined = "|".join(strip_jk(s) for s in symbols)
        return self.get(f"/batch/intraday-data/{joined}",
                        {"market": market, "date": date})

    def order_book_batch(self, symbols: list[str], market: str = MARKET_REGULAR,
                         date: str | None = None, time_: str | None = None):
        """Order book for several symbols in one request. See intraday_batch on limits."""
        joined = "|".join(strip_jk(s) for s in symbols)
        return self.get(f"/batch/order-book/{joined}",
                        {"market": market, "date": date, "time": time_})

    # ---------- on-demand only ----------

    def running_trade(self, symbol: str, date: str | None = None,
                      page: int | None = None, limit: int | None = None,
                      type_: str | None = None, minimum: int | None = None,
                      maximum: int | None = None, market: str = "ALL",
                      orderby: str = "TIME", sort: str = "ASC",
                      pricefrom: float | None = None, priceto: float | None = None,
                      timefrom: str | None = None, timeto: str | None = None):
        """Tick prints. TARGETED USE ONLY — never universe-wide.

        On a CLOSED session this returns the real tape: second-granular timestamps,
        UNMASKED broker codes, and a per-print client-domicile flag. IDX masks broker
        codes to `--` intraday, so this must never be called against a live session and
        expected to name anyone.

        Fields: board, time ("HH:MM:SS"), price, volume (SHARES), buyer, seller,
        buyer_dom, seller_dom, type (aggressor side), avg_price (running VWAP).

        `buyer_dom`/`seller_dom` is the CLIENT's domicile, not the broker's — verified on
        BREN 2026-08-12, where adjacent prints read `ZP,DP,D,F` then `ZP,DP,F,F` (same
        two brokers, different flag). That makes it a DIRECT institution-vs-retail
        measurement, strictly better than Sectors' broker `cohort` label.

        Cost: BREN 2026-08-11 was 9,499 prints = 64 pages at limit=150. Budget
        65-120 requests per liquid stock-day and use the filters below to narrow:
          `minimum`/`maximum`   by print value   -> only blocks, or only retail clips
          `timefrom`/`timeto`   "HH:MM"          -> the 30 min around the day's high
          `pricefrom`/`priceto`                  -> activity at one level
          `type`  BUY|SELL|ALL  aggressor side

        NOTE the default `market` here is ALL, not RG: crossings (NG) are exactly what a
        distribution study must not silently drop.
        """
        return self.get(f"/analysis/running-trade/{strip_jk(symbol)}",
                        {"date": date, "page": page, "limit": limit, "type": type_,
                         "minimum": minimum, "maximum": maximum, "market": market,
                         "orderby": orderby, "sort": sort, "pricefrom": pricefrom,
                         "priceto": priceto, "timefrom": timefrom, "timeto": timeto})

    def running_trade_all(self, symbol: str, date: str, max_pages: int = 150,
                          limit: int = 150, cached_only: bool = False,
                          **kw) -> list[dict]:
        """Page running_trade() to exhaustion and return one flat, time-ordered list.

        `max_pages` is a HARD request ceiling, not a target — a mis-scoped call on a
        very liquid name could otherwise burn a four-figure slice of a 30,000/month
        quota in one loop. When the cap truncates, that is logged as an error rather
        than returned silently, because a truncated tape produces burst statistics that
        look calm for the wrong reason.

        Returns [] on failure; callers degrade rather than crash.
        """
        sym = strip_jk(symbol)
        store = TAPE_DIR / date[:7] / f"{sym}-{date}.json.gz"
        if store.exists():
            try:
                with gzip.open(store, "rt", encoding="utf-8") as fh:
                    cached = json.load(fh)
                self.cache_hits += 1
                self._log(f"tape {sym} {date}: {len(cached):,} prints from disk")
                return cached
            except Exception as e:
                self._log(f"tape cache unreadable ({e}) — refetching")

        if cached_only:
            # Deliberate opt-out for reruns and for names whose session is too large to
            # be worth a full pull. A very liquid stock-day runs past 300 pages; PTRO on
            # 2026-08-12 traded ~Rp741bn. Silently pulling that because a rerun happened
            # to touch it is how a quota disappears.
            self._log(f"tape {sym} {date}: not cached, --tape-cached-only — skipped")
            return []

        out: list[dict] = []
        page = 1
        total_pages = None
        while page <= max_pages:
            payload = self.get(f"/analysis/running-trade/{sym}",
                               {"date": date, "page": page, "limit": limit,
                                "type": kw.get("type_", "ALL"),
                                "orderby": kw.get("orderby", "TIME"),
                                "sort": kw.get("sort", "ASC"),
                                "market": kw.get("market", "ALL"),
                                "minimum": kw.get("minimum"),
                                "maximum": kw.get("maximum"),
                                "timefrom": kw.get("timefrom"),
                                "timeto": kw.get("timeto")},
                               no_store=True)
            if not payload:
                break
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not rows:
                break
            out.extend(rows)
            if total_pages is None and isinstance(payload, dict):
                total_pages = payload.get("totalPage")
            if total_pages is not None and page >= int(total_pages):
                break
            page += 1

        if total_pages is not None and int(total_pages) > max_pages:
            msg = (f"running_trade_all({sym},{date}) TRUNCATED at {max_pages} pages "
                   f"of {total_pages} — burst stats from this tape are not comparable")
            self._log(msg)
            self.errors.append(msg)
        elif out:
            # Only a COMPLETE tape is persisted. Caching a truncated one would make the
            # truncation permanent and invisible on every later run.
            try:
                store.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(store, "wt", encoding="utf-8") as fh:
                    json.dump(out, fh)
            except Exception as e:
                self._log(f"tape cache write failed ({e})")
        return out

    def price_table(self, symbol: str, date: str):
        """HISTORICAL volume-at-price, split by aggressor:
        [{price, buy_volume, sell_volume, buy_freq, sell_freq}] for the busiest levels.

        `date` IS REQUIRED — without it the API answers 422 "Path `date` should be
        `string`, but got `undefined`". The bundled MCP tool cannot ever succeed because
        its schema declares `code` only (schema/stock.ts: `priceTableSchema =
        z.object({ code })`) and its handler forwards no query string, so CALL REST
        DIRECTLY. Do not conclude from the MCP schema that the endpoint is code-only; it
        is not, and this was re-verified on BREN 2026-08-11.

        This is the volume-at-price instrument, and it is the closest thing to a
        retrospective answer to "was that wall eaten, or pulled?":
            absorb_at(p) = buy_volume(p) / (buy_volume(p) + sell_volume(p))
            ticket_at(p) = buy_volume(p) / buy_freq(p)      average clip working a level
        Heavy volume at the prior day's high with high absorb_at means the offer was real
        and got eaten; price passing through a level on thin volume means it was pulled or
        was never there. That inference is WEAK — it is consistent with a pulled order but
        does not observe one, because cancellations are in no historical feed. Label it as
        weak wherever it is shown.

        Volumes are in SHARES.
        """
        return self.get(f"/analysis/price-table/{strip_jk(symbol)}", {"date": date})

    def time_table(self, symbol: str, date: str):
        """Time-of-day traded distribution. Same required-`date` shape as price_table(),
        and the same MCP schema defect — call REST directly."""
        return self.get(f"/analysis/time-table/{strip_jk(symbol)}", {"date": date})

    def order_book(self, symbol: str, market: str = MARKET_REGULAR):
        """Live bid/offer depth. On-demand only — a 07:00 WIB run happens pre-market,
        where a live book has nothing to say.

        Keys are POSITIONAL, with the level index inside the key name
        (`bid1price`/`bid1lot`/`bid1freq`, `bid2price`, ...). Parse by regex, not by
        array index, and do NOT assume the two sides are the same depth — 35 bid vs 32
        offer levels has been observed live.

        `bidNfreq` is the ORDER COUNT at that level: the only field in the entire feed
        that separates one whale order from 800 retail orders, and the reason Phase 2
        exists. Outside session hours this endpoint degrades to a single level with
        freq=0, so an off-session call looks like a working call returning junk.
        """
        return self.get(f"/analysis/order-book/{strip_jk(symbol)}", {"market": market})

    def order_queue(self, symbol: str, price: float, side: str = "BUY",
                    page: int = 0, limit: int = 50):
        """Live per-order queue at one price level. Empty outside session hours.

        The ideal spoofing instrument (individual order sizes and queue positions) and
        structurally incapable of history — there is no `date` in the schema. Phase 2
        forward capture only; must never enter a score.
        """
        return self.get(f"/analysis/queue/{strip_jk(symbol)}",
                        {"price": price, "side": side, "page": page, "limit": limit})

    # ---------- reference ----------

    def list_stock(self):
        """Every non-delisted IDX company. The universe superset for build_universe.py."""
        return self.get("/analysis/list/stock")

    def list_broker(self):
        """Every exchange member. Join table for broker code -> name."""
        return self.get("/analysis/list/broker")
