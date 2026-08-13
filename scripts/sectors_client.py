#!/usr/bin/env python3
"""Thin REST client for the Sectors Financial API (api.sectors.app/v2).

Shared by the morning brief and the Telegram screener. Kept deliberately small and
duplicated into both skills — they are separate git repos, so a copy beats a fragile
cross-repo import.

Three things this module exists to get right:

1. **Auth.** The REST API wants the raw key in `Authorization`. Adding a `Bearer `
   prefix returns 401. (The MCP server is the opposite — it *requires* `Bearer`.)
2. **Credits.** Every call is metered and costs vary by endpoint, so each request is
   counted and reported. A day-scoped disk cache shared between both skills means a
   ticker fetched by one is free for the other.
3. **Never block a build.** Any failure returns None and logs. Callers degrade to "-".

Usage:
    from sectors_client import SectorsClient
    c = SectorsClient(date="2026-07-24")
    flow = c.foreign_flow("BBCA", start="2026-07-18", end="2026-07-24")
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
    print("[sectors] ERROR: requests not installed. Run: py -m pip install requests",
          file=sys.stderr)
    raise

# Windows consoles default to cp1252; Indonesian headlines and API error bodies carry
# characters it cannot encode. Without this a stray character raises UnicodeEncodeError
# mid-run and kills the daily build.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = "https://api.sectors.app/v2"
# Used only by corporate_actions(): the REST surface has no such path.
MCP_URL = "https://sectors-mcp.supertype.ai/mcp"

# Lives outside both skill repos so neither publishes it to GitHub Pages.
# Platform-split: on Linux a Windows path does NOT raise — Path() treats it as a
# relative name, so the VPS was creating a directory literally called
# `C:\Users\ASUS\Documents\claude code\.sectors-cache` inside the repo working tree.
# It showed up as untracked junk and the cache never landed where anything expected it.
DEFAULT_CACHE = (Path(r"C:\Users\ASUS\Documents\claude code\.sectors-cache")
                 if os.name == "nt" else Path.home() / ".cache" / "sectors")
CACHE_DIR = Path(os.environ.get("SECTORS_CACHE_DIR", str(DEFAULT_CACHE)))

TIMEOUT = 30
RETRIES = 2


def strip_jk(symbol: str) -> str:
    """`BBCA.JK` / `bbca.jk` -> `BBCA`. The API accepts either but we key cache on one."""
    return str(symbol or "").strip().upper().removesuffix(".JK")


def normalize_tag(tag: str) -> str:
    """Collapse the API's inconsistent tag conventions to one comparable form.

    The vocabulary contains literal duplicates and case collisions: `free-float-compliance`
    and `free_float_compliance` both exist, articles return `Analyst Ratings` while filings
    return `divestment`. Everything folds to lowercase-kebab.
    """
    t = str(tag or "").strip().lower()
    t = t.replace("&", " and ")
    for ch in ("_", " ", "/"):
        t = t.replace(ch, "-")
    while "--" in t:
        t = t.replace("--", "-")
    return t.strip("-")


class SectorsClient:
    def __init__(self, date: str | None = None, verbose: bool = True,
                 use_cache: bool = True):
        self.key = os.environ.get("SECTORS_API_KEY", "").strip()
        self.date = date or time.strftime("%Y-%m-%d")
        self.verbose = verbose
        self.use_cache = use_cache
        self.credits = 0
        self.cache_hits = 0
        self.errors: list[str] = []
        # Windows the API silently narrowed. Not errors — the call succeeded — but a
        # caller that ignores these is quietly working with less history than it asked
        # for. See broker_summary().
        self.clamps: list[str] = []
        self._cache: dict = {}
        self._cache_path = CACHE_DIR / f"{self.date}.json"
        if self.use_cache:
            self._load_cache()

    def rekey(self, date: str) -> None:
        """Re-point the cache file at the real trading session.

        Callers often construct the client before knowing which session is current
        (weekends, holidays). Grouping the cache by session date — not by run date —
        is what lets the brief and the screener share each other's calls.
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
        if not self.key:
            return False
        return True

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
            print(f"[sectors] {msg}", file=sys.stderr)

    def get(self, path: str, params: dict | None = None, credits: int = 1):
        """GET {BASE}{path}. Returns parsed JSON, or None on any failure."""
        if not self.enabled:
            self.errors.append("SECTORS_API_KEY not set")
            return None

        ck = self._key(path, params)
        if self.use_cache and ck in self._cache:
            self.cache_hits += 1
            return self._cache[ck]

        clean = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{BASE}{path}"
        # Raw key — NOT "Bearer <key>". Bearer returns 401 on the REST API.
        headers = {"Authorization": self.key, "Accept": "application/json"}

        last = None
        for attempt in range(RETRIES + 1):
            try:
                r = requests.get(url, headers=headers, params=clean, timeout=TIMEOUT)
                if r.status_code == 200:
                    data = r.json()
                    self.credits += credits
                    if self.use_cache:
                        self._cache[ck] = data
                        self._save_cache()
                    return data
                if r.status_code in (401, 403):
                    msg = f"{r.status_code} auth failed on {path} — check SECTORS_API_KEY"
                    self._log(msg)
                    self.errors.append(msg)
                    return None  # retrying auth failures is pointless
                if r.status_code == 400:
                    msg = f"400 bad request {path} {clean} -> {r.text[:160]}"
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
        bits = [f"credits used: {self.credits}", f"cache hits: {self.cache_hits}"]
        if self.clamps:
            bits.append(f"clamped windows: {len(self.clamps)}")
        if self.errors:
            bits.append(f"errors: {len(self.errors)}")
        self._log(" | ".join(bits))

    # ---------- endpoints ----------

    def news(self, **params):
        return self.get("/news/", params, credits=1)

    def filings(self, **params):
        return self.get("/filings/", params, credits=1)

    def suspensions(self, **params):
        return self.get("/suspensions/", params, credits=1)

    def foreign_flow(self, symbol: str, start: str | None = None, end: str | None = None):
        """Exact daily net foreign IDR. Window capped at 90 days by the API."""
        return self.get(f"/foreign-flow/{strip_jk(symbol)}/",
                        {"start": start, "end": end}, credits=1)

    def brokers(self):
        """Exchange-member registry: code, name, is_foreign, cohort, license_type.

        `is_foreign` is the authoritative input for derived net foreign flow.
        `cohort` is a LICENSING label and is deliberately unused — it calls YP and MG
        institutional, which is wrong behaviourally (see reference/brokers.csv).
        """
        return self.get("/brokers/", None, credits=1)

    def broker_summary(self, symbol: str, start: str | None = None,
                       end: str | None = None, broker_code: str | None = None):
        """EVERY broker's buy/sell/net/lots/freq for one ticker, grouped by day.

        One credit returns the entire book, which is what makes both the cohort split
        and the derived foreign flow affordable — summing the foreign brokers' `nval`
        reproduces /foreign-flow/ exactly.

        THE WINDOW IS CAPPED AT 14 DAYS AND CLAMPS SILENTLY. Asking for 2026-07-20 ->
        2026-08-12 (23 days) returns HTTP 200 with no warning and a payload whose echoed
        `start` reads 2026-07-29. A caller that trusts its own request parameters loses a
        third of the window and never learns. This method now compares the echoed `start`
        against the requested one and records any shortfall on `self.clamps`; use
        broker_summary_range() to cover a longer span correctly.

        Field notes for callers:
          `blot`/`slot`   LOTS of 100 shares (Invezgo's equivalents are SHARES)
          `bavg_per_share`/`savg_per_share`   per share
          `navg_per_share`  A DECOY. It is a verbatim copy of bavg_per_share when
                            nval > 0 and of savg_per_share when nval < 0 — it is not a
                            net average price. Compute nval / (nlot * 100) instead.
          sum(bval) over brokers ~= the day's traded value, which is the only route to
                            daily VALUE on this API (/daily/ does not carry it).
        """
        payload = self.get(f"/broker-summary/{strip_jk(symbol)}/",
                           {"start": start, "end": end, "broker_code": broker_code},
                           credits=1)
        if payload and start:
            echoed = payload.get("start") if isinstance(payload, dict) else None
            if echoed and str(echoed) > str(start):
                note = (f"broker_summary({strip_jk(symbol)}) window CLAMPED: "
                        f"asked {start}, got {echoed}")
                self._log(note)
                self.clamps.append(note)
        return payload

    def broker_summary_range(self, symbol: str, start: str, end: str,
                             broker_code: str | None = None) -> list[dict]:
        """broker_summary() over an arbitrary span, walking backwards in whatever window
        the API actually grants rather than in an assumed 14 days.

        Chains from `end` towards `start`, and takes the NEXT window's end from the
        echoed `start` of the payload just received. That way the real cap is measured
        per call instead of hard-coded, so a vendor change to 10 or 20 days degrades to
        "more requests" rather than to silent holes.

        Returns a flat list of the API's per-day objects ({date, summary:[...]}),
        de-duplicated by date and sorted ascending. Empty list on total failure.
        """
        from datetime import date as _date, timedelta as _td

        def _d(s: str) -> _date:
            y, m, dd = (int(x) for x in str(s)[:10].split("-"))
            return _date(y, m, dd)

        by_date: dict[str, dict] = {}
        cursor, floor = _d(end), _d(start)
        guard = 0
        while cursor >= floor and guard < 60:
            guard += 1
            payload = self.get(
                f"/broker-summary/{strip_jk(symbol)}/",
                {"start": floor.isoformat(), "end": cursor.isoformat(),
                 "broker_code": broker_code}, credits=1)
            if not payload:
                break
            for row in (payload.get("data") or []):
                d = str(row.get("date"))[:10]
                if d:
                    by_date[d] = row
            echoed = str(payload.get("start") or "")[:10]
            if not echoed:
                break
            got = _d(echoed)
            if got <= floor:
                break            # the whole remaining span came back in one call
            cursor = got - _td(days=1)
        if guard >= 60:
            msg = f"broker_summary_range({strip_jk(symbol)}) hit the 60-window guard"
            self._log(msg)
            self.errors.append(msg)
        return [by_date[k] for k in sorted(by_date)]

    def most_traded(self, start: str | None = None, end: str | None = None,
                    n_stock: int = 10, adjusted: bool = True,
                    sub_sector: str | None = None):
        """Most-traded stocks per date. Window capped at 90 days.

        `adjusted=True` ranks by VALUE, `adjusted=False` by raw share volume, and the two
        produce genuinely different universes on the same day (2026-08-12: CUAN/TPIA/PTRO
        by value vs BUMI/IATA/JGLE by volume). Take the union when building a universe.

        `n_stock` IS HARD-CAPPED AT 10 and clamps silently — passing 20 returns 10 with
        no warning. Asserted here rather than assumed, so the day the cap moves we find
        out from a log line instead of from a quietly narrower universe.

        Output carries only {symbol, company_name, volume, price}; there is no `value`
        field, so compute volume * price yourself.
        """
        if n_stock > 10:
            self._log(f"most_traded: n_stock={n_stock} requested but the API caps at 10")
        payload = self.get("/most-traded/",
                           {"start": start, "end": end, "n_stock": n_stock,
                            "adjusted": str(bool(adjusted)).lower(),
                            "sub_sector": sub_sector}, credits=2)
        if isinstance(payload, dict):
            for day, rows in payload.items():
                if isinstance(rows, list) and len(rows) > n_stock:
                    self._log(f"most_traded: {day} returned {len(rows)} > n_stock "
                              f"{n_stock} — the cap may have changed")
                break
        return payload

    def daily(self, symbol: str, start: str | None = None, end: str | None = None):
        """Daily close, volume and market cap. Window capped at 90 days by the API.

        The price source for v3 — one credit covers enough history for Δ1d, Δ5d and
        a 20-day RVOL, so it replaces the per-ticker Yahoo pull.
        """
        return self.get(f"/daily/{strip_jk(symbol)}/",
                        {"start": start, "end": end}, credits=1)

    def index_daily(self, index_code: str = "ihsg", start: str | None = None,
                    end: str | None = None):
        """Daily index close. `ihsg` is the market-adjustment baseline for the Broker
        Alpha backtest — raw returns would just rank brokers by beta. 90-day cap, so
        long histories are chained."""
        return self.get(f"/index-daily/{index_code.lower()}/",
                        {"start": start, "end": end}, credits=1)

    def corporate_actions(self, symbol: str):
        """Splits, bonus, rights and dividends — the price-adjustment inputs.

        Routed over the MCP JSON-RPC endpoint rather than REST: the REST host works
        fine (verified 200 on /daily/ and /index-daily/ with the raw key) but exposes
        no corporate-actions path — every variant tried returns a JSON 404 "endpoint
        does not exist". The MCP tool returns it correctly, so this one method takes a
        different transport. Cached like any other call.

        This matters because Invezgo prices are RAW: an unadjusted 10:1 split reads as
        a -90% day and would poison any forward-return study.
        """
        sym = strip_jk(symbol)
        ck = self._key("/mcp/corporate-actions/", {"symbol": sym})
        if self.use_cache and ck in self._cache:
            self.cache_hits += 1
            return self._cache[ck]
        if not self.enabled:
            self.errors.append("SECTORS_API_KEY not set")
            return None

        import re as _re
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "fetch-corporate-actions", "arguments": {"symbol": sym}},
        }
        headers = {
            # MCP wants Bearer; the REST surface above refuses it. Opposite conventions
            # on the same vendor — see the module docstring.
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "idx-telegram-screener/4.0",
        }
        for attempt in range(RETRIES + 1):
            try:
                r = requests.post(MCP_URL, headers=headers, json=payload,
                                  timeout=TIMEOUT)
                if r.status_code == 200:
                    # Reply is SSE-framed: "event: message\ndata: {json}"
                    m = _re.search(r"^data:\s*(.+)$", r.text, _re.M)
                    env = json.loads(m.group(1) if m else r.text)
                    data = json.loads(env["result"]["content"][0]["text"])
                    self.credits += 1
                    if self.use_cache:
                        self._cache[ck] = data
                        self._save_cache()
                    return data
                last = f"HTTP {r.status_code} {r.text[:120]}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
        msg = f"corporate_actions({sym}) failed — {last}"
        self._log(msg)
        self.errors.append(msg)
        return None

    def top_brokers(self, date: str | None = None, metric: str = "net",
                    origin: str = "all", cohort: str = "all",
                    n_brokers: int | None = None):
        """Brokers ranked for one date. metric='net' ranks by ABSOLUTE net, so the
        list mixes accumulators and distributors — which is what aggregation wants."""
        return self.get("/brokers/top/",
                        {"date": date, "metric": metric, "origin": origin,
                         "cohort": cohort, "n_brokers": n_brokers}, credits=2)

    def broker_activity_top(self, broker_code: str, start: str | None = None,
                            end: str | None = None, n_brokers: int | None = None):
        """One broker's top accumulations/distributions by ticker."""
        return self.get(f"/broker-activity/{broker_code.upper()}/top/",
                        {"start": start, "end": end, "n_brokers": n_brokers}, credits=2)

    def broker_summary_top(self, symbol: str, start: str | None = None,
                           end: str | None = None, cohort: str = "all",
                           origin: str = "all", n_brokers: int | None = None):
        """One ticker's top buying/selling brokers. cohort= retail|institutional is
        what makes the retail-vs-institution read possible."""
        return self.get(f"/broker-summary/{strip_jk(symbol)}/top/",
                        {"start": start, "end": end, "cohort": cohort,
                         "origin": origin, "n_brokers": n_brokers}, credits=2)
