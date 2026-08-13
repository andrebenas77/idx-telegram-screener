#!/usr/bin/env python3
"""Pre-open: turn the momentum board into sized positions with stops.

This script deliberately does LESS than the design it came from, because the backtest
rejected most of that design. What survived, and why:

  SHIPS  1.5x ATR stop, never at the prior session's low, plus a close-based
         5-session structure exit. On 915 trades over two years, measured against the
         SAME universe held naively: +0.82pp excess, +0.110 R, and the worst trade
         improves from -6.8R to -1.7R. Won 3 of 4 time folds.

  CUT    Every screener veto. The RVOL1 exhaustion filter and the CLV-aware structure
         filter were built from the ISAT and ERAA trades and looked compelling. Across
         915 trades they cost 0.66pp and 1.01pp respectively, and the veto set landed
         at the 16th percentile of random filters of the same size — worse than
         throwing darts. They are printed here as DIAGNOSTICS and gate nothing.

  CUT    The blow-off exit, the time stop, and trailing stops. Each degraded the
         result on its own (-0.03, -0.06, -0.09 R) and each won only 1 of 4 folds.

  CUT    The whole intraday entry layer. Best single gate reached the 60th percentile
         of random filters taking the same fraction of candidates. One useful thing
         did survive as a read-out: requiring a break of the opening-range high
         genuinely selects better names (+1.57pp on what it skips) — but you pay all
         of it back in a worse fill. That is why `hod_in_or` is shown and not acted on.

So the output is: what to buy, how many lots, where the stop goes, and what the
diagnostics say — with the diagnostics clearly marked as not-a-filter, the same way
the board already treats HH/HL.

Advisory only. Nothing places an order.

Usage:
    py scripts/trade_plan.py --dry-run
    py scripts/trade_plan.py --date 2026-08-06 --summary
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel  # noqa: E402
from position_book import rebuild as book_state  # noqa: E402
from trade_lib import (SHARES_PER_LOT, RiskConfig, admit, atr_series, beta,  # noqa: E402
                       config_from_env, low_n_prior, market_exposure,
                       portfolio_heat, round_tick, rvol1_series, size_position,
                       stop_price)

WIB = timezone(timedelta(hours=7))
ROOT = Path(__file__).resolve().parents[1]
BOARD = PANEL / "momentum_board.json"
OUT = PANEL / "trade_plan.json"
ENVF = ROOT / "secrets" / "trade.env"
SITE = "https://andrebenas77.github.io/idx-telegram-screener/momentum.html"


def rupiah(x: float) -> str:
    if abs(x) >= 1e9:
        return f"Rp{x / 1e9:.2f}bn"
    if abs(x) >= 1e6:
        return f"Rp{x / 1e6:.1f}m"
    return f"Rp{x:,.0f}"


def diagnostics(row: dict, rv1, hod_in_or=None) -> list[str]:
    """Read-outs, never filters. Each was TESTED as a filter and rejected."""
    d = []
    if rv1 is not None and rv1 >= 3.0:
        d.append(f"RVOL1 {rv1:.1f}")
    clv = row.get("clv")
    if clv is not None and clv <= 0.25:
        d.append(f"CLV {clv:.2f}")
    if row.get("trend") == "LH/LL":
        d.append("LH/LL")
    if (row.get("cmf20") or 0) < -0.10:
        d.append(f"CMF {row['cmf20']:+.2f}")
    return d


def build(p: Panel, session: str, cfg: RiskConfig) -> dict:
    board = json.loads(BOARD.read_text(encoding="utf-8")) if BOARD.exists() else {}
    rows = board.get("candidates") or []
    sess = board.get("session") or session
    i = p.didx.get(sess)
    if i is None:
        raise SystemExit(f"[!!] session {sess} is not in the daily panel — "
                         f"run backfill_panel.py first")

    st = book_state()
    equity = st["equity_idr"] or cfg.equity_idr
    cfg.equity_idr = equity
    positions = list(st["positions"].values())
    for pos in positions:
        pos.setdefault("beta", beta(p, pos["symbol"], i))

    out = {"session": sess, "generated": datetime.now(WIB).isoformat(timespec="seconds"),
           "equity_idr": equity, "heat": portfolio_heat(positions, equity),
           "beta_gross": market_exposure(positions, equity),
           "open": [], "candidates": [], "skipped": [],
           "rules": {"stop": "1.5*ATR14, widened to the 5-session low only within "
                             "2.5 ATR; NEVER the prior-day low",
                     "exit": "E1 hard stop, or E2 close below the 5-session low",
                     "tier": "VALIDATED (n=915, 2y, 3/4 folds, +0.82pp like-for-like)"}}

    for pos in positions:
        sym = pos["symbol"]
        px = (p.raw_close.get(sym) or {}).get(i)
        l5 = low_n_prior(p, sym, i, cfg.struct_lookback)
        out["open"].append({
            "symbol": sym, "lots": pos["lots"], "entry_px": pos["entry_px"],
            "stop_px": pos["stop_px"], "mark": px, "lo_5d": l5,
            "R": ((px - pos["entry_px"]) * pos["lots"] * SHARES_PER_LOT / pos["r_idr"])
                 if px and pos.get("r_idr") else None,
            "e2_armed": bool(px and l5 and px < l5)})

    atrs = {r["symbol"]: atr_series(p, r["symbol"]) for r in rows}
    rv1s = {r["symbol"]: rvol1_series(p, r["symbol"]) for r in rows}
    sized: list[dict] = []

    for r in rows:
        sym = r["symbol"]
        ref = (p.raw_close.get(sym) or {}).get(i) or r.get("close")
        a = atrs.get(sym, {}).get(i)
        if not ref or not a:
            out["skipped"].append({"symbol": sym, "reason": "no ATR / price"})
            continue
        stop, basis = stop_price(ref, a, low_n_prior(p, sym, i, cfg.struct_lookback), cfg)
        if not stop:
            out["skipped"].append({"symbol": sym, "reason": f"cannot size: {basis}"})
            continue
        s = size_position(ref, stop, r.get("adtv"), cfg)
        if not s["lots"]:
            out["skipped"].append({"symbol": sym, "reason": s["reason"] or "zero lots"})
            continue
        cand = {"symbol": sym, "ref_px": ref, "atr14": a, "atr_pct": a / ref,
                "stop_px": stop, "stop_basis": basis,
                "stop_pct": (ref - stop) / ref, "stop_atr": (ref - stop) / a,
                "lots": s["lots"], "risk_idr": s["risk_idr"], "risk_pct": s["risk_pct"],
                "notional_idr": s["notional_idr"], "binding": s["binding"],
                "granularity_drag": s["granularity_drag"], "flags": s["flags"],
                "risk_use": s.get("risk_use", 0.0),
                "scalable": s["lots"] >= 2, "sector": r.get("sector"),
                "beta": beta(p, sym, i), "adtv": r.get("adtv"),
                "rvol5": r.get("rvol"), "rvol1": rv1s.get(sym, {}).get(i),
                "clv": r.get("clv"), "trend": r.get("trend"), "cmf20": r.get("cmf20"),
                "lo_5d": r.get("lo_5d"), "entry_rule": "board+size",
                "r_idr": (ref - stop) * s["lots"] * SHARES_PER_LOT,
                # The validated convention is entry at the close of the session AFTER
                # the signal, so the reference is that close. The zone is a practical
                # guardrail, not a validated rule: paying more than +0.5 ATR over the
                # reference means the same stop is a materially worse trade, and the
                # intraday study found no way to time the fill better than this.
                "entry_lo": round_tick(ref - 0.25 * a, "down"),
                "entry_hi": round_tick(ref + 0.5 * a, "up")}
        cand["diagnostics"] = diagnostics(r, cand["rvol1"])
        sized.append(cand)

    # Admit GREEDILY, in board order, accumulating as we go. Testing each candidate
    # against the book as it stands today would clear all of them independently and
    # propose six positions at 1.5% risk each — 9% heat against a 4.5% cap. The board's
    # own ranking decides priority; it is the part that was validated.
    running = list(positions)
    for cand in sized:
        ok, why = admit(cand | {"entry_px": cand["ref_px"]}, running, cfg)
        cand["admitted"], cand["admit_reason"] = ok, why
        if ok:
            out["candidates"].append(cand)
            running.append(cand | {"entry_px": cand["ref_px"]})
        else:
            out["skipped"].append({"symbol": cand["symbol"], "reason": f"portfolio: {why}"})
    out["heat_after"] = portfolio_heat(running, equity)
    out["beta_gross_after"] = market_exposure(running, equity)
    return out


def summary_text(plan: dict, cfg: RiskConfig, warn: list[str]) -> str:
    """Plain text for notify_telegram.py — sent as-is, no markdown, no emoji."""
    L = [f"IDX TRADE - plan {plan['session']}"]
    L.append(f"equity {rupiah(plan['equity_idr'])} | heat {plan['heat']:.2%} of "
             f"{cfg.heat_cap_pct:.1%} | {len(plan['open'])}/{cfg.max_open} slots "
             f"| beta-gross {plan['beta_gross']:.2f}")
    L.append("-" * 30)

    L.append("OPEN")
    if not plan["open"]:
        L.append("  (flat)")
    for o in plan["open"]:
        r = f"{o['R']:+.2f}R" if o["R"] is not None else "n/a"
        L.append(f"  {o['symbol']} {o['lots']} lots @{o['entry_px']:,.0f} "
                 f"stop {o['stop_px']:,.0f} | mark {o['mark']:,.0f} {r}")
        if o["e2_armed"]:
            L.append(f"    [!!] below the 5-session low ({o['lo_5d']:,.0f}) - "
                     f"E2 exit armed, sell at tomorrow's open")

    L.append("-" * 30)
    L.append("CANDIDATES (levels, not orders)")
    if not plan["candidates"]:
        L.append("  none admitted")
    for c in plan["candidates"]:
        L.append(f"  {c['symbol']}   ATR {c['atr14']:,.0f} ({c['atr_pct']:.1%}/day)")
        L.append(f"    ENTRY  {c['entry_lo']:,.0f} - {c['entry_hi']:,.0f}   "
                 f"(ref close {c['ref_px']:,.0f})")
        L.append(f"    STOP   {c['stop_px']:,.0f}  = -{c['stop_pct']:.1%} from ref, "
                 f"{c['stop_atr']:.1f} ATR")
        L.append(f"    SIZE   {c['lots']} lots = {rupiah(c['notional_idr'])} | "
                 f"risk if stopped {rupiah(c['risk_idr'])} ({c['risk_pct']:.2%} of book)")
        if c["binding"] == "liquidity":
            L.append(f"    thinner name - size held to {rupiah(c['notional_idr'])} by the "
                     f"5%-of-ADTV ceiling")
        if c["flags"]:
            L.append(f"    [!!] {' '.join(c['flags'])}")
        if c["diagnostics"]:
            L.append(f"    note (not a filter): {', '.join(c['diagnostics'])}")
        L.append(f"    if you fill above {c['entry_hi']:,.0f}, move the stop with you: "
                 f"stop = fill - {1.5 * c['atr14']:,.0f}")

    if plan["skipped"]:
        L.append("-" * 30)
        L.append("SKIPPED")
        for s in plan["skipped"]:
            L.append(f"  {s['symbol']} - {s['reason']}")

    L.append("-" * 30)
    L.append(f"if all taken: heat {plan.get('heat_after', 0):.2%} of {cfg.heat_cap_pct:.1%} "
             f"| beta-gross {plan.get('beta_gross_after', 0):.2f} of {cfg.beta_gross_cap:.2f}")
    L.append("Stop = 1.5x ATR, never the prior-day low. Exit on a CLOSE below the")
    L.append("5-session low. VALIDATED n=915 2y 3/4 folds. Diagnostics gate nothing:")
    L.append("tested as filters, they were worse than random.")
    for w in warn:
        L.append(f"[warn] {w}")
    L.append(SITE)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", type=str, help="session date (default: board's)")
    ap.add_argument("--summary", action="store_true", help="print the Telegram text")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write JSON")
    ap.add_argument("--notify", action="store_true", help="push to Telegram")
    a = ap.parse_args()

    cfg, warn = config_from_env()
    p = Panel()
    p.load_prices()
    p.load_benchmark()
    plan = build(p, a.date or (p.dates[-1] if p.dates else ""), cfg)

    if not a.dry_run:
        OUT.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")

    text = summary_text(plan, cfg, warn)
    if a.summary or a.dry_run or a.notify:
        print(text)
    if not a.dry_run:
        print(f"\nwrote {OUT}")
    if a.notify:
        import subprocess
        n = Path(__file__).resolve().parent / "notify_telegram.py"
        r = subprocess.run([sys.executable, str(n), "--title",
                            f"IDX TRADE - plan {plan['session']}", "--text", text],
                           capture_output=True, text=True, timeout=60)
        print(r.stdout.strip() or r.stderr.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
