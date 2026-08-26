#!/usr/bin/env python3
"""Regression harness for the trade layer: golden ledger events + screener contract.

Two things this protects, both of which are silent when they break.

**Golden events.** The bot and the CLI must emit BYTE-IDENTICAL ledger events. The
refactor that lets a Telegram command write the book moves event construction out of
`cmd_open` and friends into shared builders, and the whole risk of that move is a field
quietly changing name, type or rounding. So the exact dicts the CLI writes TODAY are
frozen here, replayed against a scratch book via `IDX_BOOK_DIR`
(`position_book.py:46-50` exists for this), and compared field-for-field forever after.

`ts` is excluded from comparison and nothing else is: it is wall-clock by construction.
Every other volatile input is pinned — `--date` is passed explicitly, prices and lots
are fixed, and fees fall out of them deterministically.

**Screener contract.** The daily board pipeline is the thing that must not break while
the trade layer changes underneath it. Its read-only entry points are captured and
diffed, so "I only touched the bot" is a claim the harness can check rather than a
hope.

Usage:
    py scripts/trade_bot_test.py --capture     # freeze goldens (run on known-good code)
    py scripts/trade_bot_test.py               # verify against them
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
# The two goldens live apart ON PURPOSE, and the boundary is privacy, not tidiness.
#
# ledger_events.json is SYNTHETIC — a made-up BBCA trade at made-up prices against a
# scratch book — so it carries nothing about the real book and is versioned, which is
# what lets the harness run on a fresh clone or on the VPS.
#
# screener_contract.json captures `trade_plan --dry-run` stdout, and that lists the REAL
# open positions with lots and entry prices. It stays under data/panel/, which .gitignore
# excludes, because this repo is public. Putting both in one directory would be one
# `git add` away from publishing the book.
GOLDEN = ROOT / "reference" / "golden" / "ledger_events.json"
CONTRACT = ROOT / "data" / "panel" / "golden" / "screener_contract.json"
PY = sys.executable

# A full lifecycle, in order. Every value is pinned so the resulting events are a pure
# function of the code. `open` carries an explicit --date because it defaults to today;
# the exit events have no --date flag today (that is one of the gaps P1 closes) so their
# trade date rides on `ts`, which is why `ts` is excluded rather than normalised.
SCENARIO = [
    ("equity", ["equity", "--set", "1000000000", "--note", "harness book"]),
    ("open", ["open", "BBCA", "--lots", "100", "--px", "9000", "--stop", "8700",
              "--basis", "atr1.5 from entry", "--sector", "Bank",
              "--rule", "board+size", "--date", "2026-01-05"]),
    ("stop", ["stop", "BBCA", "--px", "8800", "--reason", "trail to breakeven"]),
    ("scale", ["scale", "BBCA", "--lots", "30", "--px", "9500", "--reason", "scale 1/3"]),
    ("close", ["close", "BBCA", "--px", "9800", "--reason", "E2 structure"]),
    ("note", ["note", "harness note, ignored by all math"]),
]

VOLATILE = ("ts", "_line")


def run_scenario() -> list[dict]:
    """Replay SCENARIO against a scratch book and return the events it produced."""
    tmp = Path(tempfile.mkdtemp(prefix="idxbook-"))
    try:
        # Pin the risk config as well as the book. load_trade_env() gives the real
        # ENVIRONMENT precedence over secrets/trade.env (trade_lib.py:283-302), so this
        # makes the harness hermetic: the golden cannot drift because someone changed
        # their book size or renegotiated their brokerage. Without it the "regression"
        # would fire on a config change and stay silent on an actual code change.
        env = dict(os.environ, IDX_BOOK_DIR=str(tmp),
                   TRADE_EQUITY_IDR="1000000000", TRADE_RISK_PCT="0.005",
                   TRADE_SIZING_MODE="risk",
                   TRADE_FEE_BUY="0.0010", TRADE_FEE_SELL="0.0010")
        for label, argv in SCENARIO:
            r = subprocess.run([PY, str(SCRIPTS / "position_book.py"), *argv],
                               capture_output=True, text=True, env=env, cwd=str(ROOT))
            if r.returncode != 0:
                raise SystemExit(f"[!!] scenario step '{label}' failed (exit {r.returncode})\n"
                                 f"{r.stdout}\n{r.stderr}")
        ledger = tmp / "ledger.jsonl"
        if not ledger.exists():
            raise SystemExit("[!!] scenario wrote no ledger")
        out = []
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append({k: v for k, v in json.loads(line).items() if k not in VOLATILE})
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CONTRACT_CMDS = [
    ("momentum_summary", ["build_momentum_board.py", "--summary"]),
    ("trade_plan_dry", ["trade_plan.py", "--dry-run", "--summary"]),
]


# docs/ files the contract commands touch as a side effect. build_momentum_board.py
# rewrites docs/momentum.html on EVERY run, including --summary — there is no read-only
# mode. A dirty docs/ is not cosmetic: it makes the VPS's `git pull --rebase` fail at the
# top of run_daily.sh, which silently strands the box on old code. So the harness restores
# them, and a harness that leaves the tree dirty would be a worse bug than the one it hunts.
TOUCHED = ("docs/momentum.html",)


def run_contract() -> dict:
    """Read-only screener entry points: exit code + output.

    'Read-only' describes intent, not behaviour — see TOUCHED. Anything they rewrite is
    restored from git before this returns.
    """
    # The board's output is a function of the PANEL, which is refreshed daily and
    # legitimately changes the answer. So the contract records which panel produced it,
    # and a comparison across different panels checks the exit code only — the same gate
    # trade_backtest's --regress uses, for the same reason: diffing results computed on
    # different data reports a regression that is really just Tuesday.
    from alpha_lib import panel_fingerprint
    out = {"_panel": panel_fingerprint()}
    try:
        for name, argv in CONTRACT_CMDS:
            r = subprocess.run([PY, str(SCRIPTS / argv[0]), *argv[1:]],
                               capture_output=True, text=True, cwd=str(ROOT))
            out[name] = {"rc": r.returncode, "stdout": r.stdout}
    finally:
        dirty = subprocess.run(["git", "status", "--porcelain", *TOUCHED],
                               capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
        if dirty:
            subprocess.run(["git", "checkout", "--", *TOUCHED],
                           capture_output=True, text=True, cwd=str(ROOT))
            print(f"  (restored {len(dirty.splitlines())} file(s) the board rewrote)")
    return out


def diff_events(golden: list[dict], fresh: list[dict]) -> list[str]:
    problems = []
    if len(golden) != len(fresh):
        problems.append(f"event count {len(fresh)} != golden {len(golden)}")
    for i, (g, f) in enumerate(zip(golden, fresh)):
        for k in sorted(set(g) | set(f)):
            if k not in g:
                problems.append(f"event {i} ({f.get('type')}): NEW field {k!r} = {f[k]!r}")
            elif k not in f:
                problems.append(f"event {i} ({g.get('type')}): LOST field {k!r} (was {g[k]!r})")
            elif repr(g[k]) != repr(f[k]):
                problems.append(f"event {i} ({g.get('type')}): {k!r} {g[k]!r} -> {f[k]!r}")
    return problems


# ------------------------------------------------------------------ write lifecycle
#
# Exercised against a scratch book via IDX_BOOK_DIR. Every assertion here is a failure
# mode that would otherwise be discovered on a real ledger, which is append-only and
# therefore unforgiving.


def run_lifecycle() -> list:
    problems = []
    tmp = Path(tempfile.mkdtemp(prefix="idxlife-"))
    os.environ["IDX_BOOK_DIR"] = str(tmp)
    os.environ.update(TRADE_EQUITY_IDR="1000000000", TRADE_RISK_PCT="0.005",
                      TRADE_SIZING_MODE="risk", TRADE_FEE_BUY="0.0010",
                      TRADE_FEE_SELL="0.0010")
    try:
        import bot_state
        import position_book as pb
        import trade_bot as tb

        def chk(name, cond, detail=""):
            print(f"  [{'ok' if cond else '!!'}] {name}{'' if cond else '  <- ' + detail}")
            if not cond:
                problems.append(name)

        def d(cmd, *args):
            return tb.dispatch(cmd, list(args), {"chat_id": "1"}) or ""

        pb.append_event({"ts": pb.now_wib(), "type": "equity",
                         "equity_idr": 1_000_000_000.0, "note": "lifecycle"})

        print("rejections that must happen before anything is written")
        chk("bad ticker is refused with a suggestion",
            "Did you mean" in d("open", "ISTA", "100", "2000"))
        chk("missing arguments print the usage", "Usage:" in d("open", "DMAS"))
        chk("non-numeric lots are caught", "Could not read" in d("open", "DMAS", "abc", "167"))
        chk("nothing pending means nothing to confirm",
            "Nothing to confirm" in d("yes"))
        chk("no ledger events written yet", len(pb.read_events()) == 1,
            f"{len(pb.read_events())} events")

        print("open -> confirm")
        t = d("open", "DMAS", "10000", "167")
        chk("ticket is built", "CONFIRM OPEN DMAS" in t, t[:80])
        chk("ticket shows shares and book share", "shares" in t and "of book" in t)
        chk("ticket shows the risk multiple", "your risk budget" in t)
        chk("a pending ticket exists", bot_state.load_pending() is not None)
        chk("the pending event carries no ts",
            "ts" not in (bot_state.load_pending() or {}).get("event", {}))
        chk("STILL nothing written", len(pb.read_events()) == 1)

        r = d("yes")
        chk("commit reports RECORDED", r.startswith("RECORDED"), r[:80])
        chk("exactly one event appended", len(pb.read_events()) == 2)
        chk("pending is cleared", bot_state.load_pending() is None)
        st = pb.rebuild()
        chk("position is open", "DMAS" in st["positions"])
        chk("r_idr matches (entry-stop)*lots*100",
            abs(st["positions"]["DMAS"]["r_idr"]
                - (167 - st["positions"]["DMAS"]["stop_px"]) * 10000 * 100) < 1.0)

        print("duplicate and conflicting writes")
        chk("a second /yes finds nothing", "Nothing to confirm" in d("yes"))
        chk("re-opening a live symbol is refused",
            "already open" in d("open", "DMAS", "500", "170"))
        chk("still two events", len(pb.read_events()) == 2)

        print("stop discipline")
        cur = st["positions"]["DMAS"]["stop_px"]
        chk("lowering a stop is refused", "BELOW the current stop"
            in d("stop", "DMAS", str(int(cur) - 5)))
        d("stop", "DMAS", str(int(cur) + 3))
        chk("raising a stop builds a ticket", bot_state.load_pending() is not None)
        d("yes")
        chk("stop moved up", pb.rebuild()["positions"]["DMAS"]["stop_px"] > cur)

        print("the fingerprint guard")
        d("scale", "DMAS", "3000", "180")
        chk("scale ticket built", bot_state.load_pending() is not None)
        pb.append_event({"ts": pb.now_wib(), "type": "note",
                         "text": "something else wrote while the ticket was open"})
        out = d("yes")
        chk("a moved book refuses the stale ticket", "book changed" in out, out[:90])
        chk("pending cleared after refusal", bot_state.load_pending() is None)

        print("expiry")
        d("scale", "DMAS", "3000", "180")
        pend = bot_state.load_pending()
        pend["expires_ts"] = "2020-01-01T00:00:00+07:00"
        bot_state.save_pending(pend)
        chk("an expired ticket is refused", "expired" in d("yes"))

        print("scale then close")
        n_before = len(pb.read_events())
        d("scale", "DMAS", "3000", "180")
        d("yes")
        chk("scale recorded", len(pb.read_events()) == n_before + 1)
        chk("7000 lots remain", pb.rebuild()["positions"]["DMAS"]["lots"] == 7000)
        chk("scaling the whole line is refused",
            "use /close" in d("scale", "DMAS", "7000", "185").lower())
        d("close", "DMAS", "185")
        d("yes")
        chk("position is closed", "DMAS" not in pb.rebuild()["positions"])
        chk("closed trade recorded", len(pb.rebuild()["closed"]) == 1)

        print("the ledger survives all of it")
        chk("verify finds no problems", pb.verify_events(pb.read_events()) == [],
            str(pb.verify_events(pb.read_events())[:2]))
        chk("/no discards cleanly", "Discarded" in (d("open", "BBCA", "100", "9000")
                                                     and d("no")))
        chk("no phantom position", "BBCA" not in pb.rebuild()["positions"])
    finally:
        os.environ.pop("IDX_BOOK_DIR", None)
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--capture", action="store_true",
                    help="freeze the current behaviour as the golden (known-good code only)")
    ap.add_argument("--lifecycle", action="store_true",
                    help="exercise the write commands against a scratch book")
    ap.add_argument("--skip-contract", action="store_true",
                    help="ledger events only; skips the slower screener commands")
    a = ap.parse_args()

    if a.lifecycle:
        print("exercising the write commands against a scratch book...")
        probs = run_lifecycle()
        print()
        if probs:
            print(f"[!!] {len(probs)} failed: {', '.join(probs)}")
            return 1
        print("[ok] full write lifecycle and every rejection path behaved")
        return 0

    print("replaying the ledger lifecycle against a scratch book...")
    fresh = run_scenario()
    print(f"  {len(fresh)} events: " + ", ".join(e.get("type", "?") for e in fresh))

    contract = None
    if not a.skip_contract:
        print("running the read-only screener entry points...")
        contract = run_contract()
        for k, v in contract.items():
            if k == "_panel":
                continue
            n = len(v["stdout"].splitlines())
            print(f"  {k}: exit {v['rc']}, {n} lines")

    if a.capture:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(fresh, indent=2), encoding="utf-8")
        print(f"\nwrote {GOLDEN}")
        if contract is not None:
            CONTRACT.write_text(json.dumps(contract, indent=2), encoding="utf-8")
            print(f"wrote {CONTRACT}")
        print("goldens frozen. Re-run WITHOUT --capture to verify.")
        return 0

    if not GOLDEN.exists():
        print(f"[!!] no golden at {GOLDEN} — run with --capture on known-good code first")
        return 3
    problems = diff_events(json.loads(GOLDEN.read_text(encoding="utf-8")), fresh)

    if contract is not None and CONTRACT.exists():
        gc = json.loads(CONTRACT.read_text(encoding="utf-8"))
        same_panel = gc.get("_panel") == contract.get("_panel")
        if not same_panel:
            print("  [note] panel differs from the golden — checking exit codes only")
        for k, v in contract.items():
            if k == "_panel" or k not in gc:
                continue
            if gc[k]["rc"] != v["rc"]:
                problems.append(f"contract {k}: exit {gc[k]['rc']} -> {v['rc']}")
            elif same_panel and gc[k]["stdout"] != v["stdout"]:
                problems.append(f"contract {k}: stdout changed "
                                f"({len(gc[k]['stdout'])} -> {len(v['stdout'])} chars)")

    print()
    if problems:
        print(f"[!!] {len(problems)} regression(s):")
        for p in problems:
            print(f"  {p}")
        return 1
    print("[ok] ledger events byte-identical to golden; screener contract unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
