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
import contextlib
import hashlib
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bot_state  # noqa: E402
import live_prices  # noqa: E402
import position_book as pb  # noqa: E402
from alpha_lib import Panel  # noqa: E402
from trade_lib import (SHARES_PER_LOT, admit, atr_series, config_from_env,  # noqa: E402
                       low_n_prior, market_exposure, portfolio_heat, round_tick,
                       size_position, split_lots, stop_price)

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "data" / "panel" / "momentum_board.json"
TRADE_LOCK = Path("/tmp/idx-trade.lock")


def _load_sectors() -> dict:
    import csv as _csv
    f = ROOT / "reference" / "tickers.csv"
    if not f.exists():
        return {}
    with f.open(encoding="utf-8") as fh:
        return {(r.get("ticker") or "").strip().upper(): (r.get("sector") or "").strip() or None
                for r in _csv.DictReader(fh) if r.get("ticker")}


SECTORS = _load_sectors()

HELP = (
    "IDX TRADE BOT\n\n"
    "READ\n"
    "/book            positions, live marks, exit levels\n"
    "/plan TICKER     stop, size and target for any ticker\n"
    "/band TICKER     where it sits on the RVOL band, with a chart\n"
    "TICKER           a bare 4-letter ticker does the same\n"
    "/report          re-send this morning's report\n"
    "/status          last screener run\n"
    "/run             run the screener now\n"
    "/help            this message\n\n"
    "The board lands at 07:00 WIB, the trade plan at 07:15, the pre-close\n"
    "exit check at 15:40, weekdays.\n\n"
    "WRITE (each one asks you to confirm first)\n"
    "/open TICKER LOTS PRICE [stop=PX] [date=...]\n"
    "/scale TICKER LOTS PRICE      partial exit\n"
    "/close TICKER PRICE           full exit\n"
    "/stop TICKER PRICE            move a stop up\n"
    "/yes  .  /no                  confirm or discard\n\n"
    "/export                       the book as an Excel workbook\n"
    "Nothing is recorded until you reply /yes. Tickets expire in 10 minutes."
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
    # Has E2 ALREADY fired? The floor printed above is the one for the NEXT session
    # (it includes today's low), which is the right forward-looking number and exactly
    # the wrong one for answering "did the rule already trigger". Both matter, so both
    # are shown: ISAT on 2026-08-26 closed at 2,480 against a prior 5-session low of
    # 2,530 — E2 had fired, while the forward floor read 2,480 and looked untouched.
    last_close = (p.raw_close.get(sym, {}) or {}).get(i)
    prev_floor = low_n_prior(p, sym, i - 1, cfg.struct_lookback)
    if last_close is not None and prev_floor and last_close < prev_floor:
        L.append(f"  [!!] E2 FIRED on {p.dates[i]}: close {last_close:,.0f} below the "
                 f"5-session low {prev_floor:,.0f} — the rule says exit")

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


# ---------------------------------------------------------------------- write commands
#
# Every command here builds a TICKET and writes nothing. The ledger is touched in exactly
# one place, commit(), and only after /yes. That separation is the whole safety model: a
# fat-fingered lot count typed on a phone is a proposal until you have read it back.


class LockBusy(Exception):
    pass


@contextlib.contextmanager
def trade_lock(timeout_s: int = 20):
    """The same /tmp/idx-trade.lock that idx-trade-plan and idx-trade-preclose take.

    A 20-second ceiling, not the 300/600s those units use: the listener must stay
    responsive, and it must never be the reason the pre-close job misses its window. If
    the lock is held we say so and let the user retry — a queue of pending financial
    writes is worse than a refusal.

    idx-bot.service already sets PrivateTmp=false (the unit says why); a private /tmp
    would make the lock invisible and this guard silently useless.
    """
    try:
        import fcntl
    except ImportError:                      # Windows: local testing only, prod is Linux
        yield None
        return
    TRADE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(TRADE_LOCK, "a+")
    deadline = time.time() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise LockBusy(
                        "the trade layer is busy right now (the plan or pre-close job is "
                        "writing). Try again in a minute.")
                time.sleep(1)
        yield fh
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:                                      # noqa: BLE001
            pass
        fh.close()


def _num(tok: str) -> float:
    """Accept 2600, 2,600 and 2.6k — a phone keyboard invites all three."""
    t = tok.strip().replace(",", "").lower()
    mult = 1
    if t.endswith("k"):
        mult, t = 1_000, t[:-1]
    elif t.endswith("m"):
        mult, t = 1_000_000, t[:-1]
    return float(t) * mult


def _kwargs(args: list) -> tuple:
    """Split trailing key=value pairs off the positional arguments."""
    pos, kw = [], {}
    for a in args:
        if "=" in a and not a[0].isdigit():
            k, v = a.split("=", 1)
            kw[k.strip().lower()] = v.strip()
        else:
            pos.append(a)
    return pos, kw


def _event_hash(ev: dict) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(ev, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _sanity(sym: str, lots: int, px: float, stop: float, cfg, st: dict) -> list:
    """Warnings that make a mis-typed number unmissable rather than merely present.

    The lot count is where a phone goes wrong, and a wrong lot count does not look wrong
    in lots — it looks wrong as a MULTIPLE OF YOUR RISK BUDGET. 500 lots reads 1.00x;
    5,000 reads 10.0x, which no one confirms by accident.
    """
    w = []
    notional = px * lots * SHARES_PER_LOT
    eq = st.get("equity_idr") or cfg.equity_idr
    if notional > eq:
        w.append(f"notional {rupiah(notional)} EXCEEDS your whole book {rupiah(eq)}")
    elif notional > 0.40 * eq:
        w.append(f"notional is {notional / eq:.0%} of the book "
                 f"(cap is {cfg.max_pos_pct:.0%})")
    if stop:
        risk = (px - stop) * lots * SHARES_PER_LOT
        mult = risk / cfg.risk_budget_idr if cfg.risk_budget_idr else 0
        if mult > 1.5 or mult < 0.5:
            w.append(f"risk {rupiah(risk)} is {mult:.2f}x your risk budget "
                     f"{rupiah(cfg.risk_budget_idr)}")
    q = live_prices.quote(sym)
    if q and q.get("px"):
        gap = px / q["px"] - 1
        if abs(gap) > 0.15:
            w.append(f"price {px:,.0f} is {gap:+.0%} away from the last mark "
                     f"{q['px']:,.0f} ({live_prices.age_str(q)})")
    return w


def _ticket(title: str, body: list, warnings: list, action: str) -> str:
    L = [title, "-" * 30] + body
    if warnings:
        L += ["", "CHECK THIS:"] + [f"  [!] {w}" for w in warnings]
    L += ["", action]
    return "\n".join(L)


def _stash(command: str, raw: str, ev: dict, text: str, warnings: list,
           ctx: dict, extra: dict | None = None) -> str:
    bot_state.save_pending(bot_state.new_ticket(
        command=command, raw_text=raw, event=ev, ticket_text=text,
        chat_id=(ctx or {}).get("chat_id", ""), update_id=(ctx or {}).get("update_id", 0),
        message_id=(ctx or {}).get("message_id", 0),
        book_fingerprint=pb.ledger_fingerprint(), warnings=warnings, extra=extra))
    return text


def cmd_open(args: list, ctx: dict) -> str:
    pos_args, kw = _kwargs(args)
    if len(pos_args) < 3:
        return ("Usage:  /open TICKER LOTS PRICE [stop=PX] [date=YYYY-MM-DD]\n"
                "e.g.    /open DMAS 10000 167")
    sym = pos_args[0].upper()
    ok, company = pb.validate_symbol(sym)
    if not ok:
        return company
    try:
        lots, px = int(_num(pos_args[1])), _num(pos_args[2])
    except ValueError:
        return f"Could not read lots/price from {pos_args[1]!r} {pos_args[2]!r}"
    if lots <= 0 or px <= 0:
        return "Lots and price must both be positive."

    cfg, _ = config_from_env()
    st = pb.rebuild()
    cfg.equity_idr = st["equity_idr"] or cfg.equity_idr
    if sym in st["positions"]:
        return (f"{sym} is already open ({st['positions'][sym]['lots']} lots @"
                f"{st['positions'][sym]['entry_px']:,.0f}).\n"
                f"The book does not average in. Use /scale to sell part, or record the "
                f"exit first.")

    p = panel()
    i = len(p.dates) - 1
    a = atr_series(p, sym).get(i)
    basis = "manual"
    if "stop" in kw:
        stop = _num(kw["stop"])
    elif a:
        # Stop from the FILL, not from the board's reference — the house rule the plan
        # already prints as "if you fill above entry_hi, move the stop with you".
        stop, basis = stop_price(px, a, low_n_prior(p, sym, i, cfg.struct_lookback), cfg)
        if not stop:
            return (f"{sym}: cannot compute a stop from a fill of {px:,.0f} — {basis}.\n"
                    f"Pass one explicitly:  /open {sym} {lots} {px:,.0f} stop=<PX>")
    else:
        return f"{sym}: no ATR in the panel, so pass a stop:  stop=<PX>"

    stop = round_tick(stop, "down")
    if stop >= px:
        return f"Stop {stop:,.0f} is at or above the fill {px:,.0f}."

    ev = pb.open_event(sym, lots, px, stop, basis=basis, atr14=a,
                       sector=SECTORS.get(sym), rule=_entry_rule(sym),
                       date=kw.get("date"), cfg=cfg)
    notional = px * lots * SHARES_PER_LOT
    risk = ev["r_idr"]
    sold, keep = split_lots(lots, cfg.scale_fraction)
    body = [
        f"{sym}  {company}",
        f"BUY   {lots:,} lots @ {px:,.0f}   ({kw.get('date') or 'today'})",
        f"      = {lots * SHARES_PER_LOT:,} shares = {rupiah(notional)} "
        f"= {notional / cfg.equity_idr:.1%} of book",
        f"STOP  {stop:,.0f}  ({(px - stop) / px:-.1%}, {basis})",
        f"RISK  {rupiah(risk)} = {risk / cfg.equity_idr:.2%} of book "
        f"= {risk / cfg.risk_budget_idr:.2f}x your risk budget",
    ]
    if sold:
        body.append(f"SCALE sell {sold:,} at "
                    f"{round_tick(px + cfg.scale_level_r * (px - stop), 'up'):,.0f} "
                    f"(+{cfg.scale_level_r:g}R), keep {keep:,}")
    if ev["entry_rule"] == "discretionary":
        body.append("ENTRY discretionary — not on the board for this session")

    txt = _ticket(f"CONFIRM OPEN {sym}", body,
                  _sanity(sym, lots, px, stop, cfg, st),
                  "Reply /yes to record it, /no to discard. Expires in 10 minutes.")
    return _stash("open", " ".join(["/open"] + args), ev, txt,
                  _sanity(sym, lots, px, stop, cfg, st), ctx)


def _entry_rule(sym: str) -> str:
    """board+size, or discretionary — and the difference becomes queryable later.

    A warning read once is forgotten; a field on the event lets you partition realised R
    by entry type at review time and find out whether discretionary picks actually pay.
    """
    _sess, syms = board_state()
    return "board+size" if sym in syms else "discretionary"


def _exit_ticket(kind: str, args: list, ctx: dict) -> str:
    pos_args, kw = _kwargs(args)
    need = 3 if kind == "scale" else 2
    if len(pos_args) < need:
        return (f"Usage:  /{kind} TICKER "
                f"{'LOTS ' if kind == 'scale' else ''}PRICE [date=YYYY-MM-DD]")
    sym = pos_args[0].upper()
    st = pb.rebuild()
    pos = st["positions"].get(sym)
    if not pos:
        held = ", ".join(sorted(st["positions"])) or "nothing"
        return f"{sym} is not open. You hold: {held}."
    try:
        if kind == "scale":
            lots, px = int(_num(pos_args[1])), _num(pos_args[2])
        else:
            lots, px = pos["lots"], _num(pos_args[1])
    except ValueError:
        return "Could not read the numbers."
    if lots <= 0 or lots > pos["lots"]:
        return f"{sym} has {pos['lots']:,} lots open; {lots:,} requested."
    if kind == "scale" and lots == pos["lots"]:
        return f"That is the whole position — use /close {sym} {px:,.0f}."

    cfg, _ = config_from_env()
    cfg.equity_idr = st["equity_idr"] or cfg.equity_idr
    ev = pb.exit_event(kind, sym, lots, px, pos=pos, reason=kw.get("reason", ""),
                       date=kw.get("date"), cfg=cfg)
    leg_r = pos["r_idr"] * lots / pos["lots_initial"]
    body = [
        f"{sym}  {lots:,} of {pos['lots']:,} lots @ {px:,.0f}   "
        f"({kw.get('date') or 'today'})",
        f"      entry {pos['entry_px']:,.0f}, stop {pos['stop_px']:,.0f}",
        f"P&L   {rupiah(ev['realised_idr'])}  "
        f"({ev['realised_idr'] / leg_r:+.2f}R on the leg)" if leg_r else
        f"P&L   {rupiah(ev['realised_idr'])}",
        f"LEAVES {pos['lots'] - lots:,} lots open" if kind == "scale" else "CLOSES the position",
    ]
    warn = []
    q = live_prices.quote(sym)
    if q and q.get("px") and abs(px / q["px"] - 1) > 0.15:
        warn.append(f"price {px:,.0f} is {px / q['px'] - 1:+.0%} from the last mark "
                    f"{q['px']:,.0f}")
    txt = _ticket(f"CONFIRM {kind.upper()} {sym}", body, warn,
                  "Reply /yes to record it, /no to discard. Expires in 10 minutes.")
    return _stash(kind, " ".join([f"/{kind}"] + args), ev, txt, warn, ctx)


def cmd_movestop(args: list, ctx: dict) -> str:
    pos_args, _kw = _kwargs(args)
    if len(pos_args) < 2:
        return "Usage:  /stop TICKER PRICE"
    sym = pos_args[0].upper()
    st = pb.rebuild()
    pos = st["positions"].get(sym)
    if not pos:
        return f"{sym} is not open."
    try:
        px = round_tick(_num(pos_args[1]), "down")
    except ValueError:
        return f"Could not read a price from {pos_args[1]!r}"
    if px < pos["stop_px"]:
        return (f"{px:,.0f} is BELOW the current stop {pos['stop_px']:,.0f}.\n"
                f"Stops are monotone by design — widening one mid-trade is how a 1R loss "
                f"becomes a 3R loss. Not available from the phone; use the CLI with "
                f"--allow-lower if you truly mean it.")
    ev = pb.stop_event(sym, px, pos["stop_px"], reason="moved from telegram")
    body = [f"{sym}  stop {pos['stop_px']:,.0f} -> {px:,.0f}",
            "      R is FROZEN at open and does not change with the stop."]
    txt = _ticket(f"CONFIRM STOP {sym}", body, [],
                  "Reply /yes to record it, /no to discard.")
    return _stash("stop", " ".join(["/stop"] + args), ev, txt, [], ctx)


def cmd_no() -> str:
    if not bot_state.load_pending():
        return "Nothing pending."
    bot_state.clear_pending()
    return "Discarded. Nothing was recorded."


def commit(args: list) -> str:
    """The only place in this file that writes. Every step removes a failure mode."""
    p_ = bot_state.load_pending()
    if not p_:
        return "Nothing to confirm. Send a command first."
    if bot_state.is_expired(p_):
        bot_state.clear_pending()
        return ("That ticket expired (10 minutes). Nothing was recorded — retype the "
                "command so the price is one you still remember.")
    ev_sym = (p_.get("event") or {}).get("symbol")
    if args and args[0].upper() != (ev_sym or "").upper():
        return f"The pending ticket is for {ev_sym}, not {args[0].upper()}. /no to discard."

    try:
        with trade_lock():
            # The book must be the one the ticket was built against. Anything appended
            # since — the pre-close job, a laptop CLI — invalidates every derived number
            # in the ticket: whether the position exists, how many lots remain, heat.
            if pb.ledger_fingerprint() != p_["book_fingerprint"]:
                bot_state.clear_pending()
                return ("The book changed since that ticket was built, so its numbers are "
                        "stale. Nothing was recorded. Send the command again.")

            seen = bot_state.load_seen()
            ev = {"ts": pb.now_wib(), **p_["event"]}
            h = _event_hash(p_["event"])
            dup = bot_state.recent_commit(seen, h)
            if dup:
                bot_state.clear_pending()
                return f"Already recorded at {dup['ts'][:16].replace('T', ' ')} — {dup['summary']}."

            # DRY RUN. An append-only file cannot be rolled back, so the invariants are
            # checked in memory in front of the write, never after it.
            problems = pb.verify_events(pb.read_events() + [ev])
            if problems:
                bot_state.clear_pending()
                return ("Refused — that would have broken the ledger:\n"
                        + "\n".join(f"  {x}" for x in problems[:5])
                        + "\nNothing was recorded.")

            pb.append_event(ev)
            bot_state.clear_pending()
            state = pb.rebuild()
            pb.write_cache(state)
            summary = f"{ev.get('symbol')} {ev.get('type')}"
            bot_state.record_commit(seen, h, summary)
            bot_state.mark_confirmed(f"committed {summary}")
            bot_state.save_seen(seen)
    except LockBusy as e:
        return str(e)

    # Reply with a READ of the ledger after the write, not an echo of the ticket, so a
    # partial failure is visible rather than papered over by an optimistic confirmation.
    return "RECORDED.\n\n" + cmd_book()


def cmd_export(args: list, ctx: dict) -> str:
    """Build the workbook and hand it back as a Telegram document.

    Detached, with an immediate acknowledgement — the pattern trigger_run() uses. The
    BoardFires reconstruction loads the whole panel and takes tens of seconds, and a poll
    loop blocked on that stops answering everything else.
    """
    import subprocess
    script = Path(__file__).resolve().parent / "_export_and_send.py"
    subprocess.Popen([sys.executable, str(script)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return ("Building the workbook - Summary, Open, Trades, Equity, Ledger and "
            "BoardFires. I will send it here in a moment (~30s).")


def dispatch(cmd: str, args: list, ctx: dict | None = None) -> str | None:
    """Return reply text, or None if this file does not own the command."""
    if cmd == "book":
        return cmd_book()
    if cmd == "plan":
        if not args:
            return "Which ticker? e.g. /plan DMAS"
        return cmd_plan(args[0], float(args[1]) if len(args) > 1 else None)
    ctx = ctx or {}
    if cmd == "open":
        return cmd_open(args, ctx)
    if cmd in ("close", "scale"):
        return _exit_ticket(cmd, args, ctx)
    if cmd == "stop":
        return cmd_movestop(args, ctx)
    if cmd == "export":
        return cmd_export(args, ctx)
    if cmd == "yes":
        return commit(args)
    if cmd == "no":
        return cmd_no()
    if cmd == "undo":
        return ("There is no /undo. The ledger is append-only, so a mistake is corrected "
                "by recording its opposite, not by deleting it.\n"
                "Use /close or /scale, or the CLI on the laptop.")
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
