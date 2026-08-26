#!/usr/bin/env python3
"""Trade commands for the Telegram bot. READ-ONLY in this phase — nothing here writes.

`dispatch()` is the only entry point `bot_listener` knows about, so the transport and the
trade logic stay separable and this file can be exercised from a terminal without a bot
token.

Two commands, and the difference between them is the whole point:

  /book          what you hold, marked live, against the levels the rules imply
  /plan TICKER   what a position in TICKER would look like — arithmetic, not a signal

Plain text throughout, no markdown. Telegram's MarkdownV2 rejects unescaped `.`, `-` and
`(` with an HTTP 400, and every line here is full of all three — see
`notify_telegram.py:130-131`, which learned this already.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_prices  # noqa: E402
import position_book as pb  # noqa: E402
from alpha_lib import Panel  # noqa: E402
from trade_lib import (SHARES_PER_LOT, admit, atr_series, config_from_env,  # noqa: E402
                       low_n_prior, market_exposure, portfolio_heat, round_tick,
                       size_position, split_lots, stop_price)

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "data" / "panel" / "momentum_board.json"

HELP = (
    "IDX TRADE BOT\n\n"
    "READ\n"
    "/book            positions, live marks, exit levels\n"
    "/plan TICKER     stop, size and target for any ticker\n"
    "/status          last screener run\n"
    "/run             run the screener now\n"
    "/help            this message\n\n"
    "The board lands at 07:00 WIB, the trade plan at 07:15, the pre-close\n"
    "exit check at 15:40, weekdays.\n\n"
    "Recording trades from here is not enabled yet."
)

_PANEL = None


def panel() -> Panel:
    """Prices only — 0.6s against 2.3s for a full load, and flows are irrelevant here.

    Cached for the process. The listener is long-lived, so a board refresh mid-day will
    not be seen until it restarts; that is acceptable because everything this file reads
    from the panel (ATR, the 5-session low) is a function of COMPLETED sessions and only
    changes overnight.
    """
    global _PANEL
    if _PANEL is None:
        p = Panel()
        p.load_prices()
        _PANEL = p
    return _PANEL


def panel_staleness(p: Panel) -> list[str]:
    """The panel is a cache, and a stale cache silently answers with old prices.

    Every number /plan produces — ATR, the stop width, the lot count, the 5-session low —
    is a function of the panel's LAST session, not of today. On 2026-08-27 this laptop's
    panel still ended at 2026-08-14, so DMAS priced at 149 with an ATR of 3, when it had
    since broken out to 167 with an ATR near 7. The stop would have been less than half
    what the name now needs, and the lot count more than double.

    The session date alone was printed and it was not enough: a date in a header reads as
    provenance, not as a warning. So say the consequence, in the same shape as the ledger
    banner, and put it above the numbers.
    """
    from datetime import datetime
    if not p.dates:
        return ["[!!] PANEL IS EMPTY — run backfill_panel.py", ""]
    last = p.dates[-1]
    try:
        then = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return []
    today = datetime.now(live_prices.WIB).date()
    wd, d = 0, then
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            wd += 1
    if wd <= 2:
        return []
    return [
        f"[!!] PANEL IS {wd} TRADING SESSIONS STALE (last session {last})",
        "     Every figure below — ATR, stop width, lot count, the 5-session low —",
        "     comes from that session, not from today. Run backfill_panel.py.",
        "",
    ]


def rupiah(x) -> str:
    if x is None:
        return "—"
    a = abs(x)
    if a >= 1e9:
        return f"Rp{x / 1e9:.2f}bn"
    if a >= 1e6:
        return f"Rp{x / 1e6:.1f}m"
    return f"Rp{x:,.0f}"


def staleness_banner(events: list) -> list[str]:
    """Printed ABOVE the positions, always.

    A warning under the numbers is read after the numbers have been believed. And it
    states the CONSEQUENCE, not just the fact: a stale book does not merely lack recent
    trades, it makes every R, every heat figure and every stop level below it wrong.
    """
    act = pb.last_activity(events)
    wd = act.get("weekdays")
    if wd is None or wd <= 2:
        return []
    when = (act.get("ts") or "")[:16].replace("T", " ")
    conf = None
    try:
        import bot_state
        c = bot_state.last_confirmed()
        conf = (c or {}).get("ts", "")[:10] or None
    except Exception:                                          # noqa: BLE001
        pass
    return [
        f"[!!] LEDGER IS {wd} TRADING SESSIONS STALE",
        f"     last fill {when} ({act.get('type')} {act.get('symbol') or ''})".rstrip(),
        "     If you have traded since, this book is wrong — and every R, heat",
        "     figure and stop level below is wrong with it.",
        f"     last confirmed by you: {conf or 'never'}",
        "",
    ]


def position_lines(pos: dict, p: Panel, q: dict | None, cfg) -> list[str]:
    sym = pos["symbol"]
    i = len(p.dates) - 1
    entry, stop, lots = pos["entry_px"], pos["stop_px"], pos["lots"]
    r_ps = entry - stop
    mark = q["px"] if q else (p.raw_close.get(sym, {}) or {}).get(i)
    L = [f"{sym}  {lots} lots @{entry:,.0f}  stop {stop:,.0f}"]
    if mark is None:
        L.append("  no mark available — not in the panel and no quote")
        return L

    R = (mark - entry) / r_ps if r_ps else 0.0
    pnl = (mark - entry) * lots * SHARES_PER_LOT
    flag = "  [STALE]" if live_prices.is_stale(q) else ""
    L.append(f"  mark {mark:,.0f} {live_prices.age_str(q)}{flag}"
             f"   {R:+.2f}R  {rupiah(pnl)}")

    a = atr_series(p, sym).get(i)
    lo5 = low_n_prior(p, sym, i, cfg.struct_lookback)
    bits = [f"stop {stop:,.0f} ({(mark - stop) / mark:+.1%})"]
    if lo5:
        gap = (mark - lo5) / mark
        atr_gap = f", {(mark - lo5) / a:.1f} ATR" if a else ""
        bits.append(f"E2 floor {lo5:,.0f} ({gap:+.1%}{atr_gap})")
    L.append("  " + " | ".join(bits))

    if r_ps:
        tgt = round_tick(entry + cfg.scale_level_r * r_ps, "up")
        sold, keep = split_lots(lots, cfg.scale_fraction)
        if sold:
            hit = " <- REACHED" if mark >= tgt else ""
            L.append(f"  scale: sell {sold} of {lots} at {tgt:,.0f} "
                     f"(+{cfg.scale_level_r:g}R), keep {keep}{hit}")

    # The rule's own verdict, stated plainly. E2 is a CLOSE rule, so an intraday mark
    # under the floor is a warning and not a trigger — that distinction is the whole
    # GGRM lesson and it must survive into the phone view.
    if mark <= stop:
        L.append("  [!!] STOP BREACHED")
    elif lo5 and mark < lo5:
        L.append("  [!] below the 5-session low intraday — E2 fires only on a CLOSE")
    return L


def cmd_book() -> str:
    cfg, _ = config_from_env()
    events = pb.read_events()
    st = pb.rebuild(events)
    positions = list(st["positions"].values())
    p = panel()
    qs = live_prices.quotes([x["symbol"] for x in positions]) if positions else {}

    from datetime import datetime
    L = [f"POSITION BOOK - {datetime.now(live_prices.WIB):%Y-%m-%d %H:%M}"]
    heat = portfolio_heat(positions, st["equity_idr"])
    L.append(f"equity {rupiah(st['equity_idr'])} | {len(positions)} open | "
             f"realised {rupiah(st['realised_idr'])}")
    L.append(f"heat {heat:.2%} of {cfg.heat_cap_pct:.1%} | "
             f"{len(positions)}/{cfg.max_open} slots | "
             f"beta-gross {market_exposure(positions, st['equity_idr']):.2f}")
    L.append("-" * 30)
    L += panel_staleness(p)
    L += staleness_banner(events)

    if not positions:
        L.append("(flat)")
    for pos in sorted(positions, key=lambda x: x["symbol"]):
        L += position_lines(pos, p, qs.get(pos["symbol"]), cfg)
        L.append("")
    if qs and live_prices.market_looks_closed(qs):
        L.append("every mark is over 2h old — the market appears closed")
    return "\n".join(L).rstrip()


def board_state() -> tuple:
    """(session, symbols) from the last board BUILD — not from the panel.

    These are different dates and conflating them inverts the answer. momentum_board.json
    is overwritten by whichever run last built a board; the panel is refreshed separately.
    On 2026-08-27 this laptop held a board for session 2026-08-14 (BYAN, UNTD, ISAT, EXCL)
    against a panel ending 2026-08-26, and reporting the PANEL's date credited ISAT and
    EXCL to a session they were never scored in — while WIFI and DMAS, which are on the
    current board, were labelled "arithmetic only".

    So the session travels with the membership, and a caller that finds them out of step
    is told rather than left to assume.
    """
    import json
    if not BOARD.exists():
        return None, set()
    try:
        d = json.loads(BOARD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, set()
    return d.get("session"), {r.get("symbol") for r in d.get("candidates", [])}


def cmd_plan(sym: str, px: float | None = None) -> str:
    sym = (sym or "").strip().upper()
    ok, msg = pb.validate_symbol(sym)
    if not ok:
        return f"{msg}"
    company = msg

    p = panel()
    if sym not in p.raw_close:
        return (f"{sym} ({company}) is not in the panel, so there is no ATR to size it "
                f"from.\nThe panel covers the {len(p.raw_close)} names the board tracks.")
    i = len(p.dates) - 1
    ref = p.raw_close[sym].get(i)
    a = atr_series(p, sym).get(i)
    if not ref or not a:
        return f"{sym}: no usable price or ATR on {p.dates[i]}."

    b_session, b_syms = board_state()
    on_board = sym in b_syms
    board_stale = bool(b_session and p.dates and b_session != p.dates[i])
    cfg, _ = config_from_env()
    st = pb.rebuild()
    cfg.equity_idr = st["equity_idr"] or cfg.equity_idr

    L = panel_staleness(p)
    if board_stale:
        L += [f"[!] the last board built is for {b_session}, but the panel now ends "
              f"{p.dates[i]}.",
              "    Board membership below is from that older session — rebuild with",
              "    build_momentum_board.py for today's.", ""]
    if on_board:
        L.append(f"IDX TRADE - plan {sym}")
        L.append(f"{company} — ON the momentum board for {b_session}")
    else:
        # The disclaimer goes BEFORE any number. A well-formatted stop-and-size block
        # reads like a signal no matter what the footnote says, so the header, the first
        # line and the recorded entry_rule all have to disagree with that reading.
        L.append(f"IDX TRADE - ARITHMETIC ONLY - {sym}")
        L.append(f"{company}")
        L.append("")
        L.append(f"This is NOT a signal. It is not on the board for {b_session or 'any session'}.")
        L.append("What was validated is the ENTRY SIGNAL (n=1088, 2y, 3/4 folds).")
        L.append("The stop and lot count below are arithmetic that works on any")
        L.append("ticker you type, including one picked at random. Sizing does not")
        L.append("test whether a trade is worth taking — only how much you lose if")
        L.append("it is not.")
    L.append("-" * 30)

    stop, basis = stop_price(ref, a, low_n_prior(p, sym, i, cfg.struct_lookback), cfg)
    L.append(f"close {ref:,.0f} on {p.dates[i]} | ATR14 {a:,.0f} ({a / ref:.1%}/day)")
    if not stop:
        L.append(f"CANNOT SIZE: {basis}")
        if "too_wide" in basis:
            L.append(f"A 1.5x ATR stop is wider than the {cfg.max_stop_pct:.0%} ceiling.")
            L.append("Volatility has outrun what this book will risk on one name.")
        return "\n".join(L)

    adtv = (p.adtv.get(sym) or {}).get(i)
    s = size_position(ref, stop, adtv, cfg)
    r_ps = ref - stop
    L.append(f"STOP  {stop:,.0f}  = {(ref - stop) / ref:-.1%} from close, "
             f"{(ref - stop) / a:.1f} ATR ({basis})")
    if not s["lots"]:
        L.append(f"CANNOT SIZE: {s.get('reason') or 'zero lots'}")
        return "\n".join(L)

    L.append(f"SIZE  {s['lots']} lots = {rupiah(s['notional_idr'])} "
             f"({s['notional_idr'] / cfg.equity_idr:.1%} of book)")
    L.append(f"      risk if stopped {rupiah(s['risk_idr'])} "
             f"({s['risk_idr'] / cfg.equity_idr:.2%}) | bound by {s['binding']}")
    sold, keep = split_lots(s["lots"], cfg.scale_fraction)
    if sold:
        L.append(f"SCALE sell {sold} at "
                 f"{round_tick(ref + cfg.scale_level_r * r_ps, 'up'):,.0f} "
                 f"(+{cfg.scale_level_r:g}R), keep {keep}")

    cand = {"symbol": sym, "entry_px": ref, "stop_px": stop, "lots": s["lots"],
            "sector": None, "beta": None}
    fits, why = admit(cand, list(st["positions"].values()), cfg)
    L.append(f"FITS  {'yes' if fits else 'NO — ' + why}")
    return "\n".join(L)


def dispatch(cmd: str, args: list, ctx: dict | None = None) -> str | None:
    """Return reply text, or None if this file does not own the command."""
    if cmd == "book":
        return cmd_book()
    if cmd == "plan":
        if not args:
            return "Which ticker? e.g. /plan DMAS"
        return cmd_plan(args[0], float(args[1]) if len(args) > 1 else None)
    if cmd in ("open", "close", "scale", "stop", "yes", "no", "undo", "reconcile"):
        return ("Recording trades from Telegram is not enabled yet — this phase is "
                "read-only on purpose.\nUse /book and /plan, and record fills with the "
                "CLI for now.")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", help="book | plan")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    out = dispatch(a.command.lstrip("/").lower(), a.args)
    print(out if out is not None else f"unknown command {a.command!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
