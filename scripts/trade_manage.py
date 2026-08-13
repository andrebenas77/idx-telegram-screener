#!/usr/bin/env python3
"""Pre-close: for each open position, hold or exit.

Runs at 15:40 WIB, off the 15:35 bar — the last complete 5-minute bar before the
close, leaving ~15 minutes to act.

Only two rules decide anything, and both were validated on 915 trades over two years:

  E1  the hard stop was touched          -> you are already out; record the fill
  E2  price is below the 5-SESSION low   -> exit, this is a structure break

E2 is deliberately a CLOSE-based rule. The prior-day low is not a level this system
will ever act on: GGRM on 2026-08-10 breached Friday's low by Rp25 (0.12%) and closed
back above it, and acting on that intraday probe is what turned a live position into
a realised loss. A 15:40 reading is the closest thing to a close that still leaves
time to trade.

Everything else printed here — VWAP, CLV, the share of the session spent below VWAP,
whether the high of day came from the opening range — is CONTEXT. Those were all
tested as decision rules and none survived: the best intraday gate landed at the 60th
percentile of random filters taking the same number of trades. They are shown because
they inform a discretionary judgement, which is yours to make, not the system's.

Advisory only. Nothing places an order.

Usage:
    py scripts/trade_manage.py --dry-run
    py scripts/trade_manage.py --notify
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import Panel  # noqa: E402
from intraday_lib import (BROWSER_UA, PRECLOSE, bar_at, frac_below,  # noqa: E402
                          intraday_clv, opening_range, parse_payload, pin_ipv4,
                          reference_series, vwap_series)
from invezgo_client import InvezgoClient  # noqa: E402
from position_book import rebuild as book_state  # noqa: E402
from trade_lib import (SHARES_PER_LOT, RiskConfig, config_from_env,  # noqa: E402
                       low_n_prior)
from trade_plan import rupiah  # noqa: E402

WIB = timezone(timedelta(hours=7))
NOTIFY = Path(__file__).resolve().parent / "notify_telegram.py"


def today() -> str:
    return datetime.now(WIB).strftime("%Y-%m-%d")


def fetch_today(c: InvezgoClient, sym: str, day: str):
    """Today's 5-minute bars.

    `from == to` returns [] on this endpoint, so the range runs from yesterday and the
    result is filtered. Whether the feed serves a PARTIAL session, and with what lag,
    is the one thing that cannot be checked outside market hours — if this comes back
    empty at 15:40 the script says so rather than reporting a stale session as live.
    """
    prev = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
    nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    bars = parse_payload(c.multi_time_chart(sym, prev, nxt, 5))
    return [b for b in bars if b.date == day and b.hhmm >= "09:00"]


def assess(pos: dict, bars, l5: float | None, cfg: RiskConfig) -> dict:
    """Verdict plus context. Verdict uses only E1/E2."""
    i = bar_at(bars, PRECLOSE) if bars else None
    if i is None:
        return {"symbol": pos["symbol"], "stale": True}
    px = bars[i].c
    ref = reference_series(bars)
    orng = opening_range(bars)
    hod = max(b.h for b in bars[:i + 1])
    r_idr = pos.get("r_idr") or 0
    out = {
        "symbol": pos["symbol"], "lots": pos["lots"], "entry_px": pos["entry_px"],
        "stop_px": pos["stop_px"], "px": px, "lo_5d": l5,
        "R": ((px - pos["entry_px"]) * pos["lots"] * SHARES_PER_LOT / r_idr) if r_idr else None,
        "pnl": (px - pos["entry_px"]) * pos["lots"] * SHARES_PER_LOT,
        "vwap": ref[i], "is_vwap": vwap_series(bars) is not None,
        "vs_vwap": px / ref[i] - 1 if ref[i] else 0.0,
        "frac_below": frac_below(bars, ref, i),
        "clv": intraday_clv(bars, i),
        "hod_in_or": (hod <= orng["hi"]) if orng else None,
        "stale": False,
    }
    low_today = min(b.l for b in bars[:i + 1])
    if pos["stop_px"] and low_today <= pos["stop_px"]:
        out["verdict"], out["rule"] = "EXIT", "E1 stop touched"
    elif l5 and px < l5:
        out["verdict"], out["rule"] = "EXIT", f"E2 below the 5-session low ({l5:,.0f})"
    else:
        out["verdict"], out["rule"] = "HOLD", ""
    return out


def summary_text(rows: list[dict], day: str, cfg: RiskConfig) -> str:
    L = [f"IDX TRADE - pre-close {day} {PRECLOSE}"]
    L.append("-" * 30)
    if not rows:
        L.append("no open positions")
        L.append("-" * 30)
        L.append("Advisory only. No orders are placed.")
        return "\n".join(L)

    for r in rows:
        if r.get("stale"):
            L.append(f"[warn] {r['symbol']}: no bars for today - cannot assess. "
                     f"Check manually before the close.")
            continue
        tag = "[!!] EXIT" if r["verdict"] == "EXIT" else "[ok] HOLD"
        L.append(f"{tag}  {r['symbol']} {r['lots']} lots @{r['entry_px']:,.0f}")
        rr = f"{r['R']:+.2f}R" if r["R"] is not None else "n/a"
        L.append(f"  px {r['px']:,.0f} | {rr} | {rupiah(r['pnl'])} | stop {r['stop_px']:,.0f}")
        if r["verdict"] == "EXIT":
            L.append(f"  rule: {r['rule']}")
            L.append(f"  >> sell {r['lots']} lots before 16:00")
            L.append(f"  record: position_book.py close {r['symbol']} --px <fill>")
        else:
            L.append(f"  5-session low {r['lo_5d']:,.0f} - intact")
        ctx = (f"  context (not rules): {'VWAP' if r['is_vwap'] else 'TWAP'} "
               f"{r['vwap']:,.0f} ({r['vs_vwap']:+.2%}), below {r['frac_below']:.0%} of session")
        if r["clv"] is not None:
            ctx += f", CLV {r['clv']:.2f}"
        if r["hod_in_or"]:
            ctx += ", high of day was set in the opening range"
        L.append(ctx)
        L.append("")
    L.append("-" * 30)
    L.append("Verdict uses E1/E2 only (VALIDATED n=915, 2y). Context lines were")
    L.append("tested as rules and rejected - they inform your judgement, not the call.")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", type=str)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notify", action="store_true")
    a = ap.parse_args()

    cfg, warn = config_from_env()
    st = book_state()
    positions = list(st["positions"].values())
    day = a.date or today()

    if not positions:
        print(f"[ok] no open positions on {day} — nothing to manage")
        if a.notify:
            return 0          # silence is correct; do not push an empty message
        return 0

    p = Panel()
    p.load_prices()
    prior = [d for d in p.dates if d < day]
    i = p.didx.get(max(prior)) if prior else None
    # A stale panel silently produces a WRONG 5-session low, and E2 is the only rule
    # that can tell you to sell. Refuse to be quietly wrong about it.
    if i is not None:
        # Count WEEKDAYS between the panel's last session and today. A calendar-day
        # threshold reads Friday->Monday as a 3-day gap and would either cry wolf
        # every Monday or, if loosened to compensate, miss a genuinely missing
        # session — which is what happened here: the panel ended Aug 6 while the
        # decision was for Aug 10, so E2 was silently using a 5-session low that
        # excluded Aug 7.
        d0 = datetime.strptime(p.dates[i], "%Y-%m-%d")
        d1 = datetime.strptime(day, "%Y-%m-%d")
        missed = sum(1 for n in range(1, (d1 - d0).days)
                     if (d0 + timedelta(days=n)).weekday() < 5)
        if missed:
            warn.append(f"daily panel ends {p.dates[i]} but this is {day} — "
                        f"{missed} trading session(s) missing, so the 5-session low "
                        f"behind E2 is STALE. Run backfill_panel.py first.")

    pin_ipv4()
    c = InvezgoClient(user_agent=BROWSER_UA, use_cache=False, verbose=False)
    rows = []
    for pos in positions:
        bars = fetch_today(c, pos["symbol"], day)
        l5 = low_n_prior(p, pos["symbol"], i, cfg.struct_lookback) if i is not None else None
        rows.append(assess(pos, bars, l5, cfg))

    text = summary_text(rows, day, cfg)
    if warn:
        text += "\n" + "\n".join(f"[warn] {w}" for w in warn)
    print(text)
    print(f"\n[{c.requests_used} requests used]")

    if a.notify and not a.dry_run:
        r = subprocess.run([sys.executable, str(NOTIFY), "--title",
                            f"IDX TRADE - pre-close {day}", "--text", text],
                           capture_output=True, text=True, timeout=60)
        print(r.stdout.strip() or r.stderr.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
