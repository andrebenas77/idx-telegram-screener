#!/usr/bin/env python3
"""
Broker-cohort flow for the crowded tickers — "who is actually buying".

    py scripts/fetch_brokers.py                 # top-N crowded from today's mentions
    py scripts/fetch_brokers.py --tickers BBCA,TINS
    py scripts/fetch_brokers.py --date 2026-08-04

One /broker-summary/ call per ticker (1 credit) returns EVERY broker's buy/sell/net
for up to 14 days. From that single payload we derive, at no extra cost:

  * net flow per behavioural group   (institutional / hnw / scalper / retail / other)
  * net FOREIGN flow                 — summing foreign brokers reproduces the official
                                       /foreign-flow/ figure exactly (verified on BBCA
                                       2026-08-04: Rp398,344,810,000)
  * accumulation runs                — consecutive sessions a group has been net long
  * ticket ratio                     — each group's average trade size vs the stock's
                                       own median, so "retail-sized" travels across a
                                       Rp50 stock and a Rp30,000 one

Groups come from reference/brokers.csv (behavioural, hand-edited). `is_foreign` comes
from the Sectors registry, cached to reference/broker-registry.json.

Writes build/brokers-<date>.json. Degrades to available:false rather than crashing.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sectors_client import SectorsClient, strip_jk  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
CONFIG = ROOT / "reference" / "config.json"
BROKER_MAP = ROOT / "reference" / "brokers.csv"
REGISTRY = ROOT / "reference" / "broker-registry.json"

WIB = timezone(timedelta(hours=7))
GROUPS = ("institutional", "hnw", "scalper", "retail", "other")
REGISTRY_MAX_AGE_DAYS = 30

DEFAULTS = {
    "enabled": True,
    "top_n": 15,
    "history_days": 14,
    "min_freq_for_median": 5,
    "retail_ticket_ratio": 0.5,
    "inst_ticket_ratio": 1.5,
    "top_brokers_shown": 5,
    "credit_ceiling": 60,
}


# --------------------------------------------------------------------------- config

def config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG.read_text(encoding="utf-8")).get("brokers", {})
        cfg.update({k: v for k, v in raw.items() if not k.startswith("_")})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def load_broker_map() -> dict[str, str]:
    """code -> group, from the hand-edited behavioural taxonomy."""
    out: dict[str, str] = {}
    if not BROKER_MAP.exists():
        print(f"[!!] {BROKER_MAP} missing — every broker will bucket as 'other'",
              file=sys.stderr)
        return out
    for line in BROKER_MAP.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.lower().startswith("code,"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0] and parts[1] in GROUPS:
            out[parts[0].upper()] = parts[1]
    return out


def load_registry(c: SectorsClient) -> dict[str, bool]:
    """code -> is_foreign. Cached ~30 days; brokers rarely change and it costs a credit.

    Committed to the repo so an offline or key-less run still classifies foreign flow.
    """
    cached = None
    if REGISTRY.exists():
        try:
            cached = json.loads(REGISTRY.read_text(encoding="utf-8"))
            age = time.time() - cached.get("fetched_epoch", 0)
            if age < REGISTRY_MAX_AGE_DAYS * 86400:
                return {k.upper(): bool(v) for k, v in cached["is_foreign"].items()}
        except (OSError, json.JSONDecodeError, KeyError):
            cached = None

    rows = c.brokers()
    if not rows:
        if cached:
            print("[brokers] registry refresh failed — using the stale cached copy")
            return {k.upper(): bool(v) for k, v in cached.get("is_foreign", {}).items()}
        print("[!!] no broker registry — foreign flow cannot be derived", file=sys.stderr)
        return {}

    is_foreign = {r["code"].upper(): bool(r.get("is_foreign"))
                  for r in rows if r.get("code")}
    names = {r["code"].upper(): r.get("name", "") for r in rows if r.get("code")}
    try:
        REGISTRY.write_text(json.dumps({
            "fetched_at": datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB"),
            "fetched_epoch": time.time(),
            "count": len(is_foreign),
            "is_foreign": is_foreign,
            "names": names,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[brokers] registry refreshed — {len(is_foreign)} brokers")
    except OSError as e:
        print(f"[!!] could not cache registry ({e})", file=sys.stderr)
    return is_foreign


def broker_names() -> dict[str, str]:
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8")).get("names", {})
    except (OSError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------- inputs

def tickers_from_mentions(date: str | None):
    """Crowded tickers for the session, most-posted first."""
    paths = sorted(BUILD.glob("mentions-*.json"), reverse=True)
    if date:
        want = BUILD / f"mentions-{date}.json"
        paths = [want] + [p for p in paths if p != want]
    for p in paths:
        if not p.exists():
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = payload.get("tickers") or payload.get("top") or []
        out = []
        for r in rows:
            t = (r.get("ticker") if isinstance(r, dict) else r)
            if t:
                out.append(strip_jk(str(t)).upper())
        if out:
            return out, payload.get("date") or p.stem.replace("mentions-", "")
    return [], None


def resolve_session(c: SectorsClient, wanted: str | None, max_back: int = 5):
    """Find a session the API will actually serve.

    Same UTC-rollover problem fetch_flows hit: at 07:00 WIB it is still yesterday in
    UTC, so today's date is rejected as "in the future". Stepping back also absorbs
    weekends and IDX holidays.
    """
    probe = wanted or datetime.now(WIB).strftime("%Y-%m-%d")
    try:
        start = datetime.strptime(probe, "%Y-%m-%d")
    except ValueError:
        return None
    for back in range(0, max_back + 1):
        day = (start - timedelta(days=back)).strftime("%Y-%m-%d")
        payload = c.broker_summary("BBCA", start=day, end=day)
        if payload and payload.get("data"):
            if back:
                print(f"[brokers] {probe} unavailable upstream — using session {day}")
            return day
    return None


# --------------------------------------------------------------------------- analysis

def classify(code: str, bmap: dict[str, str]) -> str:
    return bmap.get(code.upper(), "other")


def day_groups(summary: list[dict], bmap: dict[str, str],
               foreign: dict[str, bool]) -> tuple[dict, int, list[str]]:
    """Aggregate one day's per-broker rows into groups + derived foreign net."""
    agg = {g: {"net_idr": 0, "buy_idr": 0, "sell_idr": 0, "freq": 0} for g in GROUPS}
    net_foreign = 0
    unknown: list[str] = []

    for row in summary:
        code = (row.get("broker_code") or "").upper()
        if not code:
            continue
        bval = row.get("bval") or 0
        sval = row.get("sval") or 0
        nval = row.get("nval") or 0
        freq = (row.get("bfreq") or 0) + (row.get("sfreq") or 0)

        g = classify(code, bmap)
        agg[g]["net_idr"] += nval
        agg[g]["buy_idr"] += bval
        agg[g]["sell_idr"] += sval
        agg[g]["freq"] += freq

        if code in foreign:
            if foreign[code]:
                net_foreign += nval
        else:
            # Not in the registry — counted as domestic. Surfaced so a large unknown
            # flow can't silently distort the foreign number.
            unknown.append(code)

    return agg, net_foreign, unknown


def ticket_stats(summary: list[dict], bmap: dict[str, str], min_freq: int):
    """Median value-per-trade for the stock, and each group's average ticket.

    Relative, not absolute: a fixed rupiah cut-off misclassifies across price levels,
    since the same rupiah ticket is 128x the lots on a Rp50 stock as on a Rp6,400 one.
    """
    per_broker = []
    grp_val: dict[str, float] = defaultdict(float)
    grp_freq: dict[str, int] = defaultdict(int)

    for row in summary:
        code = (row.get("broker_code") or "").upper()
        freq = (row.get("bfreq") or 0) + (row.get("sfreq") or 0)
        val = (row.get("bval") or 0) + (row.get("sval") or 0)
        if not code or freq <= 0:
            continue
        g = classify(code, bmap)
        grp_val[g] += val
        grp_freq[g] += freq
        if freq >= min_freq:          # floor stops 1-trade brokers skewing the median
            per_broker.append(val / freq)

    median = statistics.median(per_broker) if per_broker else 0.0
    out = {}
    for g in GROUPS:
        if grp_freq[g] > 0:
            vpt = grp_val[g] / grp_freq[g]
            out[g] = {"value_per_trade": round(vpt),
                      "ticket_ratio": round(vpt / median, 3) if median else None}
        else:
            out[g] = {"value_per_trade": None, "ticket_ratio": None}
    return round(median), out


def runs(days: list[tuple[str, dict]]) -> dict[str, dict]:
    """Consecutive sessions each group has been net long / net short, latest first."""
    out = {}
    for g in GROUPS:
        n, direction = 0, None
        for _, agg in days:
            net = agg[g]["net_idr"]
            if net == 0:
                break
            d = "in" if net > 0 else "out"
            if direction is None:
                direction = d
            if d != direction:
                break
            n += 1
        out[g] = {"run_sessions": n, "run_direction": direction}
    return out


def anomalies(groups: dict, tickets: dict, cfg: dict) -> list[str]:
    """Brokers acting out of character — the map says one thing, the tape another."""
    out = []
    r = tickets.get("retail", {}).get("ticket_ratio")
    i = tickets.get("institutional", {}).get("ticket_ratio")
    if r is not None and r > 1.0:
        out.append(f"retail ticket {r}x the stock median — unusually large for retail")
    if i is not None and i < cfg["retail_ticket_ratio"]:
        out.append(f"institutional ticket {i}x the stock median — retail-sized blocks")
    return out


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Broker-cohort flow for crowded tickers.")
    ap.add_argument("--date", help="session date YYYY-MM-DD (default: latest available)")
    ap.add_argument("--tickers", help="comma-separated override")
    ap.add_argument("--top-n", type=int, help="how many tickers to measure")
    ap.add_argument("--credit-ceiling", type=int,
                    help="override brokers.credit_ceiling (also makes the guard testable)")
    ap.add_argument("--out", help="output path")
    args = ap.parse_args()

    cfg = config()
    if args.credit_ceiling is not None:
        cfg["credit_ceiling"] = args.credit_ceiling
    top_n = args.top_n or cfg["top_n"]
    names = broker_names()

    if args.tickers:
        priority = [strip_jk(t).strip().upper() for t in args.tickers.split(",") if t.strip()]
        mention_date = args.date
    else:
        priority, mention_date = tickers_from_mentions(args.date)
        if priority:
            print(f"[brokers] priority from mentions-{mention_date}: "
                  f"{','.join(priority[:top_n])}")

    session_label = args.date or mention_date or datetime.now(WIB).strftime("%Y-%m-%d")
    c = SectorsClient(date=session_label)

    out = {
        # `date` is the MARKET session actually measured (may be yesterday, since the
        # board is built pre-open). `session_label` is the screener's own session and
        # is what the filename uses, so build_screener finds it by today's date.
        "date": session_label,
        "session_label": session_label,
        "generated_at": datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB"),
        "available": False,
        "history_days": cfg["history_days"],
        "tickers": {},
        "unknown_brokers": [],
        "notes": [],
        "errors": [],
    }

    if not cfg["enabled"]:
        out["errors"].append("brokers.enabled is false in reference/config.json")
        return write(out, args, c)
    if not c.enabled:
        out["errors"].append("SECTORS_API_KEY not set — broker flow unavailable")
        return write(out, args, c)
    if not priority:
        out["errors"].append("no mentions file — nothing to measure")
        return write(out, args, c)

    bmap = load_broker_map()
    foreign = load_registry(c)
    if not bmap:
        out["notes"].append("reference/brokers.csv missing — groups will all be 'other'")

    session = resolve_session(c, args.date or mention_date)
    if not session:
        out["errors"].append("could not resolve a tradeable session")
        return write(out, args, c)

    out["date"] = session
    c.rekey(session)                       # share the cache with the rest of the run
    start = (datetime.strptime(session, "%Y-%m-%d")
             - timedelta(days=cfg["history_days"])).strftime("%Y-%m-%d")

    unknown_all: set[str] = set()
    measured = 0

    for sym in priority[:top_n]:
        if c.credits >= cfg["credit_ceiling"]:
            msg = (f"credit ceiling {cfg['credit_ceiling']} reached after {measured} "
                   f"tickers — remaining names degrade to '-'")
            print(f"[brokers] {msg}")
            out["notes"].append(msg)
            break

        payload = c.broker_summary(sym, start=start, end=session)
        if not payload or not payload.get("data"):
            out["errors"].append(f"{sym}: no broker summary")
            continue

        # API returns oldest-first; we want latest-first for run counting.
        days_raw = sorted(payload["data"], key=lambda d: d.get("date", ""), reverse=True)
        if not days_raw:
            continue

        parsed = []
        for d in days_raw:
            agg, nf, unk = day_groups(d.get("summary") or [], bmap, foreign)
            unknown_all.update(unk)
            parsed.append((d.get("date"), agg, nf))

        latest_date, latest_agg, latest_foreign = parsed[0]
        median_vpt, tickets = ticket_stats(days_raw[0].get("summary") or [],
                                           bmap, cfg["min_freq_for_median"])
        run = runs([(dt, agg) for dt, agg, _ in parsed])

        groups = {}
        for g in GROUPS:
            groups[g] = {**latest_agg[g], **tickets[g], **run[g]}

        # Named movers, so the board can show WHO rather than only which bucket.
        rows = sorted((days_raw[0].get("summary") or []),
                      key=lambda r: r.get("nval") or 0)
        def brief(r):
            code = (r.get("broker_code") or "").upper()
            return {"code": code, "name": names.get(code, ""),
                    "group": classify(code, bmap), "net_idr": r.get("nval") or 0}
        k = cfg["top_brokers_shown"]

        out["tickers"][sym] = {
            "symbol": sym,
            "session": latest_date,
            "net_foreign_idr": latest_foreign,
            "stock_median_value_per_trade": median_vpt,
            "groups": groups,
            "top_buyers": [brief(r) for r in reversed(rows[-k:])],
            "top_sellers": [brief(r) for r in rows[:k]],
            "anomalies": anomalies(groups, tickets, cfg),
        }
        measured += 1

    out["available"] = measured > 0
    out["unknown_brokers"] = sorted(unknown_all)
    if unknown_all:
        out["notes"].append(
            f"{len(unknown_all)} broker code(s) not in the registry, counted as "
            f"domestic: {', '.join(sorted(unknown_all))}")

    print(f"[brokers] measured {measured}/{min(len(priority), top_n)} tickers "
          f"for session {session}")
    return write(out, args, c)


def write(out: dict, args, c: SectorsClient) -> int:
    out["credits_used"] = c.credits
    out["cache_hits"] = c.cache_hits
    out["errors"].extend(c.errors)
    BUILD.mkdir(parents=True, exist_ok=True)
    label = out.get("session_label") or out.get("date")
    path = Path(args.out) if args.out else BUILD / f"brokers-{label}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[brokers] wrote {path}")
    print(f"[brokers] session={out.get('date')} available={out['available']} "
          f"credits={c.credits} cache_hits={c.cache_hits}")
    if out["errors"]:
        print(f"[brokers] {len(out['errors'])} error(s): {out['errors'][:3]}",
              file=sys.stderr)
    c.report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
