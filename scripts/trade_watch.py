#!/usr/bin/env python3
"""Intraday watcher: alert when an open position crosses a level that matters.

Deliberately alerts on FEW things. The intraday study rejected every entry gate it
tested, and the daily study rejected the prior-day-low stop that this watcher could
so easily be pointed at. So the alerts are:

  STOP      a 5-minute bar CLOSES below the hard stop      -> act
  E2 WATCH  price is below the 5-session low intraday      -> watch, decide at 15:35
  E2        the 15:35 bar is below the 5-session low       -> act at the close

Everything else — VWAP, CLV, the share of the session below VWAP, whether the high of
day came out of the opening range — is reported once per alert as context and never
triggers one.

Why a bar CLOSE and not a touch: across 9,205 IDX sessions that closed in the bottom
decile of their range, the next session traded below that low 68% of the time and
closed back above it in 29% of those cases. A touch is the norm after a weak close,
not information.

State lives in data/book/watch-<date>.json so a level alerts once, not every poll.

Usage:
    py scripts/trade_watch.py                 # one check, print only
    py scripts/trade_watch.py --notify        # one check, push if something crossed
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import Panel  # noqa: E402
from intraday_lib import (BROWSER_UA, bar_at, frac_below, intraday_clv,  # noqa: E402
                          opening_range, parse_payload, pin_ipv4, reference_series,
                          vwap_series)
from invezgo_client import InvezgoClient  # noqa: E402
from position_book import BOOK, rebuild as book_state  # noqa: E402
from trade_lib import SHARES_PER_LOT, config_from_env, low_n_prior  # noqa: E402

WIB = timezone(timedelta(hours=7))
NOTIFY = Path(__file__).resolve().parent / "notify_telegram.py"


def now_wib() -> datetime:
    return datetime.now(WIB)


def state_path(day: str) -> Path:
    return BOOK / f"watch-{day}.json"


def load_state(day: str) -> dict:
    p = state_path(day)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(day: str, st: dict) -> None:
    BOOK.mkdir(parents=True, exist_ok=True)
    state_path(day).write_text(json.dumps(st, indent=2), encoding="utf-8")


def fetch(c: InvezgoClient, sym: str, day: str):
    prev = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
    nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    bars = parse_payload(c.multi_time_chart(sym, prev, nxt, 5))
    return [b for b in bars if b.date == day and b.hhmm >= "09:00"]


def check(pos: dict, bars, l5, cfg) -> tuple[list[str], dict]:
    """Return (alert keys fired, context)."""
    if not bars:
        return [], {"stale": True}
    i = len(bars) - 1
    ref = reference_series(bars)
    orng = opening_range(bars)
    hod = max(b.h for b in bars)
    px = bars[i].c
    ctx = {
        "px": px, "hhmm": bars[i].hhmm,
        "R": ((px - pos["entry_px"]) * pos["lots"] * SHARES_PER_LOT / pos["r_idr"])
             if pos.get("r_idr") else None,
        "pnl": (px - pos["entry_px"]) * pos["lots"] * SHARES_PER_LOT,
        "vwap": ref[i], "is_vwap": vwap_series(bars) is not None,
        "vs_vwap": (px / ref[i] - 1) if ref[i] else 0.0,
        "frac_below": frac_below(bars, ref, i),
        "clv": intraday_clv(bars, i),
        "low": min(b.l for b in bars),
        "hod_in_or": (hod <= orng["hi"]) if orng else None,
        "stale": False,
    }
    fired = []
    stop = pos.get("stop_px")
    if stop and px < stop:
        fired.append("STOP")
    if l5:
        if bars[i].hhmm >= "15:35" and px < l5:
            fired.append("E2")
        elif min(b.l for b in bars) < l5:
            fired.append("E2_WATCH")
    return fired, ctx


def line(sym: str, pos: dict, fired: list[str], c: dict, l5) -> str:
    L = []
    head = {"STOP": "[!!] STOP", "E2": "[!!] EXIT E2", "E2_WATCH": "[warn] watch"}
    tag = head.get(fired[0], "[ok]") if fired else "[ok]"
    r = f"{c['R']:+.2f}R" if c.get("R") is not None else "n/a"
    L.append(f"{tag}  {sym} {pos['lots']} lots @{pos['entry_px']:,.0f}")
    L.append(f"  {c['hhmm']} px {c['px']:,.0f} | {r} | Rp{c['pnl']/1e6:+,.1f}m "
             f"| stop {pos['stop_px']:,.0f} | 5d low {l5:,.0f}" if l5 else
             f"  {c['hhmm']} px {c['px']:,.0f} | {r} | stop {pos['stop_px']:,.0f}")
    if "STOP" in fired:
        L.append(f"  >> a 5m bar closed BELOW the stop. Sell {pos['lots']} lots.")
        L.append(f"  record: position_book.py close {sym} --px <fill>")
    elif "E2" in fired:
        L.append(f"  >> 15:35 print is below the 5-session low. Sell before 16:00.")
        L.append(f"  record: position_book.py close {sym} --px <fill>")
    elif "E2_WATCH" in fired:
        L.append(f"  >> traded below the 5-session low but has not closed there. "
                 f"No action yet - the rule is close-based. Decide at 15:35.")
    ref = "VWAP" if c["is_vwap"] else "TWAP"
    s = (f"  context: {ref} {c['vwap']:,.0f} ({c['vs_vwap']:+.2%}), "
         f"below {c['frac_below']:.0%} of session")
    if c.get("clv") is not None:
        s += f", CLV {c['clv']:.2f}"
    if c.get("hod_in_or"):
        s += ", HoD from the opening range"
    L.append(s)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", type=str)
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--force", action="store_true", help="report even if nothing crossed")
    a = ap.parse_args()

    cfg, _ = config_from_env()
    st_book = book_state()
    positions = list(st_book["positions"].values())
    if not positions:
        print("[ok] flat — nothing to watch")
        return 0

    day = a.date or now_wib().strftime("%Y-%m-%d")
    seen = load_state(day)
    p = Panel()
    p.load_prices()
    prior = [d for d in p.dates if d < day]
    i = p.didx.get(max(prior)) if prior else None

    pin_ipv4()
    c = InvezgoClient(user_agent=BROWSER_UA, use_cache=False, verbose=False)

    blocks, new_alerts = [], False
    for pos in positions:
        sym = pos["symbol"]
        bars = fetch(c, sym, day)
        l5 = low_n_prior(p, sym, i, cfg.struct_lookback) if i is not None else None
        fired, ctx = check(pos, bars, l5, cfg)
        if ctx.get("stale"):
            print(f"[warn] {sym}: no bars yet for {day}")
            continue
        fresh = [f for f in fired if seen.get(f"{sym}:{f}") != day]
        if fresh:
            new_alerts = True
            for f in fresh:
                seen[f"{sym}:{f}"] = day
        if fresh or a.force:
            blocks.append(line(sym, pos, fired, ctx, l5))

    save_state(day, seen)
    if not blocks:
        print(f"[ok] {now_wib():%H:%M} — nothing crossed")
        return 0

    text = (f"IDX TRADE - watch {day} {now_wib():%H:%M}\n" + "-" * 30 + "\n"
            + "\n\n".join(blocks) + "\n" + "-" * 30
            + "\nSTOP and E2 are the only rules. Context lines were tested and rejected.")
    print(text)
    print(f"\n[{c.requests_used} requests]")
    if a.notify and (new_alerts or a.force):
        r = subprocess.run([sys.executable, str(NOTIFY), "--title",
                            f"IDX TRADE - watch {day}", "--text", text],
                           capture_output=True, text=True, timeout=60)
        print(r.stdout.strip() or r.stderr.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
