#!/usr/bin/env python3
"""The morning report: the momentum board, plus the three things it cannot show.

Runs at 07:30 WIB, after `idx-trade-plan` (07:15) and before `idx-brief` (07:45). The 07:00 job
on day D scores session D-1, so this reports the previous close. It reads what that job already
produced and adds ZERO Invezgo requests: the extra sections come from Yahoo, which reproduces the
panel EXACTLY for the price leg (147 names, 147 verdicts agreeing, median difference 0.000 on
rvol5, dd60 and rsi, measured 2026-08-29).

FIVE SECTIONS, ORDERED BY EVIDENTIAL STRENGTH, WHICH IS THE POINT

  1. BOARD          both legs. The only section with a validated result behind it.
  2. DIRECTION      where rvol5 has been for six sessions.
  3. LEG 2 ONLY     price passes, accumulation does not. Half a gate, labelled as half.
  4. UNIVERSE GAP   would qualify, but the panel cannot see it.
  5. AVOID          rvol5 >= 3.0.

WHY DIRECTION IS HERE. The board publishes a LEVEL. On 2026-08-27 KIJA appeared as a Tier A
candidate at rvol5 2.57 -- having spent the three previous sessions at 3.41, 3.54 and 3.59, all
above the 3.0 exhaustion ceiling. A name decaying through the band from above and one rising into
it from below are opposite situations and `is_momentum` cannot tell them apart. RVOL carries
+1.15pp of the rule's +1.28pp measured lift, so which way it is moving is the highest-value column
the board does not have.

WHY THERE IS A LIQUIDITY FLOOR. RVOL is a RATIO. A thin stock trading 2.3x its own thin average
clears the gate on nothing: GEMS passed the price leg on 2026-08-28 on 69,500 shares, about
Rp481m. The real board is protected from this by leg 1's Rp500m broker-net minimum; a leg-2-only
screen has no such protection and must declare its own.

Usage:
    py scripts/build_daily_report.py --dry-run
    py scripts/build_daily_report.py --summary          # plain text for notify_telegram.py
    py scripts/build_daily_report.py --backtest-render 10
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import statistics
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel  # noqa: E402
from fetch_prices import yahoo_chart  # noqa: E402  stdlib urllib; no `requests` dependency
from momentum_setup import is_momentum  # noqa: E402
from overlay_test import features  # noqa: E402
import build_momentum_board as B  # noqa: E402  the live gate constants, never a copy

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "yahoo"          # under data/, which .gitignore already excludes
BOARD = PANEL / "momentum_board.json"
SITE = "https://andrebenas77.github.io/idx-telegram-screener/momentum.html"

# Declared thresholds. Changed only deliberately, never to make a section look better.
MIN_VALUE_IDR = 5e9        # median daily traded value for the leg-2 sections. See the docstring.
TRAJ_SESSIONS = 6          # how far back DIRECTION looks
BREADTH = 0.60             # a date needs this share of symbols to count as a trading day
CAL_MIN_HISTORY = 120      # features() needs 120 sessions

# Names outside the pool worth scoring anyway. Seeded from the traded-value audit of 2026-08-29,
# which found seven names inside the market's top 60 by median daily value that the panel cannot
# see -- the pool is admitted from a top-10 call while the board universe is top-20, leaving
# ranks 11-20 permanently invisible. Refresh this list when that audit is re-run; it is a
# stopgap for a structural gap, not a fix for it.
EXTRA_WATCH = ["INET", "VKTR", "ARCI", "PSAB", "RATU", "RMKE", "CDIA", "COIN"]

GUARDED = ["scripts/build_momentum_board.py", "data/panel/momentum_board.json",
           "docs/momentum.html", "docs/index.html"]


# --------------------------------------------------------------------------- guards

def fingerprints() -> dict:
    out = {}
    for rel in GUARDED:
        p = ROOT / rel
        out[rel] = hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else None
    return out


def assert_unmoved(before: dict) -> None:
    now = fingerprints()
    moved = [k for k in before if before[k] != now[k]]
    if moved:
        raise SystemExit("REFUSING TO CONTINUE: these files changed during the run: "
                         + ", ".join(moved))


# --------------------------------------------------------------------------- yahoo

def fetch(sym: str, log) -> dict | None:
    """Yahoo daily bars, cached per day, retried on TRANSIENT failure only.

    The retry is not defensive padding. `unattended-upgrades` runs on the VPS around 03:05 and
    06:07 WIB and needrestart bounces long-running units, which races `systemd-resolved` coming
    back; a process that touches the network in its first second can see
    `Temporary failure in name resolution`. That killed the quant bot for five hours on
    2026-08-27, and `After=network-online.target` does not protect against it because it orders
    BOOT, not a mid-life bounce. So: DNS and connect failures retry; anything else returns None
    and the symbol is simply absent from the report rather than taking the run down with it.
    """
    day = datetime.date.today().isoformat()
    f = CACHE / day / (sym + ".json")
    if f.exists() and f.stat().st_size > 200:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            pass
    f.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 4):
        try:
            d = yahoo_chart(sym + ".JK", "2y")
            f.write_text(json.dumps(d), encoding="utf-8")
            time.sleep(0.12)
            return d
        except (urllib.error.URLError, OSError) as e:
            log("   %s attempt %d: %s -- retrying" % (sym, attempt, type(e).__name__))
            time.sleep(2.0 * attempt)
        except urllib.error.HTTPError as e:
            log("   %s: HTTP %s -- not retrying" % (sym, e.code))
            return None
    return None


class Shim:
    """Panel-shaped, holding only what overlay_test.features and structure() read."""


def build_yahoo_panel(syms, log):
    """A Panel-shaped object over a REAL trading calendar.

    The calendar is built from dates carrying >= BREADTH of symbols, not from the union. Yahoo
    emits a record for a handful of names on IDX market holidays -- 2026-08-17, 08-25, 05-01,
    06-01 and 06-16 each show 4 to 7 symbols of 167 -- and because `features()` requires 60
    CONTIGUOUS sessions, five such dates put a hole in every other symbol's window and cut the
    scored universe from 167 names to ONE. That is an absence of calendar hygiene wearing the
    costume of an absence of candidates, and it is silent.
    """
    bars, failed = {}, []
    for k, s in enumerate(syms):
        d = fetch(s, log)
        if not d:
            failed.append(s)
            continue
        try:
            res = d["chart"]["result"][0]
            ts, q = res["timestamp"], res["indicators"]["quote"][0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
        except (KeyError, IndexError, TypeError):
            failed.append(s)
            continue
        rows = {}
        for i, t in enumerate(ts):
            o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i],
                             q["close"][i], q["volume"][i])
            if None in (o, h, l, c):
                continue
            day = datetime.datetime.fromtimestamp(t, datetime.UTC).strftime("%Y-%m-%d")
            rows[day] = (o, h, l, c, v or 0, adj[i] if adj[i] is not None else c)
        if rows:
            bars[s] = rows
        if (k + 1) % 50 == 0:
            log("   ... %d/%d" % (k + 1, len(syms)))

    cov = {}
    for r in bars.values():
        for d in r:
            cov[d] = cov.get(d, 0) + 1
    need = BREADTH * max(len(bars), 1)
    dates = sorted(d for d, n in cov.items() if n >= need)
    dropped = sorted(d for d, n in cov.items() if n < need)
    log("   calendar: %d trading dates, %d thin dates dropped%s"
        % (len(dates), len(dropped),
           (" (" + ", ".join("%s:%d" % (d, cov[d]) for d in dropped[-5:]) + ")")
           if dropped else ""))

    p = Shim()
    p.dates = dates
    p.didx = {d: i for i, d in enumerate(dates)}
    p.close, p.raw_close, p.high, p.low, p.volume, p.open = {}, {}, {}, {}, {}, {}
    for s, rows in bars.items():
        for store in (p.close, p.raw_close, p.high, p.low, p.volume, p.open):
            store[s] = {}
        for d, (o, h, l, c, v, a) in rows.items():
            i = p.didx.get(d)
            if i is None:
                continue
            p.open[s][i], p.high[s][i], p.low[s][i] = o, h, l
            p.raw_close[s][i], p.volume[s][i], p.close[s][i] = c, v, a
    return p, bars, failed


def median_value(p, sym, i, win: int = 20):
    """Median daily traded value over `win` sessions, in rupiah. The liquidity floor."""
    rc, vo = p.raw_close.get(sym) or {}, p.volume.get(sym) or {}
    xs = [rc[j] * vo[j] for j in range(i - win + 1, i + 1)
          if j in rc and j in vo and rc[j] > 0 and vo[j] > 0]
    return statistics.median(xs) if len(xs) >= win // 2 else None


# --------------------------------------------------------------------------- sections

def trajectory(p, sym, i, n: int = TRAJ_SESSIONS):
    out = []
    for j in range(i - n + 1, i + 1):
        f = features(p, sym, j)
        out.append(f["rvol5"] if f and f.get("rvol5") is not None else None)
    return out


def direction(traj) -> str:
    """Label the last three readings. The band edges matter more than the slope.

    A name whose window peaked at or above the exhaustion ceiling is called out separately
    however gentle its current slope: above 3.0 the rule does not weaken, it inverts, and a name
    arriving in the band from there is not the same object as one arriving from below.
    """
    xs = [x for x in traj if x is not None]
    if len(xs) < 3:
        return "?"
    a, b = xs[-3], xs[-1]
    if max(xs) >= B.EXHAUST_RVOL:
        return "decayed from above %.1f" % B.EXHAUST_RVOL
    if b > a * 1.08:
        return "rising into the band"
    if b < a * 0.92:
        return "falling"
    return "holding"


def collect(p, pool, extras, i, log):
    """Score every symbol once. Sections are views over this, not separate passes."""
    rows = []
    for s in sorted(set(pool) | set(extras)):
        if s not in p.close:
            continue
        f = features(p, s, i)
        if not f or f.get("rvol5") is None:
            continue
        mv = median_value(p, s, i)
        rows.append({
            "symbol": s, "in_pool": s in pool,
            "rvol5": f["rvol5"], "rsi": f["rsi"], "dd60": f["dd60"],
            "cmf20": f.get("cmf20"), "clv": f.get("clv"), "trend": f.get("trend"),
            "range_pct": f.get("range_pct"), "close": (p.raw_close.get(s) or {}).get(i),
            "median_value": mv,
            "pass2": is_momentum(f, B.RVOL_MIN, B.DD_MIN, B.RSI_MIN, B.RVOL_MAX),
            "exhaust": f["rvol5"] >= B.EXHAUST_RVOL,
        })
    log("   scored %d symbols" % len(rows))
    return rows


# --------------------------------------------------------------------------- rendering

def summary_text(session, board, rows, p, i, stale_note) -> str:
    """Plain text for notify_telegram.py.

    Plain text on purpose and NEVER markdown: Telegram's MarkdownV2 returns HTTP 400 on
    unescaped '.', '-' and '(', and every line below is full of all three. `split_message()`
    chunks at 3800 characters on LINE boundaries, so a table row never breaks mid-row.
    """
    by = {r["symbol"]: r for r in rows}
    L = ["IDX DAILY - session %s" % session]
    if stale_note:
        L.append(stale_note)

    names = [c["symbol"] for c in (board.get("candidates") or [])]
    seen, ordered = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)

    L.append("")
    if not board:
        L.append("BOARD - both legs: NOT AVAILABLE for this session")
        L.append("  the snapshot on disk covers a different date. Absent, not empty.")
    else:
        L.append("BOARD - both legs (%d)" % len(ordered))
        if not ordered:
            L.append("  none")
    for n in ordered:
        r = by.get(n)
        if not r:
            L.append("  %s (not scoreable from price data)" % n)
            continue
        L.append("  %-5s %8s | RVOL %.2f RSI %.0f DD60 %+.3f CMF %s"
                 % (n, fmt_px(r["close"]), r["rvol5"], r["rsi"], r["dd60"],
                    "-" if r["cmf20"] is None else "%+.2f" % r["cmf20"]))
        traj = trajectory(p, n, i)
        L.append("    rvol5 %s  -> %s"
                 % (" ".join("-" if x is None else "%.2f" % x for x in traj), direction(traj)))

    liquid = [r for r in rows
              if r["median_value"] and r["median_value"] >= MIN_VALUE_IDR]
    only2 = [r for r in liquid if r["pass2"] and r["symbol"] not in seen and r["in_pool"]]
    gap = [r for r in liquid if r["pass2"] and not r["in_pool"]]
    avoid = [r for r in liquid if r["exhaust"]]
    only2.sort(key=lambda r: -r["rvol5"])
    gap.sort(key=lambda r: -r["rvol5"])
    avoid.sort(key=lambda r: -r["rvol5"])

    L.append("")
    L.append("LEG 2 ONLY - price passes, accumulation not checked (%d)" % len(only2))
    L.append("  half a gate. no evidence anyone is buying these.")
    for r in only2[:12]:
        L.append("  %-5s RVOL %.2f RSI %.0f DD60 %+.3f CMF %s  [%s]"
                 % (r["symbol"], r["rvol5"], r["rsi"], r["dd60"],
                    "-" if r["cmf20"] is None else "%+.2f" % r["cmf20"],
                    direction(trajectory(p, r["symbol"], i))))
    if not only2:
        L.append("  none")

    L.append("")
    L.append("UNIVERSE GAP - would qualify, panel cannot see it (%d)" % len(gap))
    for r in gap[:8]:
        L.append("  %-5s RVOL %.2f RSI %.0f  Rp%.1fb/day  [%s]"
                 % (r["symbol"], r["rvol5"], r["rsi"], (r["median_value"] or 0) / 1e9,
                    direction(trajectory(p, r["symbol"], i))))
    if not gap:
        L.append("  none")

    L.append("")
    L.append("AVOID - RVOL >= %.1f, the edge inverts above here (%d)" % (B.EXHAUST_RVOL,
                                                                        len(avoid)))
    for r in avoid[:8]:
        L.append("  %-5s RVOL %.2f RSI %.0f  [%s]"
                 % (r["symbol"], r["rvol5"], r["rsi"],
                    direction(trajectory(p, r["symbol"], i))))
    if not avoid:
        L.append("  none")

    L.append("")
    L.append("[label] is where rvol5 has been over %d sessions. The board sees only the level;"
             % TRAJ_SESSIONS)
    L.append("a name falling through the band from above 3.0 is not the same as one rising in.")
    L.append("Sections 2-5 are price-only, from free data, above a Rp%.0fbn/day floor."
             % (MIN_VALUE_IDR / 1e9))
    L.append("Only BOARD carries the validated result. Gross of costs.")
    L.append(SITE)
    return "\n".join(L)


def fmt_px(v):
    """Indonesian thousands separator. `%` formatting has no comma flag -- str.format does."""
    return "-" if v is None else "{:,.0f}".format(v).replace(",", ".")


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--summary", action="store_true",
                    help="print plain text to stdout for notify_telegram.py")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backtest-render", type=int, default=0,
                    help="render the last N sessions and stop, for eyeballing")
    a = ap.parse_args()

    # The house idiom: under --summary stdout carries ONLY the message, so $(... --summary)
    # in run_daily.sh captures cleanly.
    log = (lambda *x: None) if a.summary else (lambda *x: print(*x))

    before = fingerprints()

    board = {}
    if BOARD.exists():
        try:
            board = json.loads(BOARD.read_text(encoding="utf-8"))
        except ValueError:
            pass

    rp = Panel()
    rp.load_prices()
    pool = sorted(rp.close)
    log("panel %d symbols, last session %s" % (len(pool), rp.dates[-1] if rp.dates else "?"))

    p, bars, failed = build_yahoo_panel(pool + [s for s in EXTRA_WATCH if s not in pool], log)
    if failed:
        log("   %d symbols unavailable: %s" % (len(failed), ", ".join(failed[:10])))
    if not p.dates:
        print("no trading dates -- refusing to report", file=sys.stderr)
        return 2

    sessions = [a.date] if a.date else p.dates[-max(a.backtest_render, 1):]
    for session in sessions:
        i = p.didx.get(session)
        if i is None or i < CAL_MIN_HISTORY:
            log("skipping %s (not enough history)" % session)
            continue
        # momentum_board.json is a SINGLE snapshot, not a history. Replaying its candidates
        # against a session it does not cover would print eight names under a date they were
        # never selected for -- a board-shaped object that is not the board. Show it only for
        # its own session, and say plainly that it is missing otherwise.
        matched = board.get("session") == session
        stale = ""
        if board.get("session") and not matched:
            stale = ("BOARD snapshot is session %s, so it is omitted below; the price sections "
                     "are %s." % (board["session"], session))
        rows = collect(p, set(pool), EXTRA_WATCH, i, log)
        text = summary_text(session, board if matched else {}, rows, p, i, stale)
        if a.summary:
            print(text)
        else:
            print("\n" + "=" * 78)
            print(text)

    assert_unmoved(before)
    if not a.summary:
        print("\nguarded files verified unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
