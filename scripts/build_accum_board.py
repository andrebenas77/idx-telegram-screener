#!/usr/bin/env python3
"""Build the accumulation board — the pre-momentum stage of the funnel.

    Crowd (attention) -> ACCUMULATION (footprint) -> Momentum (price) -> Trade plan

OBSERVATION MODE. Nothing on this page has passed a walk-forward test yet, and the page
says so in its own header. It emits no entry, no stop and no size; `trade_plan.py`
integration stays blocked until `accum_test.py --mode gate` clears the bar declared in
reference/accumulation.md 6.6. That sequencing was chosen deliberately: the board exists
now so names stop being missed, and the trade half waits for evidence.

WHAT IS ALREADY KNOWN, and is on the page rather than buried

  The universe was the real defect. `tickers.csv` is a Telegram alias dictionary, and the
  panel built from it reproduced Sectors' own top-10-by-value on 4 of 49 days. DSSA had
  been top-10 by value since 2026-06-03 and was in no board at any score. The universe is
  now value-derived and dated (build_universe.py); DSSA now appears in the top-20 on 36 of
  40 sessions.

  Net value cannot separate an accumulator from a market maker. On BREN, CC netted
  +33.7bn on 132.1/98.5bn gross (57% - churn) while DX netted +55.5bn on 56.4/0.9bn
  (98% - real). A ratio is not recoverable from a difference, hence the gross partition.

  REFUTED 2026-08-13 (accum_test.py --mode coalition, n=8,675 baseline stock-days):
  persistence of net buying plus a size floor has NO edge inside a top-20 universe.
  5d lift -0.06pp against a required +1.2pp, and both null controls reproduced the real
  result to within 0.03pp. The rule fired on 56-65% of the universe, so it was never a
  screen. The COALITION form was tested and REJECTED: its marginal events flipped sign
  across the theta grid (+0.23 / -0.63 / +0.44pp). That is why one-sidedness, not net, is
  the column this board is built on - and why it is still labelled unvalidated.

Usage:
    py scripts/build_accum_board.py [--date YYYY-MM-DD] [--top 20] [--summary]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import accum_lib  # noqa: E402
from alpha_lib import PANEL, Panel  # noqa: E402
from overlay_test import features  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
WIB = timezone(timedelta(hours=7))
SITE = "https://andrebenas77.github.io/idx-telegram-screener/accumulation.html"

BN = 1_000_000_000
WINDOWS = (5, 20, 60)


def rupiah(v) -> str:
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:,.2f}T"
    if a >= 1e9:
        return f"{v / 1e9:,.1f}B"
    if a >= 1e6:
        return f"{v / 1e6:,.0f}M"
    return f"{v:,.0f}"


def pct(v, dp=0) -> str:
    return "-" if v is None else f"{v * 100:.{dp}f}%"


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ inputs

def load_universe() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(PANEL.glob("universe-*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                out[r["date"]].append(r)
    for d in out:
        out[d].sort(key=lambda r: int(r["rank_value"]))
    return out


def load_gross() -> dict[tuple[str, str], dict[str, dict]]:
    """{(symbol, broker): {date: row}} from gross-*.csv.gz. Empty when not yet built —
    the board then degrades to a universe listing rather than refusing to render, the
    same posture every other builder here takes toward a missing input."""
    out: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for path in sorted(PANEL.glob("gross-*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                out[(r["symbol"], r["broker"])][r["date"]] = {
                    "buy_value": f(r["buy_value"]) or 0.0,
                    "sell_value": f(r["sell_value"]) or 0.0,
                    "buy_freq": f(r["buy_freq"]) or 0.0,
                    "sell_freq": f(r["sell_freq"]) or 0.0,
                    "buy_avg": f(r.get("buy_avg")),
                }
    return out


def window_dates(p: Panel, i: int, w: int) -> list[str]:
    return [p.dates[j] for j in range(max(0, i - w + 1), i + 1)]


def broker_window(rows: dict[str, dict], dates: list[str]) -> dict:
    bv = sum(rows[d]["buy_value"] for d in dates if d in rows)
    sv = sum(rows[d]["sell_value"] for d in dates if d in rows)
    bf = sum(rows[d]["buy_freq"] for d in dates if d in rows)
    sf = sum(rows[d]["sell_freq"] for d in dates if d in rows)
    return {"buy_value": bv, "sell_value": sv, "net": bv - sv, "gross": bv + sv,
            "buy_freq": bf, "sell_freq": sf}


def daily_xr(p: Panel, sym: str, dates: list[str]) -> dict[str, float]:
    """Per-session excess return vs IHSG, keyed by date.

    Excess, not raw: `absorb` is meant to catch a broker buying while the stock is weak
    RELATIVE to the market. On a day the whole index falls 2%, a stock closing flat is
    strong, and counting that as absorption would fire the entry rule on every name in a
    down market.
    """
    out = {}
    cl = p.close.get(sym) or {}
    for d in dates:
        i = p.didx.get(d)
        if i is None or i - 1 < 0:
            continue
        if i not in cl or (i - 1) not in cl or cl[i - 1] <= 0:
            continue
        b0, b1 = p.bench.get(i - 1), p.bench.get(i)
        if not b0 or not b1:
            continue
        out[d] = (cl[i] / cl[i - 1]) - (b1 / b0)
    return out


def dd_over(p: Panel, sym: str, i: int, w: int):
    cl = p.close.get(sym) or {}
    hist = [cl[j] for j in range(max(0, i - w + 1), i + 1) if j in cl]
    if len(hist) < 3 or max(hist) <= 0:
        return None
    return hist[-1] / max(hist) - 1


def excess_over(p: Panel, sym: str, i: int, w: int):
    """Excess return of the last w sessions vs IHSG, ending at i."""
    cl, j = p.close.get(sym) or {}, i - w
    if i not in cl or j not in cl or j not in p.bench or i not in p.bench:
        return None
    if cl[j] <= 0 or p.bench[j] <= 0:
        return None
    return (cl[i] / cl[j]) - (p.bench[i] / p.bench[j])


# ------------------------------------------------------------------ build

def hot_list_for(p: Panel, i: int, universe: dict, top: int,
                 lookback: int = 40) -> list[str]:
    """Names that were top-`top` by value on at least one of the trailing `lookback`
    sessions ending at i.

    NOT same-day rank, and the difference is the whole point. Stealth accumulation
    happens on QUIET days — BREN traded Rp138bn on 2026-08-11, below that session's
    top-20 cut, and a same-day filter therefore drops the name on precisely the day the
    board is supposed to flag it. It reappears the next session, at +13.3%, far too late.

    A trailing window still honours "top 10-20 traded names" as a liquidity requirement:
    the stock has to have been that liquid recently, just not on the sleepy day itself.
    """
    lo = max(0, i - lookback + 1)
    hot: list[str] = []
    seen = set()
    for j in range(i, lo - 1, -1):        # most recent first, so ordering is stable
        for r in universe.get(p.dates[j], [])[:top]:
            s = r["symbol"]
            if s not in seen:
                seen.add(s)
                hot.append(s)
    return hot


def build(p: Panel, i: int, universe: dict, gross: dict, alpha: dict,
          top: int) -> list[dict]:
    session = p.dates[i]
    members = hot_list_for(p, i, universe, top)
    n_brokers = len(alpha) or None
    rows = []

    for sym in members:
        adtv = (p.adtv.get(sym) or {}).get(i)
        if not adtv:
            continue
        try:
            # features() RETURNS None (rather than raising) when a name has too little
            # history — a new listing, or one that entered the universe recently. The
            # try/except alone does not catch that, and it only surfaced once the gross
            # partition widened from 4 symbols to 52.
            feat = features(p, sym, i) or {}
        except Exception:
            feat = {}

        d5, d20 = window_dates(p, i, 5), window_dates(p, i, 20)
        xr_by_date = daily_xr(p, sym, d20)
        # Market-wide freq/value for this stock over 20d, for slice_z's denominators.
        tot_f = tot_v = 0.0
        for (s2, _), bd in gross.items():
            if s2 == sym:
                ww = broker_window(bd, d20)
                tot_f += ww["buy_freq"]
                tot_v += ww["buy_value"]

        brokers = []
        for (s, code), byday in gross.items():
            if s != sym:
                continue
            w = {n: broker_window(byday, window_dates(p, i, n)) for n in WINDOWS}
            nets = {d: (byday[d]["buy_value"] - byday[d]["sell_value"])
                    for d in byday}
            prev_dates = [x for x in window_dates(p, i - 1, 20)] if i > 0 else []
            wprev = broker_window(byday, prev_dates) if prev_dates else None
            osr = {n: accum_lib.osr(w[n]["buy_value"], w[n]["sell_value"], adtv,
                                    window=n)
                   for n in WINDOWS}
            if osr[20] is None:
                continue          # definedness guard: excluded, never scored neutral
            today = byday.get(session) or {}
            brokers.append({
                "broker": code,
                "osr5": osr[5], "osr20": osr[20], "osr60": osr[60],
                "net5": w[5]["net"], "net20": w[20]["net"], "net60": w[60]["net"],
                "gross20": w[20]["gross"],
                "ats": accum_lib.ats(w[20]["buy_value"], w[20]["buy_freq"]),
                "slice_z": accum_lib.slice_z(w[20]["buy_freq"], w[20]["buy_value"],
                                             tot_f, tot_v),
                "osr1d": accum_lib.osr(today.get("buy_value", 0.0),
                                       today.get("sell_value", 0.0), adtv, window=1),
                "osr20_prev": (accum_lib.osr(wprev["buy_value"], wprev["sell_value"],
                                             adtv, window=20) if wprev else None),
                "softrun5": accum_lib.softrun(nets, d5),
                "softrun20": accum_lib.softrun(nets, d20),
                "absorb5": accum_lib.absorb_score(nets, xr_by_date, d5),
                "absorb20": accum_lib.absorb_score(nets, xr_by_date, d20),
                "run_buy": accum_lib.run_buy(nets, session),
                "absorb_today": accum_lib.absorb_today(
                    nets.get(session), xr_by_date.get(session), adtv),
                "buy_avg": today.get("buy_avg"),
                "rank": (alpha.get(code) or {}).get("rank"),
            })
        if not brokers:
            continue

        # Evaluate the bucket conditions JOINTLY PER BROKER, then take the best.
        #
        # accumulation.md 4.3 asks whether "at least one broker" is one-sided AND
        # absorbing AND large. Testing those on whichever broker happens to have the
        # highest osr20 is a different and weaker question, and it demonstrably loses the
        # signal: on BREN 2026-08-11 the top-osr20 broker was IF, whose buying had landed
        # on up-days (absorb5 0.21), while TP — the desk that actually sat on the bid and
        # took 72% of the day's distribution — was ranked second and never evaluated.
        stock_ctx = {
            "adtv": adtv, "rvol5": feat.get("rvol5"),
            "dd20": dd_over(p, sym, i, 20),
            "xr": excess_over(p, sym, i, 1),
            "xr5": excess_over(p, sym, i, 5),
            "xr20": excess_over(p, sym, i, 20),
        }
        for b in brokers:
            fvb = {
                "osr20": b["osr20"],
                "adtv_pct20": accum_lib.adtv_pct(b["net20"], adtv),
                "softrun20": b.get("softrun20"),
                "absorb20": b.get("absorb20"),
                "cost_gap20": accum_lib.cost_gap(b.get("buy_avg"), feat.get("vwap")),
                "slice_z20": b.get("slice_z"),
            }
            b["_wf"] = accum_lib.window_factor(b["net5"], b["net20"], b["net60"])
            b["_tilt"] = accum_lib.quality_tilt(b.get("rank"), n_brokers)
            b["_score"] = accum_lib.stealth_score(fvb, tilt=b["_tilt"], wf=b["_wf"])
            b["_bucket"] = accum_lib.classify_bucket({
                **stock_ctx,
                "osr5": b["osr5"], "osr20": b["osr20"], "osr1d": b["osr1d"],
                "osr20_prev": b.get("osr20_prev"),
                "net5": b["net5"], "gross20": b["gross20"],
                "absorb5": b.get("absorb5"), "softrun5": b.get("softrun5"),
                "softrun20": b.get("softrun20"),
                "absorb_today": b.get("absorb_today"),
                "cost_gap5": fvb["cost_gap20"], "stealth": b["_score"],
            })

        # Churn sits SECOND-TO-LAST, not mid-table. accumulation.md 4.3: churn is the
        # ABSENCE of a read and must never pre-empt one. Ranked any higher, a stock's
        # label is decided by whichever market maker was busiest — XL took over every row
        # on the first run — and a genuine accumulator two lines down is never surfaced.
        prio = {k: n for n, k in enumerate(
            ("distribution", "absorption", "markup", "stealth", "cooling", "churn",
             "none"))}
        lead = min(brokers, key=lambda b: (prio.get(b["_bucket"], 99), -b["_score"]))
        wf, tilt = lead["_wf"], lead["_tilt"]
        # cost_gap needs a genuine session VWAP. The daily panel has no VWAP, and
        # substituting the CLOSE would not measure the same thing at all — a broker can
        # average below VWAP while the stock closes on its high, which is exactly the
        # case cost_gap exists to catch. Left None rather than approximated; the missing
        # term contributes 0 and its weight is NOT redistributed, so a name without it
        # scores lower than one with it instead of being silently promoted.
        vwap = feat.get("vwap")

        fv = {
            "osr20": lead["osr20"],
            "adtv_pct20": accum_lib.adtv_pct(lead["net20"], adtv),
            "softrun20": lead.get("softrun20"),
            "absorb20": lead.get("absorb20"),
            "cost_gap20": accum_lib.cost_gap(lead.get("buy_avg"), vwap),
            "slice_z20": lead.get("slice_z"),
        }
        score = accum_lib.stealth_score(fv, tilt=tilt, wf=wf)

        row = {
            "symbol": sym, "close": (p.raw_close.get(sym) or {}).get(i),
            "adtv": adtv, "rank_value": next(
                (int(r["rank_value"]) for r in universe.get(session, [])
                 if r["symbol"] == sym), None),
            "stealth": score, "wf": wf, "tilt": tilt,
            "in_top_today": any(r["symbol"] == sym
                                for r in universe.get(session, [])[:top]),
            "rvol5": feat.get("rvol5"), "rsi": feat.get("rsi"),
            "dd20": dd_over(p, sym, i, 20), "dd60": feat.get("dd60"),
            "trend": feat.get("trend"),
            "xr": excess_over(p, sym, i, 1),
            "xr5": excess_over(p, sym, i, 5),
            "xr20": excess_over(p, sym, i, 20),
            "brokers": sorted(brokers, key=lambda b: -(b["net20"] or 0))[:4],
            "lead": lead,
            "conflict": accum_lib.window_conflict(lead["net5"], lead["net60"]),
        }
        row.update({"osr5": lead["osr5"], "osr20": lead["osr20"],
                    "osr1d": lead["osr1d"], "net5": lead["net5"],
                    "gross20": lead["gross20"], "cost_gap5": fv["cost_gap20"],
                    "absorb5": lead.get("absorb5"),
                    "softrun5": lead.get("softrun5"),
                    "softrun20": lead.get("softrun20"),
                    "osr20_prev": lead.get("osr20_prev"),
                    "run_buy": lead.get("run_buy")})
        row["bucket"] = lead["_bucket"]
        row["stealth"] = lead["_score"]
        rows.append(row)

    rows.sort(key=lambda r: -r["stealth"])
    return rows


# ------------------------------------------------------------------ render

BUCKET_META = [
    ("absorption", "Absorption on weakness",
     "One-sided buying into a red or flat session. This is the entry state: BREN sat "
     "here on 2026-08-11, down 1.8%, the day before +13.3%."),
    ("stealth", "Stealth accumulation",
     "A campaign is running and the price is asleep. Watchlist, not a trigger."),
    ("markup", "Markup underway",
     "Still being bought, but the move has started and the accumulator is paying up. "
     "This is the momentum board's territory."),
    ("distribution", "Distribution - retail trap",
     "A prior accumulator has flipped to selling into strength. Avoid or exit."),
    ("churn", "Churn - not accumulation",
     "Large two-way footprint with no net positioning. Shown so a market maker is never "
     "mistaken for a whale."),
    ("cooling", "Cooling - campaign ended",
     "Was actionable within 10 sessions; the buying has stopped."),
]


def table(rows: list[dict]) -> str:
    if not rows:
        return '<p class="none">nothing in this state today</p>'
    head = ("<tr><th>#</th><th>Ticker</th><th class='num'>Close</th>"
            "<th class='num'>Score</th><th class='num'>osr 5/20/60</th>"
            "<th class='num'>Net 20d</th><th class='num'>%ADTV</th>"
            "<th class='num'>RVOL</th><th class='num'>&Delta;1d</th>"
            "<th>Lead broker</th></tr>")
    body = []
    for r in rows:
        lead = r["lead"]
        xr = r.get("xr")
        cls = "up" if (xr or 0) > 0 else ("dn" if (xr or 0) < 0 else "mid")
        badge = (' <span class="brk" title="5d and 60d flow disagree - a short-window '
                 'buyer who is a long-window seller is a bounce trader, not an '
                 'accumulator">&#9888;</span>' if r["conflict"] else "")
        rvol = f"{r['rvol5']:.2f}" if r.get("rvol5") is not None else "-"
        close = f"{r['close']:,.0f}" if r.get("close") is not None else "-"
        others = " ".join(b["broker"] for b in r["brokers"][:3]
                          if b["broker"] != lead["broker"])
        body.append(
            f"<tr><td class='dimtx'>{r.get('rank_value') or '-'}</td>"
            f"<td class='tk'>{esc(r['symbol'])}</td>"
            f"<td class='num'>{close}</td>"
            f"<td class='num'><b>{r['stealth']:.0f}</b></td>"
            f"<td class='num'>{pct(lead['osr5'])} / {pct(lead['osr20'])} / "
            f"{pct(lead['osr60'])}{badge}</td>"
            f"<td class='num'>{rupiah(lead['net20'])}</td>"
            f"<td class='num'>{pct(accum_lib.adtv_pct(lead['net20'], r['adtv']))}</td>"
            f"<td class='num'>{rvol}</td>"
            f"<td class='num {cls}'>{pct(xr, 1)}</td>"
            f"<td class='bks'><span class='bk good'>{esc(lead['broker'])}</span>"
            f"{esc(others)}</td></tr>")
    return f"<div class='scroll'><table>{head}{''.join(body)}</table></div>"


def summary_text(session: str, buckets: dict, top: int = 6) -> str:
    """Plain text for notify_telegram.py — no markdown, it 400s on unescaped punctuation."""
    parts = [f"IDX ACCUMULATION - session {session}",
             "DESCRIPTIVE ONLY - no validated signal. The gate test was INCONCLUSIVE:",
             "its 59-session window also inverts the validated momentum rule",
             "(-1.39pp vs +2.26pp full-sample), so it refutes nothing. Do not trade."]
    for key, title, _ in BUCKET_META:
        rows = buckets.get(key) or []
        if not rows:
            continue
        parts.append(f"\n{title.upper()} ({len(rows)})")
        for r in rows[:top]:
            lead = r["lead"]
            parts.append(
                f"{r['symbol']} {r['close']:,.0f} | score {r['stealth']:.0f} | "
                f"{lead['broker']} osr {pct(lead['osr20'])} net {rupiah(lead['net20'])}"
                + ("  [window conflict]" if r["conflict"] else ""))
    parts.append("\nOne-sidedness = buy/(buy+sell). Net value alone cannot tell an "
                 "accumulator from a market maker.")
    parts.append(SITE)
    return "\n".join(parts)


CSS = """
:root{--bg:#0e1116;--card:#161b22;--line:#242c37;--tx:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--bad:#f85149;--acc:#58a6ff;--warn:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 6px}
.sub{color:var(--dim);font-size:13px;margin:0 0 6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin:14px 0}
.banner{border-left:3px solid var(--warn);background:#1c1810}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--dim);font-weight:600;padding:8px 10px;
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
.tk{font-weight:700;color:var(--acc)}.num{text-align:right;font-variant-numeric:tabular-nums}
.bks{color:var(--dim);font-size:12px}
.bk.good{color:var(--ok);font-weight:600;margin-right:8px}
.none{color:var(--dim);font-style:italic;margin:8px 0}
.dimtx{color:var(--dim)}
.up{color:var(--ok)}.dn{color:var(--bad)}.mid{color:var(--dim)}
.brk{color:var(--warn);font-size:10px}
.note{color:var(--dim);font-size:12.5px;margin:8px 0 0}
.ev{font-size:12.5px;color:var(--dim);margin-top:6px}.ev b{color:var(--tx)}
.scroll{overflow-x:auto}
@media(max-width:640px){.wrap{padding:20px 12px}}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--summary", action="store_true",
                    help="print the plain-text Telegram block instead of building HTML")
    args = ap.parse_args()

    p = Panel().load()
    if not p.dates:
        print("empty panel — run backfill_panel.py", file=sys.stderr)
        return 1
    session = args.date or p.dates[-1]
    if session not in p.didx:
        print(f"{session} is not a session in the panel", file=sys.stderr)
        return 1
    i = p.didx[session]

    universe = load_universe()
    gross = load_gross()
    alpha = {}
    ap_path = PANEL / "broker_alpha.json"
    if ap_path.exists():
        try:
            raw = json.loads(ap_path.read_text(encoding="utf-8"))
            ranked = raw.get("ranked") or raw.get("brokers") or []
            alpha = {r["broker"]: {"rank": n + 1}
                     for n, r in enumerate(ranked) if r.get("broker")}
        except Exception:
            alpha = {}

    rows = build(p, i, universe, gross, alpha, args.top)
    buckets = {key: [r for r in rows if r["bucket"] == key] for key, _, _ in BUCKET_META}

    if args.summary:
        print(summary_text(session, buckets))
        return 0

    if not gross:
        print("WARNING: no gross-*.csv.gz — one-sidedness is unavailable and the board "
              "will be empty. Run backfill_gross.py.", file=sys.stderr)

    gen = datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")
    sections = []
    for key, title, blurb in BUCKET_META:
        sections.append(
            f"<h2>{esc(title)}</h2><p class='sub'>{esc(blurb)}</p>"
            f"<div class='card'>{table(buckets[key])}</div>")

    body = f"""<main class="wrap">
<h1>IDX Accumulation <span class="dimtx">&mdash; {esc(session)}</span></h1>
<p class="sub">Built {esc(gen)} &middot; universe: top {args.top} by traded value &middot;
{len(rows)} names scored &middot;
<a href="momentum.html" style="color:var(--acc)">momentum board</a> &middot;
<a href="index.html" style="color:var(--acc)">crowded board</a></p>

<div class="card banner">
<b>Observation mode &mdash; DESCRIPTIVE ONLY. Do not trade this board.</b> Its ranking
rule has not passed a walk-forward test, so the page emits no entry, stop or size. It is
kept because knowing <i>who is accumulating what</i> is useful in itself.
<div class="ev"><b>Gate test (52 symbols &times; 59 sessions, 161,313 broker-days):</b>
one-sidedness (<code>osr20 &ge; 0.80</code>, net &ge; 20% ADTV) scored a 5-day lift of
<b>&minus;0.83pp</b> against a required +1.2pp, n=162.
<b>That is INCONCLUSIVE, not a refutation.</b> The same 59-session window returns
&minus;1.39pp for the <i>validated</i> momentum rule, which earns +2.26pp over the full
two years &mdash; a ~3.8pp swing. Against the shared top-20 baseline the momentum rule was
the <i>worse</i> of the two there (&minus;1.97pp against &minus;0.83pp). The window is
hostile to the whole accumulation family, so it cannot separate a bad rule from a bad
quarter. Extending coverage is the open item.<br>
<b>Refuted on full history, and these do stand:</b> net-persistence plus a size floor
(&minus;0.06pp, n=8,675, nulls reproducing the real result to 0.03pp); the
<b>coalition</b> form of the size floor (marginal events flipping sign across the grid,
+0.23 / &minus;0.63 / +0.44pp); and the original quiet-accumulation thesis
(&minus;0.04 / +0.01pp).<br>
<b>For an actual signal use the momentum board</b>, which requires price confirmation and
is the only rule here validated over the full panel.</div>
</div>

<div class="card">
<b>How to read this.</b>
<div class="ev">
<b>osr</b> (one-sidedness) is <code>buy / (buy + sell)</code> over 5, 20 and 60 sessions.
It is the whole point: on BREN, CC netted +33.7bn on 132.1bn of buying and 98.5bn of
selling &mdash; 57%, market-making churn &mdash; while DX netted +55.5bn on 56.4bn bought
and 0.9bn sold, 98%. Ranked by <i>net</i> they sit side by side. A ratio is not
recoverable from a difference.<br>
<b>Three windows, never averaged.</b> BK was +Rp73.1bn in BREN over 8 days and
&minus;Rp413.4bn over 90. Averaging those reads as &ldquo;churn&rdquo; &mdash; wrong in a
new way &mdash; so a disagreement is penalised and <b>badged</b> instead.<br>
<b>Deliberately absent:</b> net foreign flow. It was <i>negative</i> on both decisive BREN
days while foreign brokers were the accumulators; on the tape, foreign clients bought
Rp138.5bn on 2026-08-12 against a headline of &minus;Rp1.52bn, because the standard metric
aggregates foreign-<i>broker</i> net and misses foreign clients routed through domestic
brokers.<br>
<b>Frequency:</b> high trade count relative to value is the <i>crowd</i>, not the whale.
Accumulators ran slice_z &minus;0.68 to &minus;1.07 (Rp31&ndash;46m tickets); retail
brokers ran +0.34 to +1.04 on Rp5&ndash;7m tickets. The sign of that term is an open
question the walk-forward will settle.
</div></div>

{''.join(sections)}

<p class="note">Attention and footprint measures, not investment advice. Gross of costs.
Broker codes are unmasked only after the close, so this is always the previous session.</p>
</main>"""

    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>IDX Accumulation &mdash; {esc(session)}</title>'
            f"<style>{CSS}</style></head><body>{body}</body></html>")

    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "accumulation.html").write_text(page, encoding="utf-8")
    (PANEL / "accum_board.json").write_text(json.dumps({
        "generated_at": datetime.now(WIB).isoformat(),
        "session": session, "validated": False,
        "counts": {k: len(v) for k, v in buckets.items()},
        "rows": rows,
    }, ensure_ascii=False, indent=2, default=float), encoding="utf-8")

    print(f"accumulation board {session}: {len(rows)} scored — "
          + ", ".join(f"{k}={len(v)}" for k, v in buckets.items() if v))
    print(f"wrote {DOCS / 'accumulation.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
