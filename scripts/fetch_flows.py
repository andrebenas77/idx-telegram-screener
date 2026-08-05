#!/usr/bin/env python3
"""DEPRECATED (v3) — superseded by scripts/fetch_brokers.py.

fetch_brokers.py gets the same net foreign figure from /v2/broker-summary/ by summing
the foreign brokers' net, which reproduces this script's /v2/foreign-flow/ output
EXACTLY (verified on BBCA 2026-08-04: Rp398,344,810,000) — while also returning the
behavioural cohort split, ticket sizes and 14 days of history, for fewer credits.

build_screener.py prefers brokers-<date>.json and only falls back to flows-<date>.json
if it is missing. `reference/config.json → sectors.enabled` is now false. This file is
kept for one release so a bad v3 run has somewhere to fall back to; delete it after
v3 has run clean for a couple of weeks.

Assemble the daily IDX foreign-flow picture from the Sectors API.

There is no market-wide "foreign flow by ticker" endpoint — /v2/foreign-flow/ is
per-symbol only, and covering the whole exchange would be ~900 calls a day. So this
runs three stages:

  Stage 1  Candidate generation (derived).
           Rank foreign brokers by net for the session, then pull each one's
           accumulations/distributions by ticker (up to 90 names each) and sum net IDR
           per ticker. Cheap, wide, and approximate.

  Stage 2  Exact measurement.
           For the shortlist — news/chatter tickers first, then the derived extremes —
           call /v2/foreign-flow/ directly. These numbers are exact and OVERRIDE the
           derived ones on the published board.

  Stage 3  Cohort split.
           /v2/broker-summary/{symbol}/top/ with cohort=institutional and cohort=retail,
           n_brokers=90 so the sum covers effectively every broker in the cohort. This is
           what turns "loud on Telegram" into "loud on Telegram AND institutions selling".

Usage:
    py scripts/fetch_flows.py                          # latest session, standard tier
    py scripts/fetch_flows.py --tickers BBCA,GOTO,ASII
    py scripts/fetch_flows.py --tier deep --date 2026-07-24

Writes build/flows-<date>.json. Never raises — on failure it writes an
`available: false` payload so downstream builds still render.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sectors_client import SectorsClient, strip_jk  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
CONFIG = ROOT / "reference" / "config.json"
WIB = timezone(timedelta(hours=7))


def screener_config() -> dict:
    """reference/config.json -> `sectors` block. Missing file or keys fall back to defaults."""
    try:
        return (json.loads(CONFIG.read_text(encoding="utf-8")) or {}).get("sectors", {}) or {}
    except Exception:
        return {}


def tickers_from_mentions(date: str | None):
    """Crowded tickers from the latest mentions-<date>.json, most-mentioned first.

    Screener variant: the priority list IS the chatter leaderboard, so flow gets measured
    exactly where the attention is — that pairing is the whole point of the signal.
    """
    p = (BUILD / f"mentions-{date}.json") if date else None
    if not p or not p.exists():
        cands = sorted(BUILD.glob("mentions-*.json"), reverse=True)
        if not cands:
            return [], None
        p = cands[0]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[flows] could not read {p.name}: {e}", file=sys.stderr)
        return [], None
    rows = sorted(d.get("tickers") or [], key=lambda r: r.get("posts", 0), reverse=True)
    return [strip_jk(r["ticker"]) for r in rows if r.get("ticker")], d.get("date")

# Per-broker/per-ticker breadth. 90 is the API max and costs the same as 10.
MAX_N = 90

# flow_tickers must comfortably exceed 20 so the published board (top 10 in + top 10 out)
# can be filled entirely from exactly-measured names. See BOARD ACCURACY below.
TIERS = {
    "lean":     {"brokers": 5,  "flow_tickers": 12, "cohort_tickers": 2},   # ~32 credits
    "standard": {"brokers": 8,  "flow_tickers": 20, "cohort_tickers": 3},   # ~50 credits
    "deep":     {"brokers": 15, "flow_tickers": 35, "cohort_tickers": 6},   # ~91 credits
}

# BOARD ACCURACY
# Stage 1 sums only the sampled brokers, so a ticker heavily traded by an unsampled
# broker can carry the wrong sign. Observed live on 2026-07-24: BBCA derived to
# -76.7bn (outflow) while /foreign-flow/ reported +26.7bn (inflow) — opposite
# direction. The published board therefore contains ONLY exactly-measured tickers;
# derived-but-unmeasured names are surfaced separately as unverified candidates.

TREND_LOOKBACK_DAYS = 14   # calendar days -> ~9 sessions
TREND_SESSIONS = 3         # consecutive-session test used by the signals


def parse_tickers(raw: str | None) -> list[str]:
    if not raw:
        return []
    out, seen = [], set()
    for part in raw.replace(";", ",").split(","):
        t = strip_jk(part)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def resolve_session_date(c: SectorsClient, wanted: str | None, max_back: int = 5):
    """Ask the API which session is current rather than guessing around weekends.

    /v2/brokers/top/ echoes the date it actually served, so one call both resolves the
    trading date and returns the Stage-1 broker list.

    Falls back to earlier sessions when `wanted` isn't available upstream. The screener
    labels its session in Jakarta time, but the API validates against UTC — so a morning
    run (07:00 WIB = 00:00 UTC) asks for a date the API still calls "in the future" and
    gets a 400. Stepping back a day at a time degrades to the last completed session
    instead of silently losing the whole Foreign column, and absorbs weekends and IDX
    holidays the same way. Prices are the previous close anyway, so the pairing is right.
    """
    payload = c.top_brokers(date=wanted, metric="net", origin="foreign",
                            n_brokers=TIERS["deep"]["brokers"])
    if payload:
        return payload.get("date") or wanted, payload
    if not wanted:
        return None, None

    try:
        start = datetime.strptime(wanted, "%Y-%m-%d")
    except ValueError:
        return None, None

    for back in range(1, max_back + 1):
        probe = (start - timedelta(days=back)).strftime("%Y-%m-%d")
        payload = c.top_brokers(date=probe, metric="net", origin="foreign",
                                n_brokers=TIERS["deep"]["brokers"])
        if payload:
            # The first attempt's 400 is expected on any pre-rollover run and is now
            # handled, so drop it. An unattended run that reports a daily "error"
            # which isn't one just teaches you to ignore the error line.
            c.errors[:] = [e for e in c.errors
                           if "Date cannot be in the future" not in e]
            print(f"[flows] {wanted} unavailable upstream — "
                  f"falling back to last completed session {probe}")
            return payload.get("date") or probe, payload
    return None, None


def stage1_candidates(c: SectorsClient, session: str, brokers: list[dict],
                      n_brokers: int):
    """Sum net IDR per ticker across the top foreign brokers for the session.

    Output is a CANDIDATE RANKING, not a measurement — see BOARD ACCURACY above.
    """
    codes = [b["broker_code"] for b in brokers[:n_brokers] if b.get("broker_code")]
    agg: dict[str, float] = defaultdict(float)
    contributors: dict[str, set] = defaultdict(set)
    used: list[str] = []

    for code in codes:
        data = c.broker_activity_top(code, start=session, end=session, n_brokers=MAX_N)
        if not data:
            continue
        used.append(code)
        for bucket in ("top_accumulations", "top_distributions"):
            for row in data.get(bucket) or []:
                sym = strip_jk(row.get("symbol"))
                net = row.get("net_idr")
                if not sym or net is None:
                    continue
                agg[sym] += float(net)
                contributors[sym].add(code)

    return used, agg, {s: sorted(v) for s, v in contributors.items()}


def trend_stats(rows: list[dict]):
    """Consecutive same-sign run ending at the latest session, plus a 3-session sum."""
    series = [(r.get("date"), float(r.get("net_foreign_inflow") or 0)) for r in rows]
    series = [s for s in series if s[0]]
    if not series:
        return None
    latest_net = series[-1][1]
    sign = 1 if latest_net > 0 else (-1 if latest_net < 0 else 0)
    run = 0
    if sign:
        for _, v in reversed(series):
            if (v > 0 and sign > 0) or (v < 0 and sign < 0):
                run += 1
            else:
                break
    tail = [v for _, v in series[-TREND_SESSIONS:]]
    return {
        "latest_date": series[-1][0],
        "latest_net": round(latest_net),
        "run_sessions": run,
        "run_direction": "in" if sign > 0 else ("out" if sign < 0 else "flat"),
        "sum_3": round(sum(tail)),
        "sessions": [{"date": d, "net": round(v)} for d, v in series[-6:]],
    }


def cohort_net(payload: dict | None):
    """Net IDR across every broker returned for a cohort (n_brokers=90 ~= all of them)."""
    if not payload:
        return None
    total = 0.0
    for bucket in ("top_buyers", "top_sellers"):
        for row in payload.get(bucket) or []:
            total += float(row.get("net_idr") or 0)
    return round(total)


def top_names(payload: dict | None, bucket: str, n: int = 3):
    if not payload:
        return []
    return [{"broker": r.get("broker_code"), "net_idr": round(float(r.get("net_idr") or 0))}
            for r in (payload.get(bucket) or [])[:n]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="session date YYYY-MM-DD (default: latest available)")
    ap.add_argument("--tickers", help="comma-separated priority tickers (news / chatter)")
    ap.add_argument("--tier", choices=sorted(TIERS),
                    help="override reference/config.json -> sectors.tier")
    ap.add_argument("--out", help="output path (default build/flows-<date>.json)")
    args = ap.parse_args()

    scfg = screener_config()
    args.tier = args.tier or scfg.get("tier", "standard")
    tier = dict(TIERS[args.tier])
    # Explicit limits in config.json win over the tier preset.
    tier["flow_tickers"] = int(scfg.get("flow_top_n", tier["flow_tickers"]))
    tier["cohort_tickers"] = int(scfg.get("cohort_top_n", tier["cohort_tickers"]))

    priority = parse_tickers(args.tickers)
    if not priority:
        priority, mention_date = tickers_from_mentions(args.date)
        if priority:
            print(f"[flows] priority from mentions-{mention_date}: {','.join(priority[:12])}")
            if not args.date:
                args.date = mention_date
        else:
            print("[flows] no mentions file — using derived candidates only", file=sys.stderr)
    c = SectorsClient(date=args.date or datetime.now(WIB).strftime("%Y-%m-%d"))

    out = {
        "date": args.date,
        "generated_at": datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB"),
        "tier": args.tier,
        "available": False,
        "market": {},
        "tickers": {},
        "notes": [],
        "errors": [],
    }

    if not c.enabled:
        out["errors"].append("SECTORS_API_KEY not set — flow data unavailable")
        return write(out, args, c)

    # ---- Stage 1 ----
    session, brokers_payload = resolve_session_date(c, args.date)
    if not session or not brokers_payload:
        out["errors"].append("could not resolve session date / broker ranking")
        return write(out, args, c)

    out["date"] = session
    c.rekey(session)  # group the shared cache by trading session, not run date
    brokers = brokers_payload.get("results") or []
    used, agg, contributors = stage1_candidates(c, session, brokers, tier["brokers"])

    # ---- Stage 2: measure the candidates exactly ----
    # Rank every derived candidate by |net| so the most extreme names get measured first.
    ranked_candidates = [s for s, _ in sorted(agg.items(),
                                              key=lambda kv: abs(kv[1]), reverse=True)]
    shortlist: list[str] = []
    for sym in priority + ranked_candidates:
        if sym not in shortlist:
            shortlist.append(sym)
    measured = shortlist[:tier["flow_tickers"]]
    unmeasured = [s for s in ranked_candidates if s not in measured][:10]

    out["market"] = {
        "method": (f"Candidates derived from the top {len(used)} foreign brokers by |net| "
                   f"for {session} (up to {MAX_N} names each), then measured exactly via "
                   f"/foreign-flow/. Only exactly-measured tickers appear on the board."),
        "brokers_used": used,
        "unverified_candidates": [
            {"symbol": s, "derived_net_idr": round(agg[s])} for s in unmeasured
        ],
    }
    shortlist = measured

    start = (datetime.strptime(session, "%Y-%m-%d")
             - timedelta(days=TREND_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    for sym in shortlist:
        flow = c.foreign_flow(sym, start=start, end=session)
        entry = {"symbol": sym, "in_priority": sym in priority}
        stats = trend_stats((flow or {}).get("data") or [])
        if stats:
            entry.update(stats)
            entry["exact"] = True
        out["tickers"][sym] = entry

    # Build the published board from exact measurements only, and audit Stage 1 against
    # them. A sign flip means the derived board would have pointed the wrong way.
    board_rows, sign_flips = [], []
    for sym, entry in out["tickers"].items():
        if not entry.get("exact") or entry.get("latest_net") is None:
            continue
        actual = entry["latest_net"]
        derived = round(agg[sym]) if sym in agg else None
        if derived is not None and (derived > 0) != (actual > 0):
            sign_flips.append({"symbol": sym, "derived": derived, "exact": actual})
        board_rows.append({
            "symbol": sym,
            "net_idr": actual,
            "derived_net_idr": derived,
            "run_sessions": entry.get("run_sessions"),
            "run_direction": entry.get("run_direction"),
            "sum_3": entry.get("sum_3"),
            "in_priority": entry.get("in_priority", False),
            "exact": True,
        })

    out["market"]["top_inflow"] = sorted(
        [r for r in board_rows if r["net_idr"] > 0],
        key=lambda r: r["net_idr"], reverse=True)[:10]
    out["market"]["top_outflow"] = sorted(
        [r for r in board_rows if r["net_idr"] < 0], key=lambda r: r["net_idr"])[:10]

    out["sign_flips"] = sign_flips
    out["notes"].append(
        f"Board built from {len(board_rows)} exactly-measured tickers "
        f"({len(out['market']['top_inflow'])} inflow / "
        f"{len(out['market']['top_outflow'])} outflow).")
    if sign_flips:
        out["notes"].append(
            "Stage 1 pointed the wrong way on " +
            ", ".join(s["symbol"] for s in sign_flips) +
            " - derived ranking is a candidate generator, never a measurement.")

    # ---- Stage 3: cohort split on the most interesting names ----
    cohort_pool = [s for s in shortlist if s in out["tickers"]][:tier["cohort_tickers"]]
    for sym in cohort_pool:
        inst = c.broker_summary_top(sym, start=session, end=session,
                                    cohort="institutional", n_brokers=MAX_N)
        retail = c.broker_summary_top(sym, start=session, end=session,
                                      cohort="retail", n_brokers=MAX_N)
        out["tickers"][sym].update({
            "inst_net": cohort_net(inst),
            "retail_net": cohort_net(retail),
            "inst_top_buyers": top_names(inst, "top_buyers"),
            "inst_top_sellers": top_names(inst, "top_sellers"),
        })

    out["available"] = bool(out["market"].get("top_inflow") or out["tickers"])
    return write(out, args, c)


def write(out: dict, args, c: SectorsClient) -> int:
    out["credits_used"] = c.credits
    out["cache_hits"] = c.cache_hits
    out["errors"].extend(c.errors)
    BUILD.mkdir(parents=True, exist_ok=True)
    path = Path(args.out) if args.out else BUILD / f"flows-{out.get('date') or 'unknown'}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[flows] wrote {path}")
    print(f"[flows] session={out.get('date')} tier={out['tier']} "
          f"available={out['available']} credits={c.credits} cache_hits={c.cache_hits}")
    if out["errors"]:
        print(f"[flows] {len(out['errors'])} error(s): {out['errors'][:3]}", file=sys.stderr)
    c.report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
