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

# Lives outside both skill repos so neither publishes it to GitHub Pages.
DEFAULT_CACHE = Path(r"C:\Users\ASUS\Documents\claude code\.sectors-cache")
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
