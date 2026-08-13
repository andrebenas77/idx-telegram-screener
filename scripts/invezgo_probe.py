#!/usr/bin/env python3
"""Metered probe of the Invezgo API — measures what Basic (100 req/day) can actually buy.

This exists because the Basic package is a *validation* budget, not a production one, and
the two numbers that decide whether to upgrade are not documented anywhere:

1. **What does each call really cost?** `/usage/api` reports a running counter, so calling
   it around every probe measures true cost per endpoint instead of assuming 1.
2. **How many pages is a full day of tape?** `limit` caps at 150/page (SDK docstring), so a
   liquid name could be 200+ pages — 2x the entire daily budget for ONE ticker. `totalPage`
   on page 1 answers this for 1 request instead of paging through it.

The probe also tests the design that would rescue the budget if (2) is as bad as feared:
`orderby=VOLUME&sort=DESC&limit=150` returns the day's 150 biggest tickets in a single
request. For bandarmology that is the whole signal — you want the whale prints with named
buyer/seller brokers, not every retail one-lot fill.

Uses the official SDK deliberately: the batch endpoint paths (`/batch/order-book/{a|b|c}`)
are not published anywhere, and every resolved URL is logged so the hand-rolled production
client can reproduce them without carrying the dependency.

Budget safety: a hard --budget ceiling aborts the run rather than overrunning. Mirrors the
`credit_ceiling` pattern in reference/config.json. Nothing here retries. Note that --budget
is a per-run guard, not the plan limit: the measured plan ceiling is 30,000 requests per
~30-day period (`GET /usage/api`, self-cost 0), not the "100 requests/hari" the docs claim.

Usage:
    py scripts/invezgo_probe.py --budget 40
    py scripts/invezgo_probe.py --budget 40 --thin HUMI --liquid BBCA
    py scripts/invezgo_probe.py --dry-run          # print the ledger plan, spend nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from invezgo import InvezgoClient
except ImportError:  # pragma: no cover
    print("[invezgo] ERROR: SDK not installed. Run: py -m pip install invezgo-sdk",
          file=sys.stderr)
    raise

# Windows consoles default to cp1252; API error bodies are Indonesian and carry characters
# it cannot encode. Without this a stray character kills the run mid-probe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
WIB = timezone(timedelta(hours=7))

# IDX regular session, WIB. Mon-Thu 09:00-12:00 / 13:30-15:49; Fri 09:00-11:30 / 14:00-15:49.
SESSION_START = (9, 0)
SESSION_END = (15, 49)

# Measured plan ceiling, confirmed live via GET /usage/api (self-cost 0), which is the
# authoritative source. The published "Basic: 100 requests/hari" is wrong by ~300x.
# `plan_limit_reported` in this probe's own output records the live value each run — if it
# ever disagrees with this constant, trust the meter and update here.
PLAN_MONTHLY_QUOTA = 30000
TRADING_SESSIONS_PER_MONTH = 22


def wib_now() -> datetime:
    return datetime.now(WIB)


def in_session(now: datetime) -> bool:
    """Live-data probes are meaningless outside trading hours; the run says so rather than
    silently reporting an empty order book as a finding."""
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return SESSION_START <= hm <= SESSION_END


def prev_session(d: datetime) -> str:
    """Previous weekday. Ignores IDX holidays — a 204 on this date is itself informative."""
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x.strftime("%Y-%m-%d")


class BudgetExceeded(Exception):
    """Raised to abort the run cleanly with partial results written."""


class Ledger:
    """Runs probes and measures each one's true quota cost.

    Reads `/usage/api` once after each probe and reuses that reading as the next probe's
    baseline, so metering costs one extra call per probe rather than two. The self-cost of
    the usage endpoint is measured first and subtracted out.
    """

    def __init__(self, client: InvezgoClient, budget: int, verbose: bool = True):
        self.c = client
        self.budget = budget
        self.verbose = verbose
        self.rows: list[dict] = []
        self.calls = 0            # requests we made, counted locally
        self.usage_self_cost = 0  # does /usage/api count against itself?
        self._last_usage: int | None = None
        self.limit_reported: int | None = None

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[invezgo] {msg}", file=sys.stderr)

    def _raw_usage(self) -> tuple[int | None, dict]:
        """Returns (used, raw). Field names are unconfirmed, so try the plausible ones."""
        try:
            u = self.c.usage.get_api_usage() or {}
        except Exception as e:
            self._log(f"usage read failed: {type(e).__name__}: {e}")
            return None, {"error": f"{type(e).__name__}: {e}"}
        self.calls += 1
        body = u.get("data", u) if isinstance(u, dict) else {}
        if not isinstance(body, dict):
            return None, {"raw": u}
        for k in ("usage", "used", "count", "total", "requests"):
            if isinstance(body.get(k), (int, float)):
                for lk in ("limit", "quota", "max"):
                    if isinstance(body.get(lk), (int, float)):
                        self.limit_reported = int(body[lk])
                        break
                return int(body[k]), u
        return None, u

    def calibrate(self) -> None:
        """Two back-to-back usage reads reveal whether the meter counts itself."""
        a, raw_a = self._raw_usage()
        b, _ = self._raw_usage()
        if a is not None and b is not None:
            self.usage_self_cost = max(0, b - a)
            self._last_usage = b
            self._log(f"usage baseline={a} -> {b} | /usage/api self-cost = "
                      f"{self.usage_self_cost} | plan limit = {self.limit_reported}")
        else:
            self._log(f"could not parse usage counter; raw = {json.dumps(raw_a)[:300]}")

    def probe(self, label: str, question: str, fn, capture=None) -> dict:
        """Run one probe, measure its cost, record a row. Never raises except on budget."""
        if self.calls >= self.budget:
            raise BudgetExceeded(f"{self.calls} calls >= budget {self.budget}")

        before = self._last_usage
        t0 = time.time()
        try:
            result = fn()
            err = None
        except Exception as e:
            result = None
            err = f"{type(e).__name__}: {e}"
        elapsed_ms = int((time.time() - t0) * 1000)
        if err is None:
            self.calls += 1

        after, _ = self._raw_usage()
        cost = None
        if before is not None and after is not None:
            cost = after - before - self.usage_self_cost
        self._last_usage = after

        row = {
            "probe": label,
            "question": question,
            "ok": err is None,
            "error": err,
            "measured_cost": cost,
            "elapsed_ms": elapsed_ms,
            "usage_after": after,
        }
        if err is None and capture:
            try:
                row["finding"] = capture(result)
            except Exception as e:
                row["finding"] = {"capture_failed": f"{type(e).__name__}: {e}"}
        # Keep a trimmed sample so shapes can be inspected without dumping a full tape.
        row["sample"] = _trim(result)

        self.rows.append(row)
        status = "ok " if err is None else "ERR"
        cost_s = "?" if cost is None else str(cost)
        self._log(f"{status} {label:<26} cost={cost_s:<3} {elapsed_ms:>5}ms"
                  + (f"  {err[:90]}" if err else ""))
        return row


def _trim(obj, depth: int = 0):
    """Shrink a response to something readable: first 3 list items, strings capped."""
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        return {k: _trim(v, depth + 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        out = [_trim(v, depth + 1) for v in obj[:3]]
        if len(obj) > 3:
            out.append(f"... +{len(obj) - 3} more")
        return out
    if isinstance(obj, str) and len(obj) > 200:
        return obj[:200] + "..."
    return obj


# ---------- capture helpers: turn a raw response into the answer we came for ----------

def _pages(resp) -> dict:
    """Tape pagination. The Go SDK says total_page, the Python types say totalPage —
    the wire format is unconfirmed, so accept either."""
    if not isinstance(resp, dict):
        return {"parsed": False, "type": type(resp).__name__}
    body = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    total = body.get("totalPage", body.get("total_page"))
    rows = body.get("data") if isinstance(body.get("data"), list) else None
    if rows is None and isinstance(resp.get("data"), list):
        rows = resp["data"]
    out = {
        "total_pages": total,
        "rows_this_page": len(rows) if isinstance(rows, list) else None,
        "next_page": body.get("nextPage", body.get("next_page")),
    }
    if isinstance(total, int):
        out["full_tape_cost_requests"] = total
        # Against the MEASURED plan ceiling (30,000/~30 days), not the docs' bogus 100/day.
        # Reported per-session so it is comparable to one run: ~22 trading days a month.
        out["pct_of_monthly_quota"] = round(100 * total / PLAN_MONTHLY_QUOTA, 3)
        out["pct_of_session_share"] = round(
            100 * total / (PLAN_MONTHLY_QUOTA / TRADING_SESSIONS_PER_MONTH), 1)
    if isinstance(rows, list) and rows:
        r0 = rows[0]
        if isinstance(r0, dict):
            out["tick_fields"] = sorted(r0.keys())
            out["has_broker_tags"] = all(k in r0 for k in ("buyer", "seller"))
            out["first_tick"] = r0
            out["last_tick"] = rows[-1]
            vols = [r.get("volume") for r in rows if isinstance(r.get("volume"), (int, float))]
            if vols:
                out["volume_max"] = max(vols)
                out["volume_min"] = min(vols)
            brokers = {r.get("buyer") for r in rows if r.get("buyer")}
            out["distinct_buyer_brokers"] = len(brokers)
    return out


def _book(resp) -> dict:
    """Order book depth. The SDK type only declares bid1price/bid1lot/bid1freq per array
    item, which cannot be right for a 10-level book — so dump what actually arrives."""
    if not isinstance(resp, dict):
        return {"parsed": False, "type": type(resp).__name__}
    body = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    bid = body.get("bid")
    offer = body.get("offer")
    out = {
        "code": body.get("code"),
        "bid_levels": len(bid) if isinstance(bid, list) else None,
        "offer_levels": len(offer) if isinstance(offer, list) else None,
        "top_level_keys": sorted(bid[0].keys()) if isinstance(bid, list)
                          and bid and isinstance(bid[0], dict) else None,
        "bid_raw": bid if isinstance(bid, list) and len(bid) <= 12 else None,
    }
    return out


def _book_fingerprint(resp) -> str:
    """Stable string for the staleness diff."""
    try:
        body = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        return json.dumps({"b": body.get("bid"), "o": body.get("offer")}, sort_keys=True)
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Metered Invezgo API probe")
    ap.add_argument("--budget", type=int, default=40,
                    help="hard ceiling on requests; the run aborts rather than exceed it")
    ap.add_argument("--liquid", default="BBCA", help="liquid blue chip to probe")
    ap.add_argument("--thin", default="HUMI", help="second-liner to probe")
    ap.add_argument("--batch", default="BBCA,GOTO,HUMI",
                    help="comma-separated codes for the batch cost test")
    ap.add_argument("--stale-wait", type=int, default=60,
                    help="seconds between the two order-book snapshots")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, spend nothing")
    args = ap.parse_args()

    now = wib_now()
    today = now.strftime("%Y-%m-%d")
    yday = prev_session(now)
    live = in_session(now)

    if args.dry_run:
        print(f"Would probe {args.liquid} / {args.thin} on {today} "
              f"(prev session {yday}), budget {args.budget}, "
              f"in-session={live}. No requests made.")
        return 0

    key = os.environ.get("INVEZGO_API_KEY", "").strip()
    if not key:
        print("[invezgo] ERROR: INVEZGO_API_KEY not set.\n"
              "  Set it as a Windows User environment variable, then open a NEW shell:\n"
              '    setx INVEZGO_API_KEY "your-key-here"\n'
              "  Key comes from https://invezgo.com/setting/api", file=sys.stderr)
        return 2

    print(f"[invezgo] WIB {now:%Y-%m-%d %H:%M:%S} | in-session={live} | budget={args.budget}",
          file=sys.stderr)
    if not live:
        print("[invezgo] WARNING: outside RG session. Live order-book and staleness probes "
              "(B1/B2) will not be meaningful; tape probes still are.", file=sys.stderr)

    client = InvezgoClient(api_key=key)
    led = Ledger(client, budget=args.budget)
    led.calibrate()

    a = client.analysis
    b = client.batch
    liq, thin = args.liquid.upper(), args.thin.upper()
    batch_codes = [c.strip().upper() for c in args.batch.split(",") if c.strip()]

    aborted = None
    # Pre-bound so a budget abort before the B block cannot NameError on the verdict below.
    first: dict = {}
    second: dict = {}
    try:
        # ---- A: tape economics. The decisive block. ----
        led.probe(
            "A1-tape-liquid-p1",
            f"How many pages is a full day of {liq} tape? (limit=150 is the API max)",
            lambda: a.get_stock_running_trade(code=liq, date=today, limit=150, page=1),
            capture=_pages)

        led.probe(
            "A2-tape-thin-p1",
            f"Same for the second-liner {thin} — is tape affordable where it matters most?",
            lambda: a.get_stock_running_trade(code=thin, date=today, limit=150, page=1),
            capture=_pages)

        led.probe(
            "A3-tape-by-volume",
            "Does orderby=VOLUME&sort=DESC return the day's biggest tickets in ONE call? "
            "If yes, this replaces full-tape archiving entirely.",
            lambda: a.get_stock_running_trade(code=liq, date=today, limit=150,
                                              orderby="VOLUME", sort="DESC"),
            capture=_pages)

        led.probe(
            "A4-tape-min-volume",
            "Does the server-side `minimum` share filter cut the page count?",
            lambda: a.get_stock_running_trade(code=liq, date=today, limit=150,
                                              minimum=100_000, page=1),
            capture=_pages)

        led.probe(
            "A5-tape-history",
            f"Is prior-session tape ({yday}) available, or is it same-day only?",
            lambda: a.get_stock_running_trade(code=liq, date=yday, limit=150, page=1),
            capture=_pages)

        led.probe(
            "A6-tape-negotiated",
            "Is NG (negotiated) tape exposed? That is the crossing/block detector the v4 "
            "plan deferred for lack of a source.",
            lambda: a.get_stock_running_trade(code=liq, date=today, limit=150,
                                              market="NG"),
            capture=_pages)

        # ---- B: order book reality and cost ----
        first = led.probe(
            "B1-orderbook",
            f"How many levels does the {liq} order book actually return?",
            lambda: a.get_order_book(code=liq, market="RG"),
            capture=_book)

        if live and first.get("ok"):
            print(f"[invezgo] waiting {args.stale_wait}s for the staleness diff...",
                  file=sys.stderr)
            time.sleep(args.stale_wait)
        second = led.probe(
            "B2-orderbook-repeat",
            f"Does the book CHANGE after {args.stale_wait}s? If not, 'live' is cached or "
            "delayed and the intraday use case is dead regardless of spend.",
            lambda: a.get_order_book(code=liq, market="RG"),
            capture=_book)

        led.probe(
            "B3-batch-orderbook",
            f"Does a {len(batch_codes)}-code batch cost 1 request or {len(batch_codes)}? "
            "SDK docs say max 3 codes for MAX role, 10 for ELITE.",
            lambda: b.get_order_book(code=batch_codes, market="RG"),
            capture=lambda r: {"n_returned": len(r) if isinstance(r, list) else None,
                               "requested": len(batch_codes), "shape": _trim(r)})

        led.probe(
            "B4-batch-historical",
            "Batch order-book accepts date+time. If historical snapshots work, the book can "
            "be reconstructed retrospectively instead of polled live — that changes everything.",
            lambda: b.get_order_book(code=batch_codes[:3], market="RG",
                                     date=yday, time="10:30"),
            capture=lambda r: {"n_returned": len(r) if isinstance(r, list) else None,
                               "shape": _trim(r)})

        # ---- C: intraday, as a hedge against the Yahoo 1m dependency ----
        led.probe(
            "C1-intraday",
            "Is intraday a viable fallback if Yahoo's unofficial 1m endpoint breaks? "
            "(v4 plan lists that as an unmitigated risk.)",
            lambda: a.get_intraday(code=liq, market="RG"),
            capture=lambda r: {"bars": len(r) if isinstance(r, list) else None,
                               "fields": sorted(r[0].keys()) if isinstance(r, list)
                                         and r and isinstance(r[0], dict) else None,
                               "first": r[0] if isinstance(r, list) and r else None,
                               "last": r[-1] if isinstance(r, list) and r else None})

    except BudgetExceeded as e:
        aborted = str(e)
        led._log(f"ABORTED on budget: {e}")

    # ---- staleness verdict ----
    stale_verdict = None
    if first.get("ok") and second.get("ok"):
        f1 = _book_fingerprint(first.get("sample") or {})
        f2 = _book_fingerprint(second.get("sample") or {})
        if f1 and f2:
            stale_verdict = ("CHANGED — book is live" if f1 != f2
                             else f"IDENTICAL after {args.stale_wait}s — cached or delayed")

    out = {
        "date": today,
        "generated_at": now.isoformat(),
        "in_session": live,
        "liquid": liq, "thin": thin, "prev_session": yday,
        "budget": args.budget,
        "requests_made": led.calls,
        "usage_endpoint_self_cost": led.usage_self_cost,
        "plan_limit_reported": led.limit_reported,
        "usage_final": led._last_usage,
        "staleness_verdict": stale_verdict,
        "aborted": aborted,
        "probes": led.rows,
        "errors": [r["error"] for r in led.rows if r.get("error")],
    }
    BUILD.mkdir(parents=True, exist_ok=True)
    path = BUILD / f"invezgo-probe-{today}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- console summary ----
    print()
    print(f"{'probe':<26} {'cost':>5} {'ok':>4}  finding")
    print("-" * 100)
    for r in led.rows:
        f = r.get("finding") or {}
        bits = []
        if "total_pages" in f and f["total_pages"] is not None:
            bits.append(f"pages={f['total_pages']} (={f['total_pages']} req)")
        if f.get("rows_this_page") is not None:
            bits.append(f"rows={f['rows_this_page']}")
        if f.get("has_broker_tags"):
            bits.append("broker-tagged")
        if f.get("volume_max") is not None:
            bits.append(f"maxvol={f['volume_max']:,}")
        if f.get("bid_levels") is not None:
            bits.append(f"bid_levels={f['bid_levels']}")
        if f.get("n_returned") is not None:
            bits.append(f"returned={f['n_returned']}/{f.get('requested','?')}")
        if f.get("bars") is not None:
            bits.append(f"bars={f['bars']}")
        cost = r["measured_cost"]
        print(f"{r['probe']:<26} {('?' if cost is None else cost):>5} "
              f"{('Y' if r['ok'] else 'N'):>4}  {' | '.join(bits) or (r.get('error') or '')[:70]}")
    print("-" * 100)
    print(f"requests made: {led.calls} / budget {args.budget}"
          + (f" | plan limit {led.limit_reported}" if led.limit_reported else ""))
    if stale_verdict:
        print(f"order book: {stale_verdict}")
    if aborted:
        print(f"ABORTED: {aborted}")
    print(f"written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
