#!/usr/bin/env python3
"""The four cases, FORWARD ONLY: RSI heading crossed with volume, on the VOLUME HIGH names.

Framework, claims and PASS BARS are pre-registered in `reference/rsi-volume.md`, committed
before the first session of the sample closed. Read it first. The bars are not adjustable and
this file may not be edited to change what a read computes once the sample has begun; the
commit that adds it is named in that document.

    Claim "act"    UU (RSI higher than 20 sessions ago, on more volume than that week) beats
                   the other three cases.
    Claim "avoid"  DU (RSI lower, on more volume) trails the other three cases.

Both are judged at 5 AND at 20 sessions. A claim passes only if both horizons pass.

THE SAMPLE IS A LEDGER, NOT A RECOMPUTATION. `build_daily_report.py --log-cases` appends each
morning's VOLUME HIGH names and their case to data/forward/volume_high_cases.jsonl at 07:00,
first write wins, and run_daily.sh commits it. The case a name was in is whatever that file
says. Nothing here re-derives a label from prices; only the OUTCOME is computed here.

NO PEEKING. Returns are computed on exactly two occasions, each at most once:
    --interim   after 240 logged sessions have a complete 20-session outcome. DESCRIPTIVE:
                it prints the table and cannot pass or fail anything.
    --read      after 450. The deciding read.
Before those counts both refuse. `--status` is always available and computes no return.

    py scripts/four_case_test.py --status
    py scripts/four_case_test.py --selftest
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import types
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import heading_test as H  # noqa: E402  contrast, folds, shift null, sufficient-stat bootstrap
import lift_lib as LL  # noqa: E402
from build_daily_report import BREAK_RATIO, CASES, case_of, log_cases  # noqa: E402
from fetch_prices import WIB, yahoo_chart  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FORWARD = ROOT / "data" / "forward"
INTERIM_OUT = FORWARD / "four_case_interim.json"
RESULT_OUT = FORWARD / "four_case_result.json"

# ------------------------------------------------------------------ declared constants

FORWARD_FROM = "2026-10-09"   # first session that can be in the sample
READ_SESSIONS = 450           # 15 thirty-session blocks: the floor for an inferential band
INTERIM_SESSIONS = 240        # one descriptive look, about a year in
HORIZONS = (5, 20)
BAR_PP = {5: 0.5, 20: 1.0}    # size bar by horizon, percentage points of excess return
CLAIMS = (("act", "UU", +1), ("avoid", "DU", -1))   # (name, cell, claimed sign of cell minus rest)
CELLS = ("UU", "UD", "DU", "DD")
BAR_FOLDS = 3                 # of 4
BAR_CHECK0 = 0.005
N_BOOT, N_NULL = 2000, 200
WORKERS = 6


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ ledger

def load_ledger(path: Path = CASES) -> tuple[list[str], list[dict]]:
    """(logged sessions in order, one row per logged name). Sessions before FORWARD_FROM are
    ignored: anything earlier was written before the rules were."""
    sessions, events = [], []
    if not path.exists():
        return sessions, events
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            s = r.get("session")
            if not s or s < FORWARD_FROM:
                continue
            if r.get("symbol") is None:
                if s not in sessions:
                    sessions.append(s)
            else:
                events.append(r)
    sessions.sort()
    return sessions, events


def complete_sessions(sessions: list[str], dates: list[str], k: int = max(HORIZONS)) -> int:
    """Logged sessions whose k-session outcome already exists: entry is the next close, exit
    k sessions after it, and the exit date must be on the calendar."""
    didx = {d: i for i, d in enumerate(dates)}
    return sum(1 for s in sessions if s in didx and didx[s] + 1 + k <= len(dates) - 1)


def status(sessions: list[str], events: list[dict], dates: list[str]) -> dict:
    didx = {d: i for i, d in enumerate(dates)}
    cal = [d for d in dates if d >= FORWARD_FROM]
    logged = set(sessions)
    last = sessions[-1] if sessions else None
    gaps = [d for d in cal if last and d <= last and d not in logged]
    off_cal = [s for s in sessions if s not in didx]
    done = complete_sessions(sessions, dates)
    by_case = {c: sum(1 for e in events if e.get("case") == c) for c in CELLS}
    left = READ_SESSIONS - done
    proj = None
    if dates:
        # Sessions still to come: the ones not yet logged, plus the 21 it takes the last of
        # them to reach its 20-session exit. 242 sessions a year, about 1.51 days a session.
        ahead = max(left - (len(sessions) - done), 0) + max(HORIZONS) + 1 if left > 0 else 0
        proj = (date.fromisoformat(dates[-1]) + timedelta(days=round(ahead * 365 / 242))).isoformat()
    return {"sessions_logged": len(sessions), "first": sessions[0] if sessions else None, "last": last,
            "gaps": gaps, "off_calendar": off_cal, "complete_k20": done,
            "events": sum(1 for e in events if e.get("list") != "left_out"),
            "by_case": by_case,
            "unclassified": sum(1 for e in events if e.get("list") != "left_out" and e.get("case") is None),
            "left_out": sum(1 for e in events if e.get("list") == "left_out"),
            "names": len({e["symbol"] for e in events}),
            "interim_due": done >= INTERIM_SESSIONS, "read_due": done >= READ_SESSIONS,
            "interim_done": INTERIM_OUT.exists(), "read_done": RESULT_OUT.exists(),
            "projected_read": proj}


def show_status(st: dict) -> None:
    log(f"ledger: {st['sessions_logged']} sessions logged ({st['first']} .. {st['last']}), "
        f"{st['events']} names on {st['names']} distinct tickers")
    log("  by case: " + "   ".join(f"{c} {st['by_case'][c]}" for c in CELLS)
        + f"   no case {st['unclassified']}   left out {st['left_out']}")
    if st["gaps"]:
        log(f"  [!] {len(st['gaps'])} trading session(s) with no ledger line (not backfilled): "
            + ", ".join(st["gaps"][:12]) + (" ..." if len(st["gaps"]) > 12 else ""))
    if st["off_calendar"]:
        log(f"  [!] logged sessions not on the panel calendar: {', '.join(st['off_calendar'][:8])}")
    log(f"  sessions with a complete 20-session outcome: {st['complete_k20']}")
    log(f"  interim look at {INTERIM_SESSIONS}: {'DONE' if st['interim_done'] else ('DUE' if st['interim_due'] else 'not yet')}"
        f"   deciding read at {READ_SESSIONS}: {'DONE' if st['read_done'] else ('DUE' if st['read_due'] else 'not yet')}"
        + ("" if st["read_due"] else f"   (about {st['projected_read'][:7]})"))


# ------------------------------------------------------------------ outcomes

def fetch_px(code: str) -> tuple[str, dict | None]:
    """{date: (close, adjclose)} over five years, or None. Same retry shape as build_market_hot."""
    for attempt in range(5):
        try:
            res = yahoo_chart(code + ".JK", "5y")["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
            out = {}
            for t, c, a in zip(res.get("timestamp") or [], q["close"], adj):
                if c is not None and c > 0:
                    out[datetime.fromtimestamp(t, WIB).strftime("%Y-%m-%d")] = (c, a if a else c)
            return code, out or None
        except urllib.error.HTTPError as e:      # before URLError: HTTPError IS a URLError
            if e.code in (404, 422):
                return code, None
        except (urllib.error.URLError, OSError, KeyError, IndexError, TypeError, ValueError):
            pass
        time.sleep(min(2 ** attempt, 20) + random.random())
    return code, None


def outcomes(events: list[dict], dates: list[str], px: dict, bench: dict) -> list[dict]:
    """Excess return over IHSG for each logged name: in at the close of the session AFTER the
    one it was listed for, out k sessions later. That is what a reader of the 07:00 message
    can do, and it is how check 0 measures the board.

    `px[symbol][date] = (close, adjclose)`, `bench[date] = close`. A name whose raw close
    halves or doubles in one session between the listing and the exit is VOID at that horizon
    and says so; a missing price or benchmark is None and says why. Nothing is dropped
    silently: every row comes back, with `why{k}` set when `x{k}` is not.
    """
    didx = {d: i for i, d in enumerate(dates)}
    out = []
    for e in events:
        r = dict(e)
        s = didx.get(e["session"])
        series = px.get(e["symbol"]) or {}
        for k in HORIZONS:
            r[f"x{k}"], r[f"why{k}"] = None, None
            if s is None:
                r[f"why{k}"] = "session not on the calendar"
                continue
            a, b = s + 1, s + 1 + k
            if b > len(dates) - 1:
                r[f"why{k}"] = "not yet"
                continue
            da, db = dates[a], dates[b]
            if da not in series or db not in series:
                r[f"why{k}"] = "no price"
                continue
            if da not in bench or db not in bench:
                r[f"why{k}"] = "no benchmark"
                continue
            path = [series[d][0] for d in dates[s:b + 1] if d in series]
            if any(y / x >= BREAK_RATIO or x / y >= BREAK_RATIO for x, y in zip(path, path[1:])):
                r[f"why{k}"] = "void: price history broke"
                continue
            r[f"x{k}"] = (series[db][1] / series[da][1] - 1) - (bench[db] / bench[da] - 1)
        r["i"] = s if s is not None else -1
        out.append(r)
    return out


# ------------------------------------------------------------------ evaluation

def leave_one_name_out(rows: list[dict], cell: str, k: int) -> dict:
    """The cell-minus-rest difference with each ticker removed in turn. One name carrying a
    result is the commonest way a pooled contrast lies."""
    tot = [0.0, 0, 0.0, 0]
    per: dict[str, list] = {}
    for r in rows:
        x = r.get(f"x{k}")
        if x is None:
            continue
        j = 0 if r["case"] == cell else 2
        a = per.setdefault(r["symbol"], [0.0, 0, 0.0, 0])
        for acc in (tot, a):
            acc[j] += x
            acc[j + 1] += 1
    vals = {}
    for sym, a in per.items():
        n1, n0 = tot[1] - a[1], tot[3] - a[3]
        if n1 and n0:
            vals[sym] = ((tot[0] - a[0]) / n1 - (tot[2] - a[2]) / n0) * 100.0
    if not vals:
        return {"min_pp": None, "max_pp": None, "worst": None}
    lo, hi = min(vals, key=vals.get), max(vals, key=vals.get)
    return {"min_pp": vals[lo], "min_name": lo, "max_pp": vals[hi], "max_name": hi, "names": len(vals)}


def evaluate(rows: list[dict], dates: list[str], check0_ok: bool | None,
             n_boot: int = N_BOOT, n_null: int = N_NULL) -> dict:
    """Both claims at both horizons, on rows that already carry x5 and x20."""
    p = types.SimpleNamespace(dates=dates)
    use = [r for r in rows if r.get("case") in CELLS and r.get("list") != "left_out"]
    res = {"table": {}, "claims": {}}
    for k in HORIZONS:
        allx = [r[f"x{k}"] for r in use if r.get(f"x{k}") is not None]
        res["table"][k] = {"all": {"n": len(allx), "mean_pp": statistics.fmean(allx) * 100 if allx else None}}
        for c in CELLS:
            xs = [r[f"x{k}"] for r in use if r["case"] == c and r.get(f"x{k}") is not None]
            res["table"][k][c] = {"n": len(xs), "mean_pp": statistics.fmean(xs) * 100 if xs else None,
                                  "median_pp": statistics.median(xs) * 100 if xs else None,
                                  "hit": sum(1 for x in xs if x > 0) / len(xs) if xs else None}
    for name, cell, sign in CLAIMS:
        per_k = {}
        for k in HORIZONS:
            key = lambda r, cell=cell: 1 if r["case"] == cell else 0
            c = H.contrast(p, use, key, k, n_boot, n_null)
            d, b, nl = c["diff_pp"], c.get("bootstrap") or {}, c.get("null") or {}
            lono = leave_one_name_out(use, cell, k)
            band = (b.get("lo") is not None and b["lo"] > 0) if sign > 0 else (b.get("hi") is not None and b["hi"] < 0)
            beyond = (d is not None and nl.get("p95_pp") is not None
                      and (d > nl["p95_pp"] if sign > 0 else d < nl["p05_pp"]))
            folds_ok = sum(1 for f in c["folds"] if f["diff_pp"] is not None and sign * f["diff_pp"] > 0)
            lono_edge = lono["min_pp"] if sign > 0 else lono["max_pp"]
            checks = [
                (f"1 size: {'+' if sign > 0 else '-'}{BAR_PP[k]}pp or more", d is not None and sign * d >= BAR_PP[k]),
                ("1b 10/90 band clear of zero on the claimed side", bool(band)),
                ("2 beyond the shift null's 95th percentile on the claimed side", bool(beyond)),
                (f"3 folds on the claimed side >= {BAR_FOLDS}/4", folds_ok >= BAR_FOLDS),
                ("4 blocks >= 15 in both arms", LL.is_inferential(min(c["blocks_arm1"], c["blocks_arm0"]))),
                ("5 same sign with any one name removed", lono_edge is not None and sign * lono_edge > 0),
                ("6 check 0 in the forward window", bool(check0_ok)),
            ]
            per_k[k] = {"contrast": c, "leave_one_out": lono, "checks": checks,
                        "pass": all(ok for _, ok in checks)}
        n_pass = sum(1 for k in HORIZONS if per_k[k]["pass"])
        verdict = "PASS" if n_pass == len(HORIZONS) else ("PARTIAL" if n_pass else "FAIL")
        if check0_ok is False:
            verdict = "INCONCLUSIVE"
        res["claims"][name] = {"cell": cell, "sign": sign, "horizons": per_k, "verdict": verdict}
    return res


def show(res: dict) -> None:
    for k in HORIZONS:
        t = res["table"][k]
        log(f"\nk={k}: every classified volume-high name {H.fmt(t['all']['mean_pp'])}pp (n={t['all']['n']})")
        for c in CELLS:
            log(f"    {c}  n {t[c]['n']:>5}  mean {H.fmt(t[c]['mean_pp'])}pp  median {H.fmt(t[c]['median_pp'])}pp  "
                f"hit {H.fmt(t[c]['hit'], '.1%')}")
    for name, cl in res["claims"].items():
        log("\n" + "-" * 78)
        log(f"CLAIM '{name}': {cl['cell']} {'beats' if cl['sign'] > 0 else 'trails'} the other three cases")
        for k in HORIZONS:
            h = cl["horizons"][k]
            H.show(f"  k={k}   arm 1 = {cl['cell']}, arm 0 = the other three", h["contrast"])
            lo = h["leave_one_out"]
            log(f"    one name removed: {H.fmt(lo.get('min_pp'), '+.2f')} (without {lo.get('min_name')}) .. "
                f"{H.fmt(lo.get('max_pp'), '+.2f')} (without {lo.get('max_name')})")
            for label, ok in h["checks"]:
                log(f"    [{'PASS' if ok else 'FAIL'}]  {label}")
        log(f"  VERDICT '{name}': {cl['verdict']}")


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    """Hand-built paths and planted effects. No network, no panel, no ledger on disk touched."""
    import tempfile
    fails = []
    rng = random.Random(3)

    # -- case_of: the cut is strict "above then"; equal is D on both axes
    for row, want in (({"rsi": 60, "rsi_then": 40, "vol_x": 2.0}, "UU"), ({"rsi": 60, "rsi_then": 40, "vol_x": 0.5}, "UD"),
                      ({"rsi": 30, "rsi_then": 40, "vol_x": 2.0}, "DU"), ({"rsi": 30, "rsi_then": 40, "vol_x": 0.5}, "DD"),
                      ({"rsi": 40, "rsi_then": 40, "vol_x": 1.0}, "DD"), ({"rsi": 60, "rsi_then": None, "vol_x": 2.0}, None),
                      ({"rsi": 60, "rsi_then": 40, "vol_x": None}, None)):
        if case_of(row) != want:
            fails.append(f"case_of({row}) != {want}")

    # -- ledger: first write wins, a quiet session is still a session
    tmp = Path(tempfile.mkdtemp())
    try:
        led = tmp / "cases.jsonl"
        mk = lambda sym, rsi, then, vx, rv: {"symbol": sym, "rsi": rsi, "rsi_then": then, "vol_x": vx, "rvol5": rv,
                                             "in_pool": True, "close": 100.0, "vh_break": None,
                                             "vh": {"vpct50": 0.95, "adtv20": 50e9, "value": 1e10}}
        rows = [mk("AAAA", 60, 40, 2.0, 2.0), mk("BBBB", 30, 50, 1.5, 1.0), mk("CCCC", 70, 80, 0.5, 3.5),
                dict(mk("DDDD", 60, 40, 2.0, 2.0), vh_break="2026-09-14")]
        n1 = log_cases("2026-10-09", rows, ["AAAA"], path=led)
        n2 = log_cases("2026-10-09", rows[:1], None, path=led)
        n3 = log_cases("2026-10-12", [], None, path=led)
        sess, ev = load_ledger(led)
        got = {e["symbol"]: (e["list"], e["case"]) for e in ev}
        if (n1, n2, n3) != (3, -1, 0):
            fails.append(f"log_cases counts {(n1, n2, n3)} != (3, -1, 0)")
        if sess != ["2026-10-09", "2026-10-12"]:
            fails.append(f"a session with no names must still be logged: {sess}")
        if got != {"AAAA": ("band", "UU"), "BBBB": ("below", "DU"), "CCCC": ("hot", "DD"), "DDDD": ("left_out", None)}:
            fails.append(f"ledger rows {got}")
        if not [e for e in ev if e["symbol"] == "AAAA"][0]["on_board"]:
            fails.append("on_board flag lost")
        log_cases("2026-10-01", rows, None, path=led)
        if "2026-10-01" in load_ledger(led)[0]:
            fails.append("a session before FORWARD_FROM must not enter the sample")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # -- outcomes on a path with a known answer
    dates = [f"2027-01-{d:02d}" for d in range(1, 31)]
    px = {"AAAA": {d: (100.0 + j, 100.0 + j) for j, d in enumerate(dates)},        # +1 a session
          "SPLT": {d: ((1000.0 if j < 6 else 100.0), 100.0) for j, d in enumerate(dates)}}
    bench = {d: 5000.0 for d in dates}
    bench[dates[8]] = 5050.0                                                       # +1% at the k=5 exit
    ev = [{"session": dates[2], "symbol": "AAAA", "case": "UU", "list": "band"},
          {"session": dates[2], "symbol": "SPLT", "case": "DU", "list": "below"},
          {"session": dates[2], "symbol": "NOPE", "case": "DD", "list": "below"},
          {"session": dates[27], "symbol": "AAAA", "case": "UU", "list": "band"}]
    o = outcomes(ev, dates, px, bench)
    want = (108.0 / 103.0 - 1) - 0.01            # in at the close of dates[3], out at dates[8]
    if o[0]["x5"] is None or abs(o[0]["x5"] - want) > 1e-12:
        fails.append(f"x5 {o[0]['x5']} != {want}")
    if abs(o[0]["x20"] - (123.0 / 103.0 - 1)) > 1e-12:
        fails.append(f"x20 {o[0]['x20']}")
    if o[1]["x5"] is not None or "void" not in (o[1]["why5"] or ""):
        fails.append(f"a 10:1 unadjusted drop inside the window must void the row: {o[1]}")
    if o[2]["why5"] != "no price" or o[3]["why5"] != "not yet":
        fails.append(f"missing price / not yet: {o[2]['why5']}, {o[3]['why5']}")
    if complete_sessions([dates[2], dates[27]], dates) != 1 or complete_sessions([dates[8]], dates) != 1 \
            or complete_sessions([dates[9]], dates) != 0:
        fails.append("complete_sessions: the exit date must be on the calendar")

    # -- evaluate: planted effects pass, nothing planted fails, one horizon only is PARTIAL
    cal = [f"d{j:03d}" for j in range(640)]

    def panel(e5, e20):
        rows = []
        for i in range(40, 600):
            for s in range(10):
                case = CELLS[(i * 7 + s * 3 + (i // 11)) % 4]
                rows.append({"symbol": f"S{s:02d}", "i": i, "case": case, "list": "below",
                             "x5": e5.get(case, 0.0) + rng.gauss(0, 0.02),
                             "x20": e20.get(case, 0.0) + rng.gauss(0, 0.04)})
        return rows

    both = evaluate(panel({"UU": 0.015, "DU": -0.015}, {"UU": 0.03, "DU": -0.03}), cal, True, 200, 40)
    none = evaluate(panel({}, {}), cal, True, 200, 40)
    half = evaluate(panel({"UU": 0.015}, {}), cal, True, 200, 40)
    hostile = evaluate(panel({"UU": 0.015, "DU": -0.015}, {"UU": 0.03, "DU": -0.03}), cal, False, 200, 40)
    if (both["claims"]["act"]["verdict"], both["claims"]["avoid"]["verdict"]) != ("PASS", "PASS"):
        fails.append("planted effects must PASS both claims: " + str(
            {n: [(k, [l for l, ok in c["horizons"][k]["checks"] if not ok]) for k in HORIZONS]
             for n, c in both["claims"].items()}))
    if (none["claims"]["act"]["verdict"], none["claims"]["avoid"]["verdict"]) != ("FAIL", "FAIL"):
        fails.append("no planted effect must FAIL both claims")
    if half["claims"]["act"]["verdict"] != "PARTIAL":
        fails.append(f"an effect at 5 sessions only is PARTIAL, got {half['claims']['act']['verdict']}")
    if hostile["claims"]["act"]["verdict"] != "INCONCLUSIVE":
        fails.append("a failed check 0 makes the verdict INCONCLUSIVE, not PASS or FAIL")
    one = [dict(r, x5=(0.5 if r["symbol"] == "S00" and r["case"] == "UU" else rng.gauss(0, 0.02))) for r in panel({}, {})]
    lo = leave_one_name_out(one, "UU", 5)
    if lo["min_name"] != "S00" or lo["min_pp"] > 1.0:
        fails.append(f"leave-one-name-out must find the one name carrying it: {lo}")

    # -- gates, on a calendar of real weekdays from the first forward session
    iso, d = [], date.fromisoformat(FORWARD_FROM)
    while len(iso) < READ_SESSIONS + 30:
        if d.weekday() < 5:
            iso.append(d.isoformat())
        d += timedelta(days=1)
    st = status(iso[:1], [], iso)
    if st["read_due"] or st["interim_due"]:
        fails.append("one session cannot open a read")
    st = status(iso[:INTERIM_SESSIONS], [], iso)
    if not st["interim_due"] or st["read_due"]:
        fails.append(f"{INTERIM_SESSIONS} complete sessions open the interim look and not the read")
    st = status(iso[:READ_SESSIONS + 9], [], iso)          # the last 21 of the calendar cannot be complete
    if not st["read_due"] or st["complete_k20"] != READ_SESSIONS + 9:
        fails.append(f"{READ_SESSIONS} complete sessions must open the read: {st['complete_k20']}")
    st = status(iso[:5] + iso[7:9], [], iso)
    if st["gaps"] != iso[5:7]:
        fails.append(f"a trading day with no ledger line is a gap: {st['gaps']}")

    for f in fails:
        log(f"  FAIL  {f}")
    log(f"selftest: {'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
    return 1 if fails else 0


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="counts and dates only; computes no return")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--interim", action="store_true", help=f"the one descriptive look, after {INTERIM_SESSIONS} sessions")
    ap.add_argument("--read", action="store_true", help=f"the deciding read, after {READ_SESSIONS} sessions")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    from alpha_lib import Panel
    p = Panel()
    sessions, events = load_ledger()
    if not (a.interim or a.read):
        p.load_prices()
        show_status(status(sessions, events, p.dates))
        return 0

    p.load()
    st = status(sessions, events, p.dates)
    show_status(st)
    need, out, done = ((READ_SESSIONS, RESULT_OUT, st["read_done"]) if a.read
                       else (INTERIM_SESSIONS, INTERIM_OUT, st["interim_done"]))
    if done:
        log(f"\nrefusing: {out.name} exists. This look has been taken, and it is taken once.")
        return 3
    if st["complete_k20"] < need:
        log(f"\nrefusing: {st['complete_k20']} of {need} sessions have a 20-session outcome. "
            f"No return is computed before then.")
        return 3

    import trade_backtest as TB
    start = p.didx.get(sessions[0], 0)
    cands = [c for c in TB.build_candidates(p) if c["i"] >= start]
    c0 = TB.check_zero(p, cands, k=5, bar=BAR_CHECK0) if cands else {"ok": False, "reason": "no candidates"}
    log(f"\ncheck 0 in the forward window: {100 * ((c0.get('pooled') or {}).get('lift') or 0):+.2f}pp "
        f"on n={(c0.get('pooled') or {}).get('n')} -> {'PASS' if c0.get('ok') else 'FAIL'}")

    syms = sorted({e["symbol"] for e in events})
    px = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for code, series in ex.map(fetch_px, syms):
            if series:
                px[code] = series
    bench = {p.dates[i]: v for i, v in p.bench.items()}
    rows = outcomes(events, p.dates, px, bench)
    for k in HORIZONS:
        why: dict[str, int] = {}
        for r in rows:
            if r.get(f"x{k}") is None:
                why[r[f"why{k}"]] = why.get(r[f"why{k}"], 0) + 1
        log(f"k={k}: outcomes for {sum(1 for r in rows if r.get(f'x{k}') is not None)} of {len(rows)} logged names"
            + ("; without one: " + ", ".join(f"{w} {n}" for w, n in sorted(why.items())) if why else ""))
        for r in rows:
            if (r.get(f"why{k}") or "").startswith("void"):
                log(f"    void  {r['session']} {r['symbol']}")

    res = evaluate(rows, p.dates, bool(c0.get("ok")))
    if a.interim:
        log("\nINTERIM LOOK -- DESCRIPTIVE. Under 15 blocks no band here is inferential, and nothing below"
            "\ncan pass, fail or ship anything. The verdict lines are printed for the record only.")
    show(res)
    FORWARD.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"kind": "read" if a.read else "interim", "status": st, "check_zero": c0,
                               "result": res}, indent=1, default=float), encoding="utf-8")
    log(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
