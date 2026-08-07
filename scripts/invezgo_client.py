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


class InvezgoClient:
    def __init__(self, date: str | None = None, verbose: bool = True,
                 use_cache: bool = True):
        self.key = os.environ.get("INVEZGO_API_KEY", "").strip()
        self.date = date or time.strftime("%Y-%m-%d")
        self.verbose = verbose
        self.use_cache = use_cache
        self.requests_used = 0
        self.cache_hits = 0
        self.errors: list[str] = []
        self._cache: dict = {}
        self._cache_path = CACHE_DIR / f"{self.date}.json"
        if self.use_cache:
            self._load_cache()

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
        if not self.use_cache:
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

    def get(self, path: str, params: dict | None = None):
        """GET {BASE}{path}. Returns parsed JSON, or None on any failure.

        Every successful call counts as exactly one request against the daily quota —
        including batch calls, which is the point of them.
        """
        if not self.enabled:
            self.errors.append("INVEZGO_API_KEY not set")
            return None

        ck = self._key(path, params)
        if self.use_cache and ck in self._cache:
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
            "User-Agent": "idx-telegram-screener/4.0",
        }

        last = None
        for attempt in range(RETRIES + 1):
            try:
                r = requests.get(url, headers=headers, params=clean, timeout=TIMEOUT)
                if r.status_code == 200:
                    data = r.json()
                    self.requests_used += 1
                    if self.use_cache:
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
        """Intraday bars. Takes NO date range — today only, so intraday features are
        forward-capture and cannot be backfilled. Confirm in the probe before relying
        on that."""
        return self.get(f"/analysis/intraday/{strip_jk(symbol)}", {"market": market})

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

    def sankey_chart(self, symbol: str, date: str | None = None,
                     type_: str | None = None, buyer: str | None = None,
                     seller: str | None = None, market: str = MARKET_REGULAR):
        """Broker-to-broker flow for one stock-day — crossing confirmation.

        Costs one request per (stock, date), so it is called ONLY on candidates the
        panel has already flagged. Running it across the universe x history would be a
        six-figure request bill.
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
                      market: str = MARKET_REGULAR):
        """Tick prints. DELIBERATELY NOT USED BY THE PIPELINE — on-demand only.

        There is no batch variant and the endpoint is paginated, so a liquid name's
        single session runs to dozens of requests and a universe-wide backfill reaches
        six figures. Every feature it would have provided is derived more cheaply from
        the broker panel (aggression via bavg vs VWAP, blocks via bval/bfreq) or from
        intraday bars (time-of-day profile, absorption).

        `minimum` filters by print value, so asking for *only* large prints is cheap —
        that is the one shape worth using here when inspecting a single name by hand.
        """
        return self.get(f"/analysis/running-trade/{strip_jk(symbol)}",
                        {"date": date, "page": page, "limit": limit, "type": type_,
                         "minimum": minimum, "market": market})

    def order_book(self, symbol: str, market: str = MARKET_REGULAR):
        """Live bid/offer depth. On-demand only — a 07:00 WIB run happens pre-market,
        where a live book has nothing to say."""
        return self.get(f"/analysis/order-book/{strip_jk(symbol)}", {"market": market})
