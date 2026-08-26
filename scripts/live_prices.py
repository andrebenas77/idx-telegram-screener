#!/usr/bin/env python3
"""Live-ish marks for open positions, from Yahoo v8.

Chosen over Invezgo purely on cost. Invezgo's batch endpoints return 402 on this plan
(`reference/invezgo.md`), so a live view is one request per position per refresh: a
five-name book on a 15-minute cadence is ~260 requests a day, ~5,700 a month, roughly a
fifth of the 30,000/month quota — spent on a convenience, crowding out the panel backfill
and every future study. Yahoo is free, unmetered, and ~15 minutes delayed, and 15 minutes
is immaterial to a system whose exits are decided on CLOSES.

`fetch_prices.py` already talks to this endpoint but deliberately refuses intraday data:
its `session_closed()` guard exists because the BOARDS must score completed sessions, and
a half-formed bar would corrupt rvol and chg1d. That guard is right there and wrong here —
a portfolio mark WANTS the forming session. So this reads `meta.regularMarketPrice`
directly rather than touching `compute()`.

**A mark is never shown without its own timestamp.** A stale quote that looks live is
worse than a blank: it invites acting on a price that no longer exists. So every quote
carries `ts`, `is_stale()` is a published function rather than a caller's judgement, and a
Yahoo failure falls back to the last good mark WITH ITS ORIGINAL TIMESTAMP — never blank,
and never yesterday's close passed off as today's.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from position_book import BOOK  # noqa: E402

WIB = timezone(timedelta(hours=7))
MARKS = BOOK / "marks.json"
UA = "Mozilla/5.0"
STALE_S = 1800          # 30 min: comfortably past a 15-min delay plus a slow refresh
CLOSED_S = 7200         # 2h: nothing has printed in two hours — the market is shut


def _url(symbol: str) -> str:
    # range=1d&interval=1d is the smallest payload that still carries `meta`, which is
    # the only part we read. The candles are irrelevant here.
    return (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.JK"
            f"?range=1d&interval=1d")


def quote(symbol: str, *, timeout: int = 15) -> dict | None:
    """One live-ish mark, or None. Never raises for a normal network failure."""
    try:
        req = urllib.request.Request(_url(symbol), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        meta = d["chart"]["result"][0]["meta"]
        px = meta.get("regularMarketPrice")
        ts = meta.get("regularMarketTime")
        if px is None or ts is None:
            return None
        return {"symbol": symbol, "px": float(px), "ts": int(ts),
                "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
                "source": "yahoo", "fetched": int(time.time())}
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
            IndexError, ValueError, TimeoutError, OSError):
        return None


def _load_marks() -> dict:
    if not MARKS.exists():
        return {}
    try:
        return json.loads(MARKS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_marks(marks: dict) -> None:
    MARKS.parent.mkdir(parents=True, exist_ok=True)
    MARKS.write_text(json.dumps(marks, indent=2), encoding="utf-8")


def quotes(symbols: list[str], *, use_cache: bool = True) -> dict[str, dict]:
    """Marks for several symbols, falling back to the last good one per symbol.

    Politeness sleep matches fetch_prices.py:197 — same host, same courtesy. Failures are
    per-symbol: one dead ticker must not blank the whole book.
    """
    cache = _load_marks() if use_cache else {}
    out: dict[str, dict] = {}
    for i, sym in enumerate(symbols):
        if i:
            time.sleep(0.12)
        q = quote(sym)
        if q:
            cache[sym] = q
            out[sym] = q
        elif sym in cache:
            out[sym] = dict(cache[sym], source="cache")
    if use_cache:
        _save_marks(cache)
    return out


def is_stale(q: dict | None, max_age_s: int = STALE_S) -> bool:
    if not q or not q.get("ts"):
        return True
    return (time.time() - int(q["ts"])) > max_age_s


def market_looks_closed(qs: dict[str, dict]) -> bool:
    """Every mark is hours old. The absence of prints IS the signal.

    Used instead of a hardcoded IDX holiday list, which goes stale the first year nobody
    updates it — the same reasoning trade_manage.py applies to missing bars.
    """
    if not qs:
        return True
    return all(is_stale(q, CLOSED_S) for q in qs.values())


def age_str(q: dict | None) -> str:
    if not q or not q.get("ts"):
        return "no mark"
    when = datetime.fromtimestamp(int(q["ts"]), WIB)
    mins = int((time.time() - int(q["ts"])) / 60)
    if mins < 60:
        return f"{when:%H:%M} ({mins}m ago)"
    if mins < 60 * 24:
        return f"{when:%H:%M} ({mins // 60}h ago)"
    return f"{when:%Y-%m-%d %H:%M}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", help="tickers; default = open positions")
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()

    syms = [s.upper() for s in a.symbols]
    if not syms:
        from position_book import rebuild
        syms = sorted(rebuild()["positions"])
        if not syms:
            print("book is flat — pass symbols explicitly")
            return 0

    qs = quotes(syms, use_cache=not a.no_cache)
    print(f"{'sym':<6}{'mark':>10}{'prev':>10}{'chg':>9}  as of")
    for s in syms:
        q = qs.get(s)
        if not q:
            print(f"{s:<6}{'—':>10}{'—':>10}{'—':>9}  no quote and no cached mark")
            continue
        prev = q.get("prev_close")
        chg = f"{(q['px'] / prev - 1) * 100:+.2f}%" if prev else "—"
        flag = "  [STALE]" if is_stale(q) else ""
        src = "" if q.get("source") == "yahoo" else f"  [{q.get('source')}]"
        print(f"{s:<6}{q['px']:>10,.0f}{(prev or 0):>10,.0f}{chg:>9}  "
              f"{age_str(q)}{flag}{src}")
    if market_looks_closed(qs):
        print("\nevery mark is over 2h old — the market appears closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
