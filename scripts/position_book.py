#!/usr/bin/env python3
"""The position book: an append-only ledger of what you actually did.

Nothing else in this repo knows what you own. Without that, a stop rule is a
suggestion and an R multiple is a guess.

Three design choices, each load-bearing:

1. **Append-only JSONL, never a mutable positions file.** Overwriting `current_stop`
   destroys the ability to prove what the stop was when the trade was *risked*, and
   once that is gone every past loss shrinks retroactively as the stop trails up.
   A single-line append also cannot corrupt prior records if a systemd job dies
   mid-write, which matters when the writer is an unattended timer on a VPS.
   `positions.json` is a derived cache and is safe to delete at any time.

2. **`r_idr` is frozen at open.** `r_idr = (entry_px - stop_px) * lots * 100`, written
   once in the `open` event and never recomputed. Every later R multiple divides by
   that number. Recomputing R from the *current* stop is the single most common way a
   trade journal lies to its owner.

3. **`--from-plan` rather than retyping.** The stop that gets recorded is the stop the
   plan computed, not one remembered at the end of the day. A hand-typed stop that
   differs from the planned one silently rewrites the trade's risk and makes the
   backtest replay diverge from reality with no error anywhere.

The file stays hand-repairable on purpose — it is JSONL, you can open it in an editor
during an emergency — and `verify` exists precisely to catch a bad hand-edit.

Everything here is advisory bookkeeping. Nothing places an order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trade_lib import (SHARES_PER_LOT, RiskConfig, config_from_env,  # noqa: E402
                       r_multiple, tick_size)

WIB = timezone(timedelta(hours=7))
ROOT = Path(__file__).resolve().parents[1]
# IDX_BOOK_DIR exists so the self-test can exercise a full trade lifecycle against a
# scratch ledger. It is NOT a way to run two books: the real one is whichever machine
# owns it, and a second book means filling on your phone and then reconciling against
# history that never happened.
BOOK = Path(os.environ.get("IDX_BOOK_DIR") or (ROOT / "data" / "book"))
LEDGER = BOOK / "ledger.jsonl"
POSITIONS = BOOK / "positions.json"
PLAN = ROOT / "data" / "panel" / "trade_plan.json"

EVENT_TYPES = ("equity", "open", "stop", "scale", "close", "note")


def now_wib() -> str:
    return datetime.now(WIB).isoformat(timespec="seconds")


def today_wib() -> str:
    return datetime.now(WIB).strftime("%Y-%m-%d")


# ------------------------------------------------------------------------ ledger I/O

def read_events() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for n, line in enumerate(LEDGER.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"[!!] ledger line {n} is not valid JSON: {e}\n    {line[:120]}")
        ev["_line"] = n
        out.append(ev)
    return out


def append_event(ev: dict) -> None:
    BOOK.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- rebuild

def rebuild(events: list[dict] | None = None) -> dict:
    """Replay the ledger into current state. Pure — never writes."""
    events = read_events() if events is None else events
    equity = 0.0
    positions: dict[str, dict] = {}
    closed: list[dict] = []
    realised_idr = 0.0
    realised_r = 0.0

    for ev in events:
        t = ev.get("type")
        if t == "equity":
            equity = float(ev["equity_idr"])
        elif t == "open":
            sym = ev["symbol"]
            if sym in positions:
                # Adding to a live position would change R for the whole line. Refuse
                # rather than average: a second 'open' is almost always a mistake.
                raise SystemExit(f"[!!] line {ev.get('_line', 'new')}: {sym} already open. "
                                 f"Close it first, or use a distinct symbol tag.")
            positions[sym] = {
                "symbol": sym, "opened": ev.get("date"), "lots": int(ev["lots"]),
                "lots_initial": int(ev["lots"]), "entry_px": float(ev["entry_px"]),
                "stop_px": float(ev["stop_px"]), "stop_basis": ev.get("stop_basis", ""),
                "r_idr": float(ev["r_idr"]), "atr14": ev.get("atr14"),
                "sector": ev.get("sector"), "beta": ev.get("beta"),
                "entry_rule": ev.get("entry_rule", ""), "plan_id": ev.get("plan_id", ""),
                "fees_idr": float(ev.get("fee_idr") or 0.0),
                "realised_idr": 0.0, "stop_history": [(ev["ts"], float(ev["stop_px"]))],
            }
        elif t == "stop":
            sym = ev["symbol"]
            if sym not in positions:
                raise SystemExit(f"[!!] line {ev.get('_line', 'new')}: stop for {sym}, which is not open")
            positions[sym]["stop_px"] = float(ev["stop_px"])
            positions[sym]["stop_basis"] = ev.get("reason", "")
            positions[sym]["stop_history"].append((ev["ts"], float(ev["stop_px"])))
        elif t in ("scale", "close"):
            sym = ev["symbol"]
            if sym not in positions:
                raise SystemExit(f"[!!] line {ev.get('_line', 'new')}: {t} for {sym}, which is not open")
            pos = positions[sym]
            sold = abs(int(ev["lots"]))
            if sold > pos["lots"]:
                raise SystemExit(f"[!!] line {ev.get('_line', 'new')}: {t} {sold} lots of {sym} "
                                 f"but only {pos['lots']} are open")
            px = float(ev["px"])
            fee = float(ev.get("fee_idr") or 0.0)
            pnl = (px - pos["entry_px"]) * sold * SHARES_PER_LOT - fee
            pos["lots"] -= sold
            pos["realised_idr"] += pnl
            pos["fees_idr"] += fee
            realised_idr += pnl
            realised_r += pnl / pos["r_idr"] if pos["r_idr"] else 0.0
            if pos["lots"] == 0 or t == "close":
                if pos["lots"] != 0 and t == "close":
                    raise SystemExit(f"[!!] line {ev.get('_line', 'new')}: close of {sym} leaves "
                                     f"{pos['lots']} lots open — use 'scale' for partials")
                rec = dict(pos)
                rec.update(exit_px=px, exit_reason=ev.get("reason", ""),
                           closed=ev.get("date") or ev["ts"][:10], realised_r_total=pos["realised_idr"] / pos["r_idr"]
                           if pos["r_idr"] else 0.0)
                closed.append(rec)
                del positions[sym]
        elif t == "note":
            pass
        elif t is not None:
            raise SystemExit(f"[!!] line {ev.get('_line', 'new')}: unknown event type {t!r}")

    return {"equity_idr": equity, "positions": positions, "closed": closed,
            "realised_idr": realised_idr, "realised_r": realised_r,
            "n_events": len(events)}


def write_cache(state: dict) -> None:
    BOOK.mkdir(parents=True, exist_ok=True)
    POSITIONS.write_text(json.dumps(
        {"rebuilt": now_wib(), "equity_idr": state["equity_idr"],
         "positions": list(state["positions"].values()),
         "realised_idr": state["realised_idr"]},
        indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------- event builders
#
# The bot and the CLI must write BYTE-IDENTICAL events, so event construction lives here
# and both call it. These deliberately return the event WITHOUT `ts`: a Telegram ticket is
# built when you type the command and committed when you confirm it, and verify enforces
# monotone timestamps across the whole file — so a ticket built at 10:14 must not carry
# 10:14 if something else appended at 10:17. The caller stamps `ts` at the append.


def _cfg() -> RiskConfig:
    """Risk config from secrets/trade.env, environment winning.

    Was a bare `RiskConfig()` until 2026-08-27 — the dataclass defaults: equity Rp100m,
    fees 0.15% buy / 0.25% sell. The real config is Rp2bn at 0.10%/0.10%. So every event
    ever written recorded fees 50% too high on buys and 150% too high on sells, and an
    `intended_risk_idr` computed against a placeholder book: Rp1.02m of phantom fees
    across the live ledger, and ISAT realised understated by Rp824,500.
    """
    cfg, _warn = config_from_env()
    return cfg


def open_event(symbol: str, lots: int, px: float, stop: float, *, basis=None,
               atr14=None, sector=None, beta=None, rule=None, date=None,
               fee=None, cfg: "RiskConfig | None" = None) -> dict:
    cfg = cfg or _cfg()
    d = date or today_wib()
    fee = fee if fee is not None else px * lots * SHARES_PER_LOT * cfg.fee_buy
    return {"type": "open", "symbol": symbol, "date": d,
            "lots": lots, "entry_px": px, "fee_idr": round(fee, 2),
            "stop_px": stop, "stop_basis": basis or "manual", "atr14": atr14,
            "r_idr": (px - stop) * lots * SHARES_PER_LOT,
            "intended_risk_idr": cfg.risk_budget_idr,
            "sector": sector, "beta": beta, "entry_rule": rule or "",
            "plan_id": f"{d}:{symbol}"}


def stop_event(symbol: str, px: float, prev_stop: float, *, reason: str = "") -> dict:
    return {"type": "stop", "symbol": symbol, "stop_px": px,
            "prev_stop_px": prev_stop, "reason": reason or ""}


def exit_event(kind: str, symbol: str, lots: int, px: float, *, pos: dict,
               reason: str = "", fee=None, date=None,
               cfg: "RiskConfig | None" = None) -> dict:
    """A scale or a close. `pos` is the live position from rebuild()."""
    cfg = cfg or _cfg()
    fee = fee if fee is not None else px * lots * SHARES_PER_LOT * cfg.fee_sell
    pnl = (px - pos["entry_px"]) * lots * SHARES_PER_LOT - fee
    leg_r = pos["r_idr"] * lots / pos["lots_initial"]
    ev = {"type": kind, "symbol": symbol, "lots": -lots, "px": px,
          "fee_idr": round(fee, 2), "reason": reason or "",
          "realised_idr": round(pnl, 2),
          "realised_r": round(pnl / leg_r, 4) if leg_r else 0.0}
    # Present only when supplied, so a reconciled exit can carry its real trade date
    # without adding a field to every event ever written.
    if date:
        ev["date"] = date
    return ev


def validate_symbol(symbol: str) -> tuple:
    """(ok, message_or_company). The ledger has never validated symbols.

    A typo'd ticker from a phone becomes a permanent phantom position that rebuild()
    carries forever and heat counts against you. The alias dictionary is the natural
    authority — the board and the screener already trust it.
    """
    import csv as _csv
    import difflib
    if not re.fullmatch(r"[A-Z]{4}", symbol or ""):
        return False, f"{symbol!r} is not a 4-letter IDX ticker"
    f = ROOT / "reference" / "tickers.csv"
    if not f.exists():
        return True, ""            # cannot validate here; do not block
    known = {}
    with f.open(encoding="utf-8") as fh:
        for r in _csv.DictReader(fh):
            t = (r.get("ticker") or "").strip().upper()
            if t:
                known[t] = (r.get("company") or "").strip()
    if symbol in known:
        return True, known[symbol]
    near = difflib.get_close_matches(symbol, list(known), n=3, cutoff=0.6)
    hint = f" Did you mean {', '.join(near)}?" if near else ""
    return False, f"{symbol} is not in reference/tickers.csv.{hint}"


def last_activity(events=None) -> dict:
    """When the book was last actually TRADED, and how stale that makes it.

    Notes do not count. The 2026-08-15 ledger ended with a note recording an intention to
    buy ISAT and EXCL; counting that as activity would have reported a current book while
    four positions went unrecorded for eight sessions.
    """
    events = read_events() if events is None else events
    fills = [e for e in events if e.get("type") in ("open", "stop", "scale", "close")]
    if not fills:
        return {"ts": None, "type": None, "weekdays": None}
    last = fills[-1]
    ts = last.get("ts", "")
    try:
        then = datetime.fromisoformat(ts).date()
    except ValueError:
        return {"ts": ts, "type": last.get("type"), "weekdays": None}
    today = datetime.now(WIB).date()
    wd, d = 0, then
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            wd += 1
    return {"ts": ts, "type": last.get("type"), "symbol": last.get("symbol"),
            "weekdays": wd}


def ledger_fingerprint() -> str:
    """Identity of the ledger's exact bytes.

    A Telegram ticket is built against one book and confirmed against whatever the book is
    a minute later. If anything appended in between — the pre-close job, a laptop CLI —
    every derived number in the ticket is stale and the confirmation must be refused.
    """
    if not LEDGER.exists():
        return "sha256:empty"
    return "sha256:" + hashlib.sha256(LEDGER.read_bytes()).hexdigest()[:16]


# --------------------------------------------------------------------------- verify

def verify_events(events: list) -> list:
    """The invariant checks, pure and printing nothing.

    Split out of verify() so a proposed event can be checked BEFORE it is appended:
    verify_events(read_events() + [candidate]). An append-only file cannot be rolled back,
    so the only safe place to catch a bad write is in memory, in front of it.

    Tolerates a missing `_line` — only read_events() sets that, and a candidate event has
    never been read from anywhere.
    """
    problems: list[str] = []
    last_ts = ""
    for ev in events:
        ts = ev.get("ts", "")
        if ev.get("type") not in EVENT_TYPES:
            problems.append(f"line {ev.get('_line', 'new')}: bad type {ev.get('type')!r}")
        if ts < last_ts:
            problems.append(f"line {ev.get('_line', 'new')}: timestamp {ts} precedes previous {last_ts}")
        last_ts = max(last_ts, ts)
        if ev.get("type") == "open":
            need = ("symbol", "lots", "entry_px", "stop_px", "r_idr")
            for k in need:
                if ev.get(k) in (None, ""):
                    problems.append(f"line {ev.get('_line', 'new')}: open missing {k}")
            if all(ev.get(k) is not None for k in need):
                want = (float(ev["entry_px"]) - float(ev["stop_px"])) * int(ev["lots"]) * SHARES_PER_LOT
                if abs(want - float(ev["r_idr"])) > 1.0:
                    problems.append(
                        f"line {ev.get('_line', 'new')}: r_idr {float(ev['r_idr']):,.0f} does not match "
                        f"(entry-stop)*lots*100 = {want:,.0f} — the risk was silently rewritten")
                if float(ev["stop_px"]) >= float(ev["entry_px"]):
                    problems.append(f"line {ev.get('_line', 'new')}: stop at or above entry")
                # The STOP must be a valid tick — it is an order you place, and an
                # off-tick stop is rejected by the exchange. The ENTRY must not be
                # checked the same way: a large position filled across several prices
                # has a blended average that legitimately sits between ticks
                # (ERAA 2,000 lots at an average of 471, where the band's tick is 2).
                sp = float(ev["stop_px"])
                if sp % tick_size(sp):
                    problems.append(f"line {ev.get('_line', 'new')}: stop {sp:g} is not a valid tick "
                                    f"(band tick is {tick_size(sp)}) — the exchange will "
                                    f"reject that order")
                if int(ev["lots"]) <= 0:
                    problems.append(f"line {ev.get('_line', 'new')}: non-positive lots")

    try:
        rebuild(events)
    except SystemExit as e:
        problems.append(str(e).replace("[!!] ", ""))
    return problems


def verify() -> int:
    """Catch a bad hand-edit before a rule acts on it."""
    events = read_events()
    if not events:
        print("[warn] ledger is empty")
        return 0
    problems = verify_events(events)
    try:
        state = rebuild(events)
    except SystemExit:
        state = None

    print(f"ledger: {len(events)} events, {LEDGER}")
    if state:
        print(f"  equity Rp{state['equity_idr']:,.0f} | {len(state['positions'])} open "
              f"| {len(state['closed'])} closed | realised Rp{state['realised_idr']:,.0f}")
        if state["equity_idr"] <= 0:
            problems.append("no equity set — run: position_book.py equity --set <idr>")
    if problems:
        print(f"\n[!!] {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("[ok] ledger is internally consistent")
    return 0


# ------------------------------------------------------------------------------ show

def _fmt_money(x: float) -> str:
    return f"Rp{x:,.0f}"


def show(marks: dict | None = None, summary: bool = False) -> int:
    state = rebuild()
    write_cache(state)
    eq = state["equity_idr"]
    pos = list(state["positions"].values())
    print(f"POSITION BOOK — {today_wib()}")
    print(f"equity {_fmt_money(eq)} | {len(pos)} open | realised {_fmt_money(state['realised_idr'])}")
    if not pos:
        print("  (flat)")
    open_risk = 0.0
    for p in pos:
        mark = (marks or {}).get(p["symbol"], p["entry_px"])
        r = r_multiple(p["entry_px"], mark, p["r_idr"], p["lots"])
        risk = max(0.0, (p["entry_px"] - p["stop_px"]) * p["lots"] * SHARES_PER_LOT)
        open_risk += risk
        print(f"  {p['symbol']:<6} {p['lots']:>3} lots @{p['entry_px']:>8,.0f} "
              f"stop {p['stop_px']:>8,.0f} ({p['stop_basis']}) "
              f"R {_fmt_money(p['r_idr']):>14} | mark {mark:>8,.0f} {r:+.2f}R "
              f"| open risk {_fmt_money(risk)}")
    if eq > 0:
        print(f"  heat {open_risk / eq:.2%} of equity")
    if summary:
        for c in state["closed"][-10:]:
            print(f"  closed {c['symbol']:<6} {c['realised_r_total']:+.2f}R "
                  f"{_fmt_money(c['realised_idr'])}  {c.get('exit_reason','')}")
    return 0


# -------------------------------------------------------------------------- commands

def load_plan_row(symbol: str) -> dict:
    if not PLAN.exists():
        raise SystemExit(f"[!!] no plan at {PLAN} — drop --from-plan or run trade_plan.py")
    data = json.loads(PLAN.read_text(encoding="utf-8"))
    for row in data.get("candidates", []):
        if row.get("symbol") == symbol:
            return row
    raise SystemExit(f"[!!] {symbol} is not in today's plan ({PLAN.name}). "
                     f"Recording a stop the plan did not compute is exactly what "
                     f"--from-plan exists to prevent; pass --stop explicitly if you "
                     f"really mean to override it.")


def cmd_open(a) -> int:
    cfg = _cfg()
    stop, basis, atr14, sector, beta_, rule = a.stop, a.basis, None, a.sector, None, a.rule
    if a.from_plan:
        row = load_plan_row(a.symbol)
        stop = stop or row.get("stop_px")
        basis = basis or row.get("stop_basis")
        atr14, sector, beta_ = row.get("atr14"), sector or row.get("sector"), row.get("beta")
        rule = rule or row.get("entry_rule")
    if not stop:
        raise SystemExit("[!!] --stop is required (or use --from-plan)")
    if stop >= a.px:
        raise SystemExit(f"[!!] stop {stop:g} is at or above entry {a.px:g}")
    ev = open_event(a.symbol, a.lots, a.px, stop, basis=basis, atr14=atr14,
                    sector=sector, beta=beta_, rule=rule, date=a.date,
                    fee=a.fee, cfg=cfg)
    r_idr = ev["r_idr"]
    append_event({"ts": now_wib(), **ev})
    print(f"[ok] opened {a.symbol} {a.lots} lots @{a.px:,.0f}, stop {stop:,.0f} "
          f"({basis or 'manual'}), R = {_fmt_money(r_idr)}")
    return show()


def cmd_stop(a) -> int:
    state = rebuild()
    pos = state["positions"].get(a.symbol)
    if not pos:
        raise SystemExit(f"[!!] {a.symbol} is not open")
    if a.px < pos["stop_px"] and not a.allow_lower:
        raise SystemExit(f"[!!] {a.px:g} is BELOW the current stop {pos['stop_px']:g}. "
                         f"Stops are monotone by design — widening one mid-trade is how a "
                         f"1R loss becomes a 3R loss. Pass --allow-lower if you truly mean it.")
    append_event({"ts": now_wib(),
                  **stop_event(a.symbol, a.px, pos["stop_px"], reason=a.reason)})
    print(f"[ok] {a.symbol} stop {pos['stop_px']:,.0f} -> {a.px:,.0f}")
    return show()


def _exit_event(a, kind: str) -> int:
    cfg = _cfg()
    state = rebuild()
    pos = state["positions"].get(a.symbol)
    if not pos:
        raise SystemExit(f"[!!] {a.symbol} is not open")
    lots = a.lots if a.lots else pos["lots"]
    if kind == "close":
        lots = pos["lots"]
    if lots > pos["lots"]:
        raise SystemExit(f"[!!] {lots} lots requested but only {pos['lots']} open")
    ev = exit_event(kind, a.symbol, lots, a.px, pos=pos, reason=a.reason,
                    fee=a.fee, date=getattr(a, "date", None), cfg=cfg)
    pnl, leg_r = ev["realised_idr"], pos["r_idr"] * lots / pos["lots_initial"]
    append_event({"ts": now_wib(), **ev})
    leg = f" ({pnl / leg_r:+.2f}R on the leg)" if leg_r else ""
    print(f"[ok] {kind} {a.symbol} {lots} lots @{a.px:,.0f} -> {_fmt_money(pnl)}{leg}")
    return show()


def cmd_equity(a) -> int:
    append_event({"ts": now_wib(), "type": "equity", "equity_idr": a.set,
                  "note": a.note or ""})
    print(f"[ok] equity set to {_fmt_money(a.set)}")
    return show()


def cmd_note(a) -> int:
    append_event({"ts": now_wib(), "type": "note", "text": a.text})
    print("[ok] note recorded")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="record a fill")
    o.add_argument("symbol", type=str.upper)
    o.add_argument("--lots", type=int, required=True)
    o.add_argument("--px", type=float, required=True)
    o.add_argument("--stop", type=float)
    o.add_argument("--basis", type=str)
    o.add_argument("--from-plan", action="store_true",
                   help="take stop/atr/sector from today's trade_plan.json")
    o.add_argument("--sector", type=str)
    o.add_argument("--rule", type=str)
    o.add_argument("--fee", type=float)
    o.add_argument("--date", type=str)
    o.set_defaults(fn=cmd_open)

    s = sub.add_parser("stop", help="move a stop (monotone unless overridden)")
    s.add_argument("symbol", type=str.upper)
    s.add_argument("--px", type=float, required=True)
    s.add_argument("--reason", type=str)
    s.add_argument("--allow-lower", action="store_true")
    s.set_defaults(fn=cmd_stop)

    sc = sub.add_parser("scale", help="partial exit")
    sc.add_argument("symbol", type=str.upper)
    sc.add_argument("--lots", type=int, required=True)
    sc.add_argument("--px", type=float, required=True)
    sc.add_argument("--reason", type=str)
    sc.add_argument("--fee", type=float)
    sc.add_argument("--date", type=str,
                       help="trade date if back-dating a reconciled exit")
    sc.set_defaults(fn=lambda a: _exit_event(a, "scale"))

    c = sub.add_parser("close", help="full exit")
    c.add_argument("symbol", type=str.upper)
    c.add_argument("--px", type=float, required=True)
    c.add_argument("--reason", type=str)
    c.add_argument("--fee", type=float)
    c.add_argument("--lots", type=int, default=0)
    c.add_argument("--date", type=str,
                       help="trade date if back-dating a reconciled exit")
    c.set_defaults(fn=lambda a: _exit_event(a, "close"))

    e = sub.add_parser("equity", help="set account equity")
    e.add_argument("--set", type=float, required=True)
    e.add_argument("--note", type=str)
    e.set_defaults(fn=cmd_equity)

    n = sub.add_parser("note", help="free-text note (ignored by all math)")
    n.add_argument("text")
    n.set_defaults(fn=cmd_note)

    sh = sub.add_parser("show", help="current state")
    sh.add_argument("--summary", action="store_true")
    sh.set_defaults(fn=lambda a: show(summary=a.summary))

    v = sub.add_parser("verify", help="check the ledger for hand-edit damage")
    v.set_defaults(fn=lambda a: verify())

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
