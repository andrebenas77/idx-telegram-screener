#!/usr/bin/env python3
"""
Fetch last-completed-session price + volume for the tickers mentioned today, from the free
Yahoo Finance v8 chart API (.JK / Jakarta, IDR). Run after fetch_mentions.py.

    py scripts/fetch_prices.py                 # tickers from build/mentions-<newest>.json
    py scripts/fetch_prices.py --date 2026-07-24

Writes: build/prices-<date>.json  (git-ignored) — read by build_screener.py.
Morning-run note: the latest complete daily bar is *yesterday's* close, which is exactly the
price we want to pair with overnight chatter ("crowded yesterday -> watch today").
"""
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "reference" / "config.json"
BUILD = ROOT / "build"
WIB = timezone(timedelta(hours=7))


def yahoo_chart(symbol, rng):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={rng}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.load(resp)


def session_closed(bar_ts, now=None):
    """True once the session a bar belongs to has finished.

    IDX regular trading ends 16:00 WIB and pre-closing runs to ~16:15, so a bar
    stamped with today's date is still forming until then. Yahoo serves that
    partial bar as the newest row, which would make close/chg1d/rvol an
    intraday snapshot rather than the completed session the board expects.
    """
    now = now or datetime.now(WIB)
    if datetime.fromtimestamp(bar_ts, WIB).date() < now.date():
        return True
    return (now.hour, now.minute) >= (16, 15)


def compute(ticker, rng, vol_win):
    d = yahoo_chart(ticker + ".JK", rng)
    res = d["chart"]["result"][0]
    meta = res["meta"]
    q = res["indicators"]["quote"][0]
    ts = res["timestamp"]
    closes, vols = list(q["close"]), list(q["volume"])

    # Yahoo can lag consolidating the newest daily bar: it serves close=None
    # while meta already holds that session's closing print. Left alone, the
    # bar is filtered out and the board silently falls back to the session
    # before it. Backfill from meta when meta's timestamp is the same session.
    mt, mp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if ts and closes and closes[-1] is None and mt and mp is not None:
        if datetime.fromtimestamp(mt, WIB).date() == datetime.fromtimestamp(ts[-1], WIB).date():
            closes[-1] = mp
            if vols[-1] is None:
                vols[-1] = meta.get("regularMarketVolume")

    rows = [(t, c, v) for t, c, v in zip(ts, closes, vols) if c is not None]
    if rows and not session_closed(rows[-1][0]):
        rows = rows[:-1]  # drop the still-forming session
    if len(rows) < 2:
        return None
    last_t, last_c, last_v = rows[-1]
    prev_c = rows[-2][1]
    chg1d = (last_c / prev_c - 1) * 100 if prev_c else None
    chg5d = (last_c / rows[-6][1] - 1) * 100 if len(rows) >= 6 and rows[-6][1] else None
    recent_vols = [v for _, _, v in rows[-(vol_win + 1):-1] if v]
    vol_avg = sum(recent_vols) / len(recent_vols) if recent_vols else None
    rvol = (last_v / vol_avg) if (vol_avg and last_v) else None
    return {
        "close": round(last_c, 2),
        "chg1d": round(chg1d, 2) if chg1d is not None else None,
        "chg5d": round(chg5d, 2) if chg5d is not None else None,
        "rvol": round(rvol, 2) if rvol is not None else None,
        "vol": int(last_v) if last_v else None,
        "currency": meta.get("currency"),
        "price_date": datetime.fromtimestamp(last_t, WIB).strftime("%Y-%m-%d"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="session date YYYY-MM-DD (default: newest mentions file)")
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    rng = cfg.get("price_range", "2mo")
    vol_win = cfg.get("vol_avg_window", 20)

    # Which tickers? Those mentioned in today's fetch.
    if args.date:
        mp = BUILD / f"mentions-{args.date}.json"
    else:
        files = sorted(BUILD.glob("mentions-*.json"))
        if not files:
            sys.exit("No build/mentions-*.json found — run fetch_mentions.py first.")
        mp = files[-1]
    mdata = json.loads(mp.read_text(encoding="utf-8"))
    session_date = mdata["date"]
    tickers = [t["ticker"] for t in mdata.get("tickers", [])]
    if not tickers:
        sys.exit(f"No tickers in {mp.name}.")

    print(f"Fetching Yahoo .JK prices for {len(tickers)} tickers (range={rng})...")
    prices, failed, price_date = {}, [], None
    for t in tickers:
        try:
            row = compute(t, rng, vol_win)
            if row is None:
                failed.append(t)
                continue
            prices[t] = row
            price_date = row["price_date"]
        except Exception as e:
            failed.append(t)
            print(f"  [!!] {t}: {type(e).__name__} {e}")
        time.sleep(0.12)  # be polite to Yahoo

    out = {
        "date": session_date,
        "generated_at": datetime.now(WIB).isoformat(timespec="seconds"),
        "price_date": price_date,
        "price_range": rng,
        "vol_avg_window": vol_win,
        "prices": prices,
        "failed": failed,
    }
    BUILD.mkdir(exist_ok=True)
    outp = BUILD / f"prices-{session_date}.json"
    outp.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nGot prices for {len(prices)}/{len(tickers)} tickers "
          f"(last close {price_date}). Failed: {', '.join(failed) or 'none'}")
    print(f"  {'TICKER':<8}{'CLOSE':>10}{'1d%':>8}{'5d%':>8}{'RVOL':>7}")
    for t in tickers[:15]:
        p = prices.get(t)
        if not p:
            continue
        def f(x, s=""):
            return f"{x:+.1f}{s}" if x is not None else "  –"
        print(f"  {t:<8}{p['close']:>10.0f}{f(p['chg1d']):>8}{f(p['chg5d']):>8}"
              f"{(str(p['rvol'])+'x') if p['rvol'] is not None else '  –':>7}")
    print(f"\nDetail -> {outp}\nNext: news scan (top {cfg.get('news_top_n',5)}), then build_screener.py")


if __name__ == "__main__":
    main()
