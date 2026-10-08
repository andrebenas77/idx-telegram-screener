#!/usr/bin/env python3
"""The market's own hot list: every listed name ranked by traded value, for free.

WHY THIS EXISTS
    SQMI went from 63 to 106 between 2026-08-31 and 2026-10-07 and appeared on no screen we
    run. Nothing rejected it. The panel is `reference/tickers.csv` filtered to `liquid=1`, a
    hand-kept list of 112 names, and SQMI has no row. `build_universe.py` was written in August
    to fix exactly this for DSSA, but production runs it with `--no-discover`, so it ranks only
    the names the panel already holds and an outside name can never enter. Measured 2026-10-08
    over the whole market: 38 of the 74 names that were top-20 by value in the last 40 sessions
    sit outside the panel, DSSA on 40 sessions of 40.

    So this ranks the WHOLE roster, not the panel, and it costs nothing: Yahoo daily bars for
    the 962 listed codes, about two minutes on six threads. No Invezgo request, no Sectors
    credit.

THE RULE
        hot(D) = top-40 by daily value (close * volume) on >= 1 of the 40 sessions ending D

    Top 40, not the top 20 `build_universe.py` uses: on Rp13tn of daily turnover the top 20
    holds 55% of the value and cuts off at Rp163bn a day; the top 40 holds 71% and cuts off at
    Rp72bn (owner's call, 2026-10-08). It also contains all eleven names that had been added to
    the daily report's EXTRA_WATCH by hand, which the top 20 does not. `build_universe.TOP_N`
    stays 20 because the broker board and accum_test.py read the partitions it writes.

    ">= 1 session" admits one-day spikes: 28 of the 77 outside names have a median day under
    Rp5bn. That is deliberate. This file decides what gets LOOKED AT; the liquidity floors in
    build_daily_report.py decide what gets SHOWN, and a second floor here would have hidden SQMI
    on 2026-09-04, when its median day was still Rp2.5bn and it had just traded Rp167bn.

WHAT IT WRITES
    data/panel/market_hot.json            today's list, read by build_daily_report.py
    data/panel/market_hot_history.jsonl   one line per session, so membership is a dated fact
                                          and not something recomputed after the event

    It refuses to write when under 80% of the roster comes back: a throttled pull ranks the
    names that happened to answer, and a top 40 of half the market is not the top 40.

Usage:
    py scripts/build_market_hot.py --dry-run
    py scripts/build_market_hot.py
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel  # noqa: E402
from build_daily_report import BREADTH, TRADED_MIN, closed  # noqa: E402  the report's calendar rule, never a copy
from build_universe import churn, hot_list, rank_days  # noqa: E402
from fetch_prices import WIB, yahoo_chart  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ROSTER = ROOT / "reference" / "idx_roster.csv"
TICKERS = ROOT / "reference" / "tickers.csv"
OUT = PANEL / "market_hot.json"
HISTORY = PANEL / "market_hot_history.jsonl"

TOP_N = 40            # see THE RULE
LOOKBACK = 40         # sessions, ~2 months: IDX themes are short-lived
RANGE = "6mo"         # ~125 sessions: the lookback plus the 20 the median needs, with room
MIN_RETURNED = 0.80   # share of the roster that must answer before a ranking is trusted
MEDIAN_WIN = 20
PANEL_RECENT = 5      # a panel name is live if it carries a bar in this many trailing sessions
WORKERS = 6


def fetch(code: str) -> tuple[str, dict | None]:
    """{date: (close, volume)} for one code, or None.

    404 and 422 are final: a delisted or renamed code never answers and retrying it only
    slows the run. Anything else (429, DNS, a reset) backs off and tries again.
    """
    for attempt in range(5):
        try:
            res = yahoo_chart(code + ".JK", RANGE)["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            bars = {}
            for t, c, v in zip(res.get("timestamp") or [], q["close"], q["volume"]):
                if c is not None:
                    bars[datetime.fromtimestamp(t, WIB).strftime("%Y-%m-%d")] = (c, v or 0)
            return code, bars or None
        except urllib.error.HTTPError as e:      # before URLError: HTTPError IS a URLError
            if e.code in (404, 422):
                return code, None
        except (urllib.error.URLError, OSError, KeyError, IndexError, TypeError, ValueError):
            pass
        time.sleep(min(2 ** attempt, 20) + random.random())
    return code, None


def fetch_all(codes: list[str], log) -> dict[str, dict]:
    out, t0 = {}, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for n, (code, bars) in enumerate(ex.map(fetch, codes), 1):
            if bars:
                out[code] = bars
            if n % 200 == 0 or n == len(codes):
                log("   ... %d/%d, %d answered, %.0fs" % (n, len(codes), len(out), time.time() - t0))
    return out


def real_sessions(bars: dict[str, dict]) -> list[str]:
    """Closed trading dates. Same two tests as build_daily_report.build_yahoo_panel.

    Yahoo prints a bar for a handful of names on some IDX holidays and a zero-volume bar for
    EVERY name on others, so a date must carry BREADTH of the names and volume on TRADED_MIN of
    the bars it carries. And the newest bar is still forming until 16:15 WIB.
    """
    cov, trd = {}, {}
    for rows in bars.values():
        for d, (_, v) in rows.items():
            cov[d] = cov.get(d, 0) + 1
            trd[d] = trd.get(d, 0) + (1 if v > 0 else 0)
    need = BREADTH * max(len(bars), 1)
    return sorted(d for d, n in cov.items()
                  if n >= need and trd[d] >= TRADED_MIN * n and closed(d))


def panel_live(sessions: list[str]) -> tuple[set[str], str | None]:
    """Names the board can actually score, and the panel's last session.

    Not `set(panel.close)`: ENRG's last panel bar is 2026-08-14 and ADHI's 08-21, and a name
    whose history stopped two months ago is in the files but not on the board. Measured against
    the MARKET's calendar, not the panel's own: a panel that stopped refreshing six weeks ago
    (the laptop's, 2026-08-26) has every name current by its own clock and none by the market's.
    """
    p = Panel()
    p.load_prices()
    if not p.dates:
        return set(), None
    floor = sessions[-PANEL_RECENT] if len(sessions) >= PANEL_RECENT else sessions[0]
    live = {s for s, cl in p.close.items() if cl and p.dates[max(cl)] >= floor}
    return live, p.dates[-1]


def build(bars: dict[str, dict], roster: dict[str, dict], end: str | None,
          lookback: int, top_n: int) -> dict | None:
    dates = real_sessions(bars)
    if end:
        dates = [d for d in dates if d <= end]
    if not dates:
        return None
    keep = set(dates)
    byday: dict[str, dict] = {d: {} for d in dates}
    for sym, rows in bars.items():
        for d, (c, v) in rows.items():
            if d in keep and c > 0 and v > 0:
                byday[d][sym] = (c * v, v)

    ranked = rank_days(byday)
    hot, sessions = hot_list(ranked, dates[-1], lookback, top_n)
    live, panel_last = panel_live(sessions)
    aliased = set()
    if TICKERS.exists():
        with TICKERS.open(encoding="utf-8") as fh:
            aliased = {r["ticker"].strip().upper() for r in csv.DictReader(fh) if r.get("ticker")}

    rank = {d: {r["symbol"]: r["rank_value"] for r in ranked[d]} for d in sessions}
    rows = []
    for s in hot:
        days = [d for d in sessions if rank[d].get(s, 10 ** 6) <= top_n]
        vals = [byday[d].get(s, (0.0, 0.0))[0] for d in sessions[-MEDIAN_WIN:]]
        rows.append({
            "symbol": s,
            "days_top": len(days),
            "days_top20": sum(1 for d in sessions if rank[d].get(s, 10 ** 6) <= 20),
            "last_top": days[-1],
            "median_value": statistics.median(vals),
            "in_panel": s in live,
            "has_alias": s in aliased,
            "board": (roster.get(s) or {}).get("board") or "",
        })
    rows.sort(key=lambda r: (-r["days_top"], -r["median_value"], r["symbol"]))
    return {
        "generated_at": datetime.now(WIB).isoformat(),
        "session": sessions[-1],
        "window": {"start": sessions[0], "end": sessions[-1], "sessions": len(sessions)},
        "lookback": lookback, "top_n": top_n,
        "roster": len(roster), "returned": len(bars),
        "coverage": len(bars) / max(len(roster), 1),
        "churn": churn(ranked, sessions, top_n),
        "panel_live": len(live), "panel_last": panel_last,
        "hot": rows,
    }


def append_history(rep: dict) -> None:
    """One line per session. A re-run replaces its own line: appending would make the same
    session look like two facts."""
    line = {"session": rep["session"], "top_n": rep["top_n"], "lookback": rep["lookback"],
            "hot": [r["symbol"] for r in rep["hot"]],
            "outside": [r["symbol"] for r in rep["hot"] if not r["in_panel"]]}
    kept = []
    if HISTORY.exists():
        for raw in HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(raw).get("session") != rep["session"]:
                    kept.append(raw)
            except ValueError:
                continue
    kept.append(json.dumps(line, ensure_ascii=False))
    HISTORY.write_text("\n".join(kept) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--end", default=None, help="last session (default: latest closed)")
    ap.add_argument("--lookback", type=int, default=LOOKBACK)
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    log = (lambda *x: None) if a.quiet else (lambda *x: print(*x, flush=True))

    with ROSTER.open(encoding="utf-8") as fh:
        roster = {r["code"].strip().upper(): r for r in csv.DictReader(fh) if r.get("code")}
    log("market hot list: top-%d by value, %d-session lookback, roster %d"
        % (a.top_n, a.lookback, len(roster)))

    bars = fetch_all(sorted(roster), log)
    share = len(bars) / max(len(roster), 1)
    if share < MIN_RETURNED:
        print("only %d of %d roster names answered (%.0f%%, need %.0f%%) -- refusing to rank"
              % (len(bars), len(roster), 100 * share, 100 * MIN_RETURNED), file=sys.stderr)
        return 1

    rep = build(bars, roster, a.end, a.lookback, a.top_n)
    if not rep:
        print("no closed trading session in the pull -- refusing to rank", file=sys.stderr)
        return 1
    if rep["window"]["sessions"] < a.lookback:
        log("   [!] only %d sessions in the window, wanted %d"
            % (rep["window"]["sessions"], a.lookback))

    hot = rep["hot"]
    outside = [r for r in hot if not r["in_panel"]]
    thin = [r for r in outside if r["median_value"] < 5e9]
    log("   answered %d/%d (%.0f%%) | sessions %s .. %s | top-%d churn %.1f%%/day"
        % (rep["returned"], rep["roster"], 100 * rep["coverage"], rep["window"]["start"],
           rep["window"]["end"], a.top_n, 100 * rep["churn"]))
    log("   board panel: %d live names, last session %s%s"
        % (rep["panel_live"], rep["panel_last"],
           "" if rep["panel_last"] == rep["session"] else "   <-- BEHIND the market's %s" % rep["session"]))
    log("   hot %d names: %d on the board panel, %d outside, %d of those under Rp5bn median"
        % (len(hot), len(hot) - len(outside), len(outside), len(thin)))
    log("   top-20 on >=1 session: %d names, %d outside"
        % (sum(1 for r in hot if r["days_top20"]),
           sum(1 for r in outside if r["days_top20"])))
    log("\n   OUTSIDE THE PANEL   days in top-%d (top-20)   median Rp bn   alias row" % a.top_n)
    for r in outside[:40]:
        log("   %-5s %3d (%2d) %10.1f   %s"
            % (r["symbol"], r["days_top"], r["days_top20"], r["median_value"] / 1e9,
               "yes" if r["has_alias"] else "NO"))
    if len(outside) > 40:
        log("   ... and %d more" % (len(outside) - 40))

    if a.dry_run:
        log("\n   --dry-run: nothing written")
        return 0
    PANEL.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    append_history(rep)
    log("\n   wrote %s and one line to %s" % (OUT.name, HISTORY.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
