#!/usr/bin/env python3
"""name_band — where ONE IDX name sits on the momentum board's RVOL band, as text and a PNG.

The 07:30 report answers "which names printed a volume high" for the pool. This answers the
question the phone asks next: "and what about DSSA?", for any ticker Yahoo knows, inside the panel
or not. It is the on-demand half of the VOLUME HIGH read-out (thesis #16(i),
research/idx-volume-momentum): a READ-OUT, never a rule, and it never touches the board.

What it reuses, and why nothing is copied: the Yahoo fetch + day cache, the Shim panel shape,
`volume_high`, `trajectory`/`direction`, `closed` and the liquidity floors come from
build_daily_report; rsi/dd60/rvol5 from overlay_test.features; the verdict from
momentum_setup.is_momentum; the "misses by" list from momentum_why.leg2; the band constants from
build_momentum_board. A number here is the number the morning report would print.

One thing is deliberately NOT reused: build_yahoo_panel's breadth calendar. With a single symbol
its 60% rule admits every date the symbol has a bar on, including Yahoo's phantom holiday bars
(zero volume), and one of those nulls `volume_high` for the next fifty sessions. `single_panel`
builds the calendar from the symbol's own traded sessions instead, cross-checked against the
day cache when it is warm.

    py scripts/name_band.py DSSA                       # text to stdout
    py scripts/name_band.py DSSA --png build/band/DSSA.png
    py scripts/name_band.py DSSA --date 2026-08-26 --n 10 --sessions 30
    py scripts/name_band.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import statistics
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_daily_report as R  # noqa: E402  fetch, Shim, volume_high, trajectory, direction, closed, floors
import build_momentum_board as B  # noqa: E402  the live gate constants, never a copy
from fetch_prices import WIB, yahoo_chart  # noqa: E402
from momentum_setup import is_momentum  # noqa: E402
from momentum_why import leg2  # noqa: E402
from overlay_test import features  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "data" / "panel"
OUT_DIR = ROOT / "build" / "band"
BUDGET = 3800                      # notify_telegram.CHUNK; one message, no chunking needed
FEATURE_MIN = 60                   # overlay_test.features needs 60 contiguous sessions
HIST_MIN = R.VOL_HIST + 1          # volume_high needs 50 prior sessions
STATS = ("in band +2.6pp/5d +6.8pp/20d (hit 50-54%); below band ~market; "
         "RVOL>=3 hit 42%")
UTC = dt.timezone.utc


def _log_noop(*_a):
    pass


# --------------------------------------------------------------------------- data

def classify_http(code: int, sym: str) -> str:
    if code == 404:
        return f"{sym} is not an IDX ticker on Yahoo (no {sym}.JK)."
    return f"Yahoo returned HTTP {code} for {sym}.JK - try again shortly."


def parse_rows(payload: dict) -> tuple[dict, dict]:
    """{date: (o, h, l, c, v, adj, ts)}, meta. Mirrors build_daily_report.build_yahoo_panel
    (:172-186): rows missing any of OHLC are dropped; adjclose falls back to close."""
    res = (payload.get("chart") or {}).get("result") or []
    if not res:
        return {}, {}
    res = res[0]
    ts, q = res.get("timestamp") or [], res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    rows = {}
    for i, t in enumerate(ts):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c):
            continue
        day = dt.datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d")
        rows[day] = (o, h, l, c, v or 0, adj[i] if adj[i] is not None else c, t)
    return rows, res.get("meta") or {}


def _today(now: dt.datetime) -> str:
    return now.date().isoformat()


def _pm_path(sym: str, now: dt.datetime) -> Path:
    return R.CACHE / _today(now) / (sym + ".pm.json")


def stale_after_close(rows: dict, now: dt.datetime) -> bool:
    """The 07:30 cache predates the session; after 16:15 on a weekday it lacks today's bar."""
    if not rows or now.weekday() >= 5 or (now.hour, now.minute) < (16, 15):
        return False
    return max(rows) < _today(now)


def load_bars(sym: str, log, now: dt.datetime, use_cache: bool = True) -> tuple[dict | None, str | None]:
    """(payload, error). Warm cache = disk read; cold = one 2y call via build_daily_report.fetch."""
    if not use_cache:
        try:
            return yahoo_chart(sym + ".JK", "2y"), None
        except urllib.error.HTTPError as e:
            return None, classify_http(e.code, sym)
        except (urllib.error.URLError, OSError) as e:
            return None, f"Yahoo unreachable ({type(e).__name__}) - try again in a minute."

    pm = _pm_path(sym, now)
    if pm.exists() and pm.stat().st_size > 200:
        try:
            return json.loads(pm.read_text(encoding="utf-8")), None
        except ValueError:
            pass
    d, err = _fetch_cached(sym, log, now)
    if err:
        return None, err
    if not ((d.get("chart") or {}).get("result")):
        return None, f"Yahoo returned no bars for {sym}.JK."
    rows, _ = parse_rows(d)
    if stale_after_close(rows, now):
        try:
            fresh = yahoo_chart(sym + ".JK", "2y")
            pm.parent.mkdir(parents=True, exist_ok=True)
            pm.write_text(json.dumps(fresh), encoding="utf-8")
            log(f"   post-close refresh for {sym} (cache ended {max(rows)})")
            return fresh, None
        except Exception as e:                                   # noqa: BLE001
            log(f"   post-close refresh failed ({e!r}) - using the morning cache")
    return d, None


def _fetch_cached(sym: str, log, now: dt.datetime) -> tuple[dict | None, str | None]:
    """The day cache build_daily_report.fetch keeps (same path, same file), with one difference:
    an HTTP error is classified at once instead of being retried as transient. `fetch` lists
    URLError before HTTPError in its except clauses, and HTTPError is a URLError, so a 404 on a
    typo costs it three retries and 12s; a phone request should hear "not a ticker" in 0.2s."""
    import time as _time
    f = R.CACHE / _today(now) / (sym + ".json")
    if f.exists() and f.stat().st_size > 200:
        try:
            return json.loads(f.read_text(encoding="utf-8")), None
        except ValueError:
            pass
    delay = 2.0
    for attempt in range(1, 4):
        try:
            d = yahoo_chart(sym + ".JK", "2y")
        except urllib.error.HTTPError as e:
            return None, classify_http(e.code, sym)
        except (urllib.error.URLError, OSError) as e:
            log(f"   {sym} attempt {attempt}: {type(e).__name__} - retrying")
            if attempt == 3:
                return None, f"Yahoo unreachable ({type(e).__name__}) - try again in a minute."
            _time.sleep(delay * attempt)
            continue
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(d), encoding="utf-8")
        except OSError as e:
            log(f"   cache write failed ({e!r}) - continuing without it")
        return d, None
    return None, "Yahoo unreachable - try again in a minute."


def cache_calendar(now: dt.datetime, min_files: int = 20) -> set | None:
    """Trading dates carried by >= BREADTH of today's cached names — the report's own rule
    (build_daily_report.py:195-196) with a real denominator. None when the cache is cold."""
    d = R.CACHE / _today(now)
    files = [f for f in d.glob("*.json") if not f.name.endswith(".pm.json")] if d.exists() else []
    if len(files) < min_files:
        return None
    cov, n = {}, 0
    for f in files:
        try:
            rows, _ = parse_rows(json.loads(f.read_text(encoding="utf-8")))
        except (ValueError, OSError, KeyError, IndexError, TypeError):
            continue
        n += 1
        for day, r in rows.items():
            if r[4] > 0:
                cov[day] = cov.get(day, 0) + 1
    if n < min_files:
        return None
    return {day for day, c in cov.items() if c >= R.BREADTH * n}


def single_panel(sym: str, rows: dict, cache_dates: set | None = None):
    """A Shim over this symbol's OWN traded sessions. Returns (shim, dropped counts)."""
    dropped = {"weekend": 0, "zero_volume": 0, "off_calendar": 0}
    cal_max = max(cache_dates) if cache_dates else None
    keep = []
    for day in sorted(rows):
        o, h, l, c, v, a, t = rows[day]
        if dt.date.fromisoformat(day).weekday() >= 5:
            dropped["weekend"] += 1
            continue
        if not v or v <= 0:
            dropped["zero_volume"] += 1
            continue
        if cache_dates and day not in cache_dates and day < cal_max:
            dropped["off_calendar"] += 1
            continue
        keep.append(day)
    p = R.Shim()
    p.dates = keep
    p.didx = {d: i for i, d in enumerate(keep)}
    p.close, p.raw_close, p.high, p.low, p.volume, p.open = {sym: {}}, {sym: {}}, {sym: {}}, {sym: {}}, {sym: {}}, {sym: {}}
    p.ts = {}
    for i, day in enumerate(keep):
        o, h, l, c, v, a, t = rows[day]
        p.open[sym][i], p.high[sym][i], p.low[sym][i] = o, h, l
        p.raw_close[sym][i], p.volume[sym][i], p.close[sym][i] = c, v, a
        p.ts[i] = t
    return p, dropped


def pick(p, now: dt.datetime, requested: str | None = None) -> tuple[int, int | None]:
    """(session index, partial index). The verdict is always on a CLOSED session."""
    if requested:
        i = p.didx.get(requested)
        if i is None:
            raise ValueError(f"no bar on {requested}")
        if not R.closed(requested, now):
            raise ValueError(f"{requested} has not closed yet")
        return i, None
    last = len(p.dates) - 1
    if last < 0:
        raise ValueError("no sessions")
    if R.closed(p.dates[last], now):
        return last, None
    if last == 0:
        raise ValueError("only a forming bar is available")
    return last - 1, last


def market_open(now: dt.datetime) -> bool:
    return now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) < (16, 15)


def partial_quote(sym: str, log) -> dict | None:
    """Today's forming bar when the day cache predates the open: one 1d call, meta fields."""
    try:
        d = yahoo_chart(sym + ".JK", "1d")
    except Exception as e:                                       # noqa: BLE001
        log(f"   partial quote failed ({e!r})")
        return None
    rows, meta = parse_rows(d)
    price, vol, t = meta.get("regularMarketPrice"), meta.get("regularMarketVolume"), meta.get("regularMarketTime")
    if price is None or vol is None:
        return None
    day = dt.datetime.fromtimestamp(t, WIB) if t else None
    hi = lo = None
    if rows:
        r = rows[max(rows)]
        hi, lo = r[1], r[2]
    return {"price": float(price), "volume": float(vol), "high": hi, "low": lo, "time": day}


# --------------------------------------------------------------------------- features

def rvol_simple(p, sym: str, i: int) -> float | None:
    """The board's rvol5 arithmetic (overlay_test.py:74-76) for names too young for features()."""
    vo = p.volume[sym]
    if i < 19:
        return None
    v5 = [vo[j] for j in range(i - 4, i + 1)]
    v20 = [vo[j] for j in range(i - 19, i + 1)]
    return (sum(v5) / 5) / (sum(v20) / 20) if sum(v20) else None


def need_volume(s4: float, s19: float, r: float) -> float:
    """Volume the NEXT session needs for rvol5 to reach r, exactly.

    rvol5 = mean(v[i-4..i]) / mean(v[i-19..i]); with next volume x and the two windows rolling,
    (S4 + x)/5 >= r (S19 + x)/20  <=>  x >= (5 r S19 - 20 S4) / (20 - 5 r), valid for r < 4.
    """
    if r >= 4:
        return math.inf
    return max(0.0, (5 * r * s19 - 20 * s4) / (20 - 5 * r))


def cell_of(rvol: float | None) -> str:
    if rvol is None:
        return "no reading"
    if rvol >= B.EXHAUST_RVOL:
        return "HOT"
    if rvol >= B.RVOL_MIN:
        return "IN BAND"
    return "BELOW"


def score(p, sym: str, i: int) -> dict:
    n_hist = i + 1
    f = features(p, sym, i) if n_hist >= FEATURE_MIN else None
    if f is not None and f.get("rvol5") is None:
        f = None
    vh = R.volume_high(p, sym, i)
    traj = R.trajectory(p, sym, i) if n_hist >= FEATURE_MIN else []
    label = R.direction(traj) if traj else "?"
    rvol = f["rvol5"] if f else rvol_simple(p, sym, i)
    out = {"f": f, "vh": vh, "traj": traj, "label": label, "rvol5": rvol, "cell": cell_of(rvol),
           "n_hist": n_hist, "pass2": None, "fails": [], "status": "n/a",
           "median_value": R.median_value(p, sym, i)}
    if f:
        out["pass2"] = is_momentum(f, B.RVOL_MIN, B.DD_MIN, B.RSI_MIN, B.RVOL_MAX)
        _ok, fails, status = leg2(f)
        out["fails"], out["status"] = fails, status
    out["vol_high_today"] = bool(vh and vh["vpct50"] >= R.VOL_TOP and vh["adtv20"] >= R.VOL_MIN_ADTV_IDR)
    return out


def arithmetic(p, sym: str, i: int) -> dict | None:
    vo = p.volume[sym]
    if i < 19:
        return None
    s4 = sum(vo[j] for j in range(i - 3, i + 1))
    s19 = sum(vo[j] for j in range(i - 18, i + 1))
    m20 = sum(vo[j] for j in range(i - 19, i + 1)) / 20
    last = vo[i]
    px = p.raw_close[sym][i]
    out = {"m20": m20, "last": last, "px": px, "drop5": (p.dates[i - 4], vo[i - 4]),
           "drop20": (p.dates[i - 19], vo[i - 19])}
    for key, r in (("to_min", B.RVOL_MIN), ("to_hot", B.EXHAUST_RVOL)):
        x = need_volume(s4, s19, r)
        out[key] = {"r": r, "shares": x, "mult": (x / last) if last else None, "idr": x * px}
    return out


def sessions_table(p, sym: str, i: int, n: int = 10) -> list[dict]:
    rows = []
    cl, rc, vo = p.close[sym], p.raw_close[sym], p.volume[sym]
    for j in range(max(0, i - n + 1), i + 1):
        f = features(p, sym, j) if j + 1 >= FEATURE_MIN else None
        vh = R.volume_high(p, sym, j)
        d1 = (cl[j] / cl[j - 1] - 1) if j >= 1 and cl[j - 1] else None
        raw1 = (rc[j] / rc[j - 1] - 1) if j >= 1 and rc[j - 1] else None
        adj_flag = d1 is not None and raw1 is not None and abs(d1 - raw1) > 0.02
        rows.append({"date": p.dates[j], "close": rc[j], "d1": d1, "volume": vo[j],
                     "value": vo[j] * rc[j], "vp": vh["vpct50"] if vh else None,
                     "rvol5": (f["rvol5"] if f else rvol_simple(p, sym, j)), "adj": adj_flag})
    return rows


def high_sessions(p, sym: str, i: int, lookback: int = 30) -> list[dict]:
    out = []
    cl = p.close[sym]
    for j in range(max(0, i - lookback + 1), i + 1):
        vh = R.volume_high(p, sym, j)
        if not vh or vh["vpct50"] < R.VOL_TOP:
            continue
        f = features(p, sym, j) if j + 1 >= FEATURE_MIN else None
        rv = f["rvol5"] if f else rvol_simple(p, sym, j)
        out.append({"date": p.dates[j], "vp": vh["vpct50"], "d1": (cl[j] / cl[j - 1] - 1) if j else None,
                    "volume": p.volume[sym][j], "rvol5": rv, "cell": cell_of(rv)})
    return out


def membership(sym: str, session: str) -> dict:
    out = {"board": None, "board_session": None, "in_panel": None, "n_panel": None, "extra": sym in R.EXTRA_WATCH}
    if R.BOARD.exists():
        try:
            b = json.loads(R.BOARD.read_text(encoding="utf-8"))
            out["board_session"] = b.get("session")
            if b.get("session") == session:
                out["board"] = sym in {c.get("symbol") for c in (b.get("candidates") or [])}
        except ValueError:
            pass
    try:
        from alpha_lib import Panel
        rp = Panel()
        rp.load_prices()
        if rp.close:
            out["in_panel"], out["n_panel"] = sym in rp.close, len(rp.close)
    except Exception:                                            # noqa: BLE001
        pass
    return out


def corporate_actions(sym: str, first: str, last: str) -> list | None:
    f = PANEL / "corporate_actions.json"
    if not f.exists():
        return None
    try:
        ca = json.loads(f.read_text(encoding="utf-8")).get(sym)
    except ValueError:
        return None
    if not ca:
        return None
    out = []
    for kind, lst in (ca.get("corporate_actions") or {}).items():
        if kind not in ("stock_split", "right_issue", "bonus", "dividend") or not isinstance(lst, list):
            continue
        for it in lst:
            d = str(it.get("ex_date") or it.get("date") or it.get("payment_date") or "")[:10]
            if d and d >= first:
                out.append({"date": d, "kind": kind, "upcoming": d > last,
                            "detail": {k: v for k, v in it.items() if k in
                                       ("split_ratio", "old_ratio", "new_ratio", "price", "dividend_amount")}})
    out.sort(key=lambda x: x["date"])
    return out


# --------------------------------------------------------------------------- build

def build(sym: str, *, n: int = 10, sessions: int = 30, date: str | None = None,
          now: dt.datetime | None = None, use_cache: bool = True, log=_log_noop) -> dict:
    """Everything both renderers need, or {'error': text}."""
    now = now or dt.datetime.now(WIB)
    payload, err = load_bars(sym, log, now, use_cache)
    if err:
        return {"sym": sym, "error": err}
    rows, meta = parse_rows(payload)
    p, dropped = single_panel(sym, rows, cache_calendar(now) if use_cache else None)
    if len(p.dates) < 2:
        return {"sym": sym, "error": f"{sym}.JK has no traded sessions on Yahoo."}
    try:
        i, partial_i = pick(p, now, date)
    except ValueError as e:
        return {"sym": sym, "error": f"{sym}: {e}."}
    session = p.dates[i]
    sc = score(p, sym, i)
    partial = None
    if partial_i is not None:
        j = partial_i
        partial = {"price": p.raw_close[sym][j], "volume": p.volume[sym][j], "high": p.high[sym][j],
                   "low": p.low[sym][j], "time": dt.datetime.fromtimestamp(p.ts[j], WIB), "source": "bar"}
    elif market_open(now) and not date and session < _today(now):
        q = partial_quote(sym, log)
        if q:
            q["source"] = "quote"
            partial = q
    if partial:
        vo = p.volume[sym]
        pv = partial["volume"]
        if i >= 19:
            s4 = sum(vo[j] for j in range(i - 3, i + 1))
            s19 = sum(vo[j] for j in range(i - 18, i + 1))
            partial["rvol5_now"] = ((s4 + pv) / 5) / ((s19 + pv) / 20) if (s19 + pv) else None
            partial["x_m20"] = pv / (sum(vo[j] for j in range(i - 19, i + 1)) / 20)
        if i >= R.VOL_HIST - 1:
            hist = [vo[j] for j in range(i - R.VOL_HIST + 1, i + 1)]
            partial["vp_now"] = sum(1 for h in hist if h < pv) / R.VOL_HIST
        partial["d1"] = partial["price"] / p.raw_close[sym][i] - 1 if p.raw_close[sym][i] else None

    cl, rc, vo = p.close[sym], p.raw_close[sym], p.volume[sym]
    first_ca = p.dates[max(0, i - 60)]
    ctx = {
        "sym": sym, "name": meta.get("longName") or meta.get("shortName") or "",
        "session": session, "i": i, "now": now,
        "close": rc[i], "d1": (cl[i] / cl[i - 1] - 1) if i >= 1 and cl[i - 1] else None,
        "volume": vo[i], "value": vo[i] * rc[i],
        "score": sc, "arith": arithmetic(p, sym, i), "partial": partial,
        "table": sessions_table(p, sym, i, n), "highs": high_sessions(p, sym, i, 30),
        "membership": membership(sym, session), "ca": corporate_actions(sym, first_ca, session),
        "dropped": dropped, "n_sessions": len(p.dates),
        "chart": chart_rows(p, sym, i, sessions, partial_i),
    }
    return ctx


def chart_rows(p, sym: str, i: int, sessions: int, partial_i: int | None) -> list[dict]:
    out = []
    vo, rc = p.volume[sym], p.raw_close[sym]
    for j in range(max(0, i - sessions + 1), i + 1):
        f = features(p, sym, j) if j + 1 >= FEATURE_MIN else None
        vh = R.volume_high(p, sym, j)
        m20 = (sum(vo[k] for k in range(j - 19, j + 1)) / 20) if j >= 19 else None
        out.append({"date": p.dates[j], "close": rc[j], "high": p.high[sym][j], "low": p.low[sym][j],
                    "volume": vo[j], "rvol5": f["rvol5"] if f else rvol_simple(p, sym, j),
                    "vp": vh["vpct50"] if vh else None, "m20": m20, "partial": False})
    if partial_i is not None:
        j = partial_i
        out.append({"date": p.dates[j], "close": rc[j], "high": p.high[sym][j], "low": p.low[sym][j],
                    "volume": vo[j], "rvol5": None, "vp": None, "m20": None, "partial": True})
    return out


# --------------------------------------------------------------------------- text

def _pct(x, digits=1):
    return "-" if x is None else f"{100 * x:+.{digits}f}%"


def _m(x):
    return "-" if x is None else f"{x / 1e6:,.0f}m"


def _bn(x):
    return "-" if x is None else f"Rp{x / 1e9:,.0f}bn"


def render_blocks(ctx: dict) -> list[tuple[int, list[str]]]:
    """(priority, lines) — lower priority is trimmed first by fit()."""
    sym, sc, mem = ctx["sym"], ctx["score"], ctx["membership"]
    f, vh, ar = sc["f"], sc["vh"], ctx["arith"]
    blocks = []

    head = [f"{sym}" + (f" - {ctx['name']}" if ctx["name"] else ""),
            f"session {ctx['session']} (closed) | close {R.fmt_px(ctx['close'])} d1 {_pct(ctx['d1'])} | "
            f"vol {_m(ctx['volume'])} {_bn(ctx['value'])}"]
    blocks.append((100, head))

    v = []
    rv = sc["rvol5"]
    cell = sc["cell"]
    cell_txt = {"IN BAND": f"IN THE BAND ({B.RVOL_MIN:.1f}-{B.RVOL_MAX:.1f})",
                "BELOW": f"BELOW THE BAND (needs {B.RVOL_MIN:.1f})",
                "HOT": f"HOT - RVOL >= {B.EXHAUST_RVOL:.1f}, the edge inverts here",
                "no reading": "NO BAND READING"}[cell]
    v.append(f"{cell_txt} - RVOL5 {rv:.2f}" if rv is not None else cell_txt
             + f" (rvol5 needs 20 sessions, has {sc['n_hist']})")
    if f is None and sc["n_hist"] < FEATURE_MIN:
        v[-1] += f" | young listing: {sc['n_hist']} sessions, board reading needs {FEATURE_MIN}"
    if vh:
        v.append(f"vp {vh['vpct50']:.2f} (share of the last {R.VOL_HIST} sessions below today's volume) | "
                 f"x{vh['vgrow']:.1f} vs prior 5-day mean | direction: {sc['label']}")
    else:
        v.append(f"vp - (needs {R.VOL_HIST} prior sessions, has {sc['n_hist'] - 1}) | direction: {sc['label']}")
    if sc["traj"]:
        v.append("rvol5 last %d: %s -> %s" % (R.TRAJ_SESSIONS,
                 " ".join("-" if x is None else f"{x:.2f}" for x in sc["traj"]), sc["label"]))
    if vh:
        floor_ok = vh["adtv20"] >= R.VOL_MIN_ADTV_IDR
        v.append("volume high today: %s (vp %.2f vs %.2f; ADTV20 %s vs floor %s%s)"
                 % ("YES" if sc["vol_high_today"] else "NO", vh["vpct50"], R.VOL_TOP,
                    _bn(vh["adtv20"]), _bn(R.VOL_MIN_ADTV_IDR), "" if floor_ok else " - below the floor"))
    if f:
        parts = []
        for name, val, thr, gap in sc["fails"]:
            parts.append(f"{name} {val:.2f} misses {thr:.2f} by {gap:.2f}")
        oks = [name for name in ("rvol5", "dd60", "rsi") if name not in {x[0] for x in sc["fails"]}]
        okt = " ".join(f"{k} {f[k]:.2f} ok" if k != "rsi" else f"rsi {f['rsi']:.0f} ok" for k in oks)
        v.append(f"leg 2 (price gate): {sc['status']}" + (" -> " + "; ".join(parts) if parts else "")
                 + (f" | {okt}" if okt else ""))
    else:
        v.append(f"leg 2 (price gate): n/a until {FEATURE_MIN} sessions (rsi/dd60 need contiguous history)")
    if mem["board"] is None:
        bt = (f"board snapshot is {mem['board_session']}, membership for {ctx['session']} unknown"
              if mem["board_session"] else "board snapshot unavailable")
    else:
        bt = f"board {ctx['session']}: {'CANDIDATE' if mem['board'] else 'not a candidate'}"
    if mem["in_panel"] is None:
        pt = "panel unavailable on this machine"
    elif mem["in_panel"]:
        pt = f"in the {mem['n_panel']}-name panel"
    else:
        pt = "outside the panel" + (" (extra watch list)" if mem["extra"] else "") + " - price-only, no broker leg"
    v.append(f"{bt} | {pt}")
    if sc["median_value"] is not None:
        v.append(f"median value20 {_bn(sc['median_value'])} (report floor {_bn(R.MIN_VALUE_IDR)})")
    blocks.append((95, [""] + v))

    if ar:
        a = []
        a.append("NEXT SESSION (20-day mean %s; the 5-day window drops %s's %s)"
                 % (_m(ar["m20"]), ar["drop5"][0][5:], _m(ar["drop5"][1])))
        for key, verb in (("to_min", f"to reach RVOL {B.RVOL_MIN:.1f}" if cell != "IN BAND" and cell != "HOT"
                           else f"to stay >= {B.RVOL_MIN:.1f}"), ("to_hot", f"to hit {B.EXHAUST_RVOL:.1f} (exhaustion)")):
            x = ar[key]
            if x["shares"] == 0:
                a.append(f"  {verb}: already there on any volume")
            elif math.isinf(x["shares"]):
                a.append(f"  {verb}: unreachable in one session")
            else:
                a.append(f"  {verb}: >= {_m(x['shares'])} shares"
                         + (f" ({x['mult']:.1f}x last session, ~{_bn(x['idr'])})" if x["mult"] else ""))
        blocks.append((90, [""] + a))

    if ctx["partial"]:
        q = ctx["partial"]
        t = q["time"].strftime("%H:%M WIB") if q.get("time") else "now"
        line = (f"TODAY SO FAR (partial, {t}): {R.fmt_px(q['price'])} {_pct(q.get('d1'))} | vol {_m(q['volume'])}"
                + (f" = {q['x_m20']:.2f}x m20" if q.get("x_m20") is not None else "")
                + (f" | rvol5 if it closed now {q['rvol5_now']:.2f}" if q.get("rvol5_now") is not None else "")
                + (f" | vp {q['vp_now']:.2f}" if q.get("vp_now") is not None else ""))
        blocks.append((85, ["", line]))

    tb = [f"LAST {len(ctx['table'])} SESSIONS   date  close  d1  vol(m)  vp  rvol5"]
    for r in ctx["table"]:
        tb.append("%s %-5s %7s %7s %6s %5s %5s%s"
                  % ("*" if (r["vp"] is not None and r["vp"] >= R.VOL_TOP) else " ",
                     r["date"][5:], R.fmt_px(r["close"]), _pct(r["d1"]), f"{r['volume'] / 1e6:,.0f}",
                     "-" if r["vp"] is None else f"{r['vp']:.2f}",
                     "-" if r["rvol5"] is None else f"{r['rvol5']:.2f}", " a" if r["adj"] else ""))
    tb.append(f"* = {R.VOL_HIST}-day volume high (vp >= {R.VOL_TOP:.2f}), a = adjustment day")
    blocks.append((80, [""] + tb))

    hs = [f"VOLUME HIGHS, LAST 30 SESSIONS ({len(ctx['highs'])})"]
    for h in ctx["highs"][-8:]:
        hs.append("  %s vp %.2f %s %s rvol5 %s %s"
                  % (h["date"][5:], h["vp"], _pct(h["d1"]), _m(h["volume"]),
                     "-" if h["rvol5"] is None else f"{h['rvol5']:.2f}", h["cell"].lower()))
    if not ctx["highs"]:
        hs.append("  none")
    blocks.append((70, [""] + hs))

    ca = ctx["ca"]
    if ca is None:
        cl = ["corporate actions: unknown (not a panel name)"]
    elif not ca:
        cl = ["corporate actions (60 sessions): none known"]
    else:
        cl = ["corporate actions (60 sessions):"]
        for c in ca:
            det = " ".join(f"{k} {v}" for k, v in c["detail"].items())
            cl.append(f"  {c['date']} {c['kind']}{' (upcoming)' if c['upcoming'] else ''} {det}".rstrip())
    blocks.append((60, [""] + cl))

    foot = ["", f"Read-out, not a rule: {STATS}.", "Price-only from Yahoo; no broker leg."]
    if ctx["dropped"]["zero_volume"] or ctx["dropped"]["off_calendar"]:
        foot.append("bars dropped: %d zero-volume, %d off the market calendar"
                    % (ctx["dropped"]["zero_volume"], ctx["dropped"]["off_calendar"]))
    foot.append(R.SITE)
    blocks.append((99, foot))          # last in order, never trimmed: the caveat travels with the numbers
    return blocks


def fit(blocks: list[tuple[int, list[str]]], budget: int = BUDGET) -> str:
    """Join the blocks; if over budget drop the lowest-priority block (never the header or the
    footer's first line), then shorten the table. Never cuts mid-line."""
    def join(bl):
        return "\n".join(l for _, lines in bl for l in lines)
    bl = list(blocks)                  # display order is the list order; priority only decides trimming
    text = join(bl)
    while len(text) > budget:
        droppable = [b for b in bl if b[0] < 80]
        if droppable:
            bl.remove(min(droppable, key=lambda b: b[0]))
        else:
            tb = next((b for b in bl if b[0] == 80), None)
            if tb and len(tb[1]) > 5:
                tb[1].pop(2)        # oldest table row
            else:
                break
        text = join(bl)
    assert len(text) <= budget, len(text)
    return text


def render_text(ctx: dict, budget: int = BUDGET) -> str:
    if ctx.get("error"):
        return ctx["error"]
    return fit(render_blocks(ctx), budget)


# --------------------------------------------------------------------------- png

COL = {"ground": "#fbfaf7", "surface": "#ffffff", "line": "#e3e1da", "grid": "#edebe5",
       "ink": "#14161a", "ink2": "#5b5f66", "ink3": "#8a8e95",
       "price": "#2a78d6", "whisker": "#b3cdee", "rvol": "#1baf7a", "band": "#e5f5ee",
       "bandedge": "#8fd5ba", "hot": "#e34948", "vol": "#a9b1bf", "volhigh": "#eb6834",
       "mean": "#5b5f66"}


def _font(kind: str, px: int):
    from PIL import ImageFont
    cands = {
        "sans": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf", "arial.ttf"],
        "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf", "arialbd.ttf"],
        "mono": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf", "consola.ttf"],
    }[kind]
    for c in cands:
        try:
            return ImageFont.truetype(c, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        return ImageFont.load_default()


def nice(lo: float, hi: float, steps: int) -> tuple[float, float, float]:
    span = max(hi - lo, 1e-9)
    raw = span / steps
    p = 10 ** math.floor(math.log10(raw))
    m = raw / p
    step = (1 if m <= 1 else 2 if m <= 2 else 2.5 if m <= 2.5 else 5 if m <= 5 else 10) * p
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def _dashed(draw, p0, p1, on, off, fill, width):
    (x0, y0), (x1, y1) = p0, p1
    length = math.hypot(x1 - x0, y1 - y0)
    if length == 0:
        return
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    pos = 0.0
    while pos < length:
        a = pos
        b = min(pos + on, length)
        draw.line([(x0 + ux * a, y0 + uy * a), (x0 + ux * b, y0 + uy * b)], fill=fill, width=width)
        pos += on + off


def render_png(ctx: dict, path=None, size=(1200, 800), scale: int = 2):
    """Three panels on one session axis. Returns PNG bytes (and writes `path` if given)."""
    from PIL import Image, ImageDraw
    S = scale
    W, H = size[0] * S, size[1] * S
    rows = ctx["chart"]
    n = max(1, len(rows))
    img = Image.new("RGB", (W, H), COL["ground"])
    d = ImageDraw.Draw(img)
    f_title, f_stat, f_lab, f_tick, f_val = (_font("bold", 17 * S), _font("mono", 12 * S), _font("sans", 11 * S),
                                            _font("mono", 10 * S), _font("mono", 10 * S))
    L, Rm = 90 * S, 30 * S
    panels = [{"top": 92 * S, "h": 236 * S, "label": "CLOSE (Rp), with the day's range"},
              {"top": 372 * S, "h": 176 * S, "label": f"RVOL5 = mean vol 5 / mean vol 20   (band {B.RVOL_MIN:.1f}-{B.RVOL_MAX:.1f})"},
              {"top": 592 * S, "h": 136 * S, "label": "VOLUME, m shares   (orange = 50-day high, dashed = 20-day mean)"}]
    sc_ = ctx["score"]

    # header
    d.text((L, 22 * S), f"{ctx['sym']}  on the band", fill=COL["ink"], font=f_title, anchor="ls")
    sub = f"{ctx['name']}" if ctx.get("name") else ""
    if sub:
        d.text((L, 42 * S), sub[:60], fill=COL["ink2"], font=f_lab, anchor="ls")
    rv = sc_["rvol5"]
    stat = (f"session {ctx['session']}  ·  close {R.fmt_px(ctx['close'])}  ·  RVOL5 "
            + (f"{rv:.2f}" if rv is not None else "-") + "  ·  "
            + (f"vp {sc_['vh']['vpct50']:.2f}" if sc_["vh"] else "vp -") + f"  ·  {sc_['cell']}")
    d.text((W - Rm, 22 * S), stat, fill=COL["ink"], font=f_stat, anchor="rs")
    d.text((W - Rm, 42 * S), f"direction: {sc_['label']}", fill=COL["ink2"], font=f_lab, anchor="rs")
    # legend
    lx = L
    for txt, col, kind in (("close", COL["price"], "line"), ("RVOL5", COL["rvol"], "line"),
                           ("band", COL["band"], "rect"), ("volume", COL["vol"], "rect"),
                           ("50-day high", COL["volhigh"], "rect"), ("20-day mean", COL["mean"], "dash")):
        y = 66 * S
        if kind == "line":
            d.line([(lx, y), (lx + 18 * S, y)], fill=col, width=2 * S)
        elif kind == "dash":
            _dashed(d, (lx, y), (lx + 18 * S, y), 4 * S, 3 * S, col, 2 * S)
        else:
            d.rectangle([lx, y - 5 * S, lx + 18 * S, y + 5 * S], fill=col,
                        outline=COL["bandedge"] if txt == "band" else None)
        d.text((lx + 24 * S, y + 4 * S), txt, fill=COL["ink2"], font=f_lab, anchor="ls")
        lx += 24 * S + int(d.textlength(txt, font=f_lab)) + 22 * S

    def x(i):
        return L + (i + 0.5) * (W - L - Rm) / n
    bw = max(4 * S, (W - L - Rm) / n - 3 * S)

    def frame(pn, lo, hi, ticks, fmt):
        def y(v):
            return pn["top"] + pn["h"] - (v - lo) / (hi - lo) * pn["h"]
        d.rectangle([L, pn["top"], W - Rm, pn["top"] + pn["h"]], fill=COL["surface"], outline=COL["line"])
        for t in ticks:
            d.line([(L, y(t)), (W - Rm, y(t))], fill=COL["grid"], width=1)
            d.text((L - 8 * S, y(t) + 4 * S), fmt(t), fill=COL["ink3"], font=f_tick, anchor="rs")
        d.text((L, pn["top"] - 8 * S), pn["label"], fill=COL["ink2"], font=f_lab, anchor="ls")
        return y

    # price
    pn = panels[0]
    lows = [r["low"] or r["close"] for r in rows]
    highs = [r["high"] or r["close"] for r in rows]
    lo, hi, step = nice(min(lows) * 0.985, max(highs) * 1.015, 4)
    ticks = [lo + k * step for k in range(int(round((hi - lo) / step)) + 1)]
    y = frame(pn, lo, hi, ticks, lambda v: R.fmt_px(v))
    pts = []
    for i, r in enumerate(rows):
        if r["high"] and r["low"]:
            d.line([(x(i), y(r["high"])), (x(i), y(r["low"]))], fill=COL["whisker"], width=1 * S)
        pts.append((x(i), y(r["close"])))
    solid = [p for p, r in zip(pts, rows) if not r["partial"]]
    if len(solid) >= 2:
        d.line(solid, fill=COL["price"], width=2 * S, joint="curve")
    if rows and rows[-1]["partial"] and len(pts) >= 2:
        _dashed(d, pts[-2], pts[-1], 4 * S, 3 * S, COL["price"], 2 * S)
    lastp = solid[-1] if solid else pts[-1]
    d.ellipse([lastp[0] - 4 * S, lastp[1] - 4 * S, lastp[0] + 4 * S, lastp[1] + 4 * S], fill=COL["ground"], outline=COL["price"], width=2 * S)
    lastr = next((r for r in reversed(rows) if not r["partial"]), rows[-1])
    d.text((lastp[0] - 8 * S, lastp[1] - 8 * S), R.fmt_px(lastr["close"]), fill=COL["ink"], font=f_val, anchor="rs")

    # rvol
    pn = panels[1]
    rvs = [r["rvol5"] for r in rows if r["rvol5"] is not None]
    hi_r = max([3.2] + rvs) * 1.05
    lo, hi, step = nice(0, hi_r, 4)
    ticks = [k * step for k in range(int(round(hi / step)) + 1)]
    y = frame(pn, 0, hi, ticks, lambda v: f"{v:.1f}")
    d.rectangle([L + 1, y(B.RVOL_MAX), W - Rm - 1, y(B.RVOL_MIN)], fill=COL["band"])
    _dashed(d, (L, y(B.RVOL_MIN)), (W - Rm, y(B.RVOL_MIN)), 3 * S, 3 * S, COL["bandedge"], 1 * S)
    _dashed(d, (L, y(B.EXHAUST_RVOL)), (W - Rm, y(B.EXHAUST_RVOL)), 2 * S, 4 * S, COL["hot"], 1 * S)
    d.text((W - Rm - 4 * S, y(B.RVOL_MIN) - 5 * S), f"{B.RVOL_MIN:.1f} band floor", fill=COL["ink2"], font=f_val, anchor="rs")
    d.text((W - Rm - 4 * S, y(B.EXHAUST_RVOL) - 5 * S), f"{B.EXHAUST_RVOL:.1f} exhaustion", fill=COL["ink2"], font=f_val, anchor="rs")
    if rvs:
        pts = [(x(i), y(r["rvol5"])) for i, r in enumerate(rows) if r["rvol5"] is not None]
        if len(pts) >= 2:
            d.line(pts, fill=COL["rvol"], width=2 * S, joint="curve")
        lp = pts[-1]
        d.ellipse([lp[0] - 4 * S, lp[1] - 4 * S, lp[0] + 4 * S, lp[1] + 4 * S], fill=COL["ground"], outline=COL["rvol"], width=2 * S)
        d.text((lp[0] - 8 * S, lp[1] - 8 * S), f"{rvs[-1]:.2f}", fill=COL["ink"], font=f_val, anchor="rs")
    else:
        d.text(((L + W - Rm) / 2, pn["top"] + pn["h"] / 2), f"needs {FEATURE_MIN} sessions for a band reading",
               fill=COL["ink3"], font=f_lab, anchor="mm")

    # volume
    pn = panels[2]
    vmax = max([r["volume"] for r in rows] + [r["m20"] for r in rows if r["m20"]]) * 1.08
    lo, hi, step = nice(0, vmax / 1e6, 3)
    ticks = [k * step for k in range(int(round(hi / step)) + 1)]
    y = frame(pn, 0, hi * 1e6, [t * 1e6 for t in ticks], lambda v: f"{v / 1e6:.0f}")
    base = y(0)
    for i, r in enumerate(rows):
        top = y(r["volume"])
        high = r["vp"] is not None and r["vp"] >= R.VOL_TOP
        if r["partial"]:
            d.rectangle([x(i) - bw / 2, top, x(i) + bw / 2, base - 1], outline=COL["vol"], width=1 * S)
        else:
            d.rectangle([x(i) - bw / 2, top, x(i) + bw / 2, base - 1], fill=COL["volhigh"] if high else COL["vol"])
        if high:
            d.text((x(i), top - 4 * S), f"vp {r['vp']:.2f}", fill=COL["ink"], font=f_val, anchor="ms")
        if r["partial"]:
            d.text((x(i), top - 4 * S), "partial", fill=COL["ink3"], font=f_val, anchor="ms")
    mpts = [(x(i), y(r["m20"])) for i, r in enumerate(rows) if r["m20"]]
    for a, b in zip(mpts, mpts[1:]):
        _dashed(d, a, b, 5 * S, 4 * S, COL["mean"], 1 * S + S // 2)

    # x axis
    yb = panels[2]["top"] + panels[2]["h"]
    for i, r in enumerate(rows):
        wd = dt.date.fromisoformat(r["date"]).weekday()
        show = wd == 0 or i == 0 or i == n - 1
        d.line([(x(i), yb), (x(i), yb + (6 if show else 3) * S)], fill=COL["line"], width=1 * S)
        if show and not (i == n - 1 and n > 1 and dt.date.fromisoformat(rows[n - 2]["date"]).weekday() == 0):
            d.text((x(i), yb + 22 * S), r["date"][5:].replace("-", "/"), fill=COL["ink3"], font=f_tick, anchor="ms")
    d.text((L, H - 14 * S), f"Read-out, not a rule: {STATS}. Yahoo daily bars, session {ctx['session']}.",
           fill=COL["ink3"], font=f_lab, anchor="ls")

    out = img.resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    if path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return data


# --------------------------------------------------------------------------- selftest

def _selftest() -> int:
    fails = []

    def chk(cond, what):
        if not cond:
            fails.append(what)

    # need_volume is exact: plug x back into the rolling formula
    import random
    rnd = random.Random(7)
    for _ in range(50):
        v = [rnd.uniform(1e6, 9e6) for _ in range(20)]
        s4, s19 = sum(v[-4:]), sum(v[-19:])
        for r in (1.5, 3.0):
            xx = need_volume(s4, s19, r)
            if xx > 0:
                got = ((s4 + xx) / 5) / ((s19 + xx) / 20)
                chk(abs(got - r) < 1e-9, f"need_volume exactness r={r}: {got}")
    chk(need_volume(4e6, 1e6, 1.5) == 0.0, "need_volume clamps at 0")
    chk(math.isinf(need_volume(1, 1, 4.0)), "need_volume inf at r>=4")

    # direction labels
    chk(R.direction([1.0, 1.1, 1.2, 1.3, 1.4, 1.6]) == "rising into the band", "direction rising")
    chk(R.direction([2.0, 1.9, 1.8, 1.6, 1.4, 1.2]) == "falling", "direction falling")
    chk(R.direction([3.4, 3.5, 3.6, 2.9, 2.6, 2.5]).startswith("decayed"), "direction decayed")
    chk(R.direction([1.0, 1.0, 1.01, 1.0, 1.0, 1.0]) == "holding", "direction holding")

    # single_panel drops a Sunday phantom and a zero-volume bar, keeps contiguity
    rows = {}
    day = dt.date(2026, 1, 5)
    t = 1_000_000
    k = 0
    while k < 130:
        if day.weekday() < 5:
            rows[day.isoformat()] = (100 + k, 102 + k, 98 + k, 101 + k, 1e6 + k * 1e4, 101 + k, t)
            k += 1
        day += dt.timedelta(days=1)
        t += 86400
    rows["2026-02-01"] = (150, 150, 150, 150, 0, 150, t)              # Sunday phantom, zero volume
    rows["2025-12-31"] = (150, 150, 150, 150, 0, 150, t)              # weekday holiday phantom, before the range
    p, dropped = single_panel("TEST", rows)
    chk(dropped["weekend"] == 1 and dropped["zero_volume"] == 1, f"single_panel drops: {dropped}")
    chk(len(p.dates) == 130, f"single_panel keeps 130: {len(p.dates)}")
    i = len(p.dates) - 1
    chk(features(p, "TEST", i) is not None, "features on the shim")
    vh = R.volume_high(p, "TEST", i)
    chk(vh is not None and abs(vh["vpct50"] - 1.0) < 1e-9, f"vpct50 on a rising series is 1.0: {vh}")
    # probe formula agreement
    vo = [p.volume["TEST"][j] for j in range(i - 50, i + 1)]
    chk(abs(sum(1 for h in vo[:-1] if h < vo[-1]) / 50 - vh["vpct50"]) < 1e-12, "vpct50 matches the probe formula")

    # pick: before and after 16:15 on the newest bar's day
    last = p.dates[-1]
    y, m, dd = (int(s) for s in last.split("-"))
    before = dt.datetime(y, m, dd, 11, 0, tzinfo=WIB)
    after = dt.datetime(y, m, dd, 16, 30, tzinfo=WIB)
    chk(pick(p, before) == (i - 1, i), f"pick before close: {pick(p, before)}")
    chk(pick(p, after) == (i, None), f"pick after close: {pick(p, after)}")

    # fit trims by priority and never exceeds the budget
    blocks = [(100, ["H"]), (95, ["v" * 100] * 10), (80, ["T"] + ["r" * 60] * 12 + ["k"]),
              (70, ["h" * 500]), (60, ["c" * 500]), (99, ["f" * 200])]
    txt = fit([(pr, list(ls)) for pr, ls in blocks], budget=1500)
    chk(len(txt) <= 1500 and txt.startswith("H") and "\nf" in txt, f"fit budget: {len(txt)}")
    chk(all(len(line) in (1, 60, 100, 200, 500) for line in txt.splitlines()), "fit never cuts mid-line")

    # render_png on a synthetic ctx with the fallback font
    chart = [{"date": p.dates[j], "close": p.raw_close["TEST"][j], "high": p.high["TEST"][j],
              "low": p.low["TEST"][j], "volume": p.volume["TEST"][j],
              "rvol5": 1.0 + 0.02 * (j - i + 30), "vp": (0.95 if j == i else 0.3),
              "m20": p.volume["TEST"][j] * 0.9, "partial": False} for j in range(i - 29, i + 1)]
    ctx = {"sym": "TEST", "name": "Test Tbk", "session": last, "close": p.raw_close["TEST"][i],
           "score": {"rvol5": 1.6, "cell": "IN BAND", "label": "rising into the band", "vh": {"vpct50": 0.95}},
           "chart": chart}
    try:
        data = render_png(ctx)
        chk(len(data) < 1_000_000 and data[:8] == b"\x89PNG\r\n\x1a\n", f"png size {len(data)}")
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        chk(im.size == (1200, 800), f"png dims {im.size}")
    except Exception as e:                                       # noqa: BLE001
        fails.append(f"render_png raised {e!r}")

    for f_ in fails:
        print("FAIL:", f_)
    print("selftest:", "ok" if not fails else f"{len(fails)} failure(s)")
    return 0 if not fails else 1


# --------------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("--png", default=None, help="write the chart here")
    ap.add_argument("--n", type=int, default=10, help="table rows")
    ap.add_argument("--sessions", type=int, default=30, help="chart window")
    ap.add_argument("--date", default=None, help="force a closed session YYYY-MM-DD")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.symbol:
        ap.error("symbol required")
    sym = a.symbol.upper().removesuffix(".JK")
    log = _log_noop if a.quiet else (lambda *x: print(*x, file=sys.stderr))
    before = R.fingerprints()
    ctx = build(sym, n=a.n, sessions=a.sessions, date=a.date, use_cache=not a.no_cache, log=log)
    text = render_text(ctx)
    print(text)
    print(f"chars={len(text)}", file=sys.stderr)
    if ctx.get("error"):
        R.assert_unmoved(before)
        return 2
    if a.png:
        data = render_png(ctx, a.png)
        print(f"png={a.png} bytes={len(data)}", file=sys.stderr)
    R.assert_unmoved(before)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
