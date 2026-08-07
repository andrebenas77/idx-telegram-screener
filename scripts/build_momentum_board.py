#!/usr/bin/env python3
"""Build the momentum entry board — the validated replacement for the Quiet board.

Every rule here earned its place in a walk-forward test; nothing is on the page because
it sounded sensible. See reference/broker-alpha.md for the full evidence trail.

WHAT VALIDATED
  Accumulation events (>= Rp500m AND >= 10% of 20d ADTV, buying on >=2 of the last 3
  sessions) that are ALSO in a momentum state:
      RVOL5 in [1.5, 3.0)   near the 60d high (>= -10%)   RSI(14) >= 55
  -> +0.96% (3d) / +1.40% (5d) mean excess over the accumulation baseline,
     positive in 4 of 4 sub-periods.

WHAT INVERTED — shown as a separate AVOID list, not hidden
  The same accumulation with RVOL5 >= 3.0 produced -1.85% (3d) / -3.82% (5d) lift and a
  5-day hit rate of 19%. Blow-off volume marks exhaustion, not continuation.

WHAT DID NOT VALIDATE — deliberately absent
  The original "quiet accumulation" thesis (coiled bases, oversold, low chatter). Lift
  was -0.04% (3d) / +0.01% (5d): indistinguishable from zero. The gradient ran the other
  way on every feature tested.

Broker quality is a RANKING INPUT, not a gate: broker skill survived walk-forward only
when ranked on mean excess, and only by +2-3%, which is too thin to exclude names on.

Usage:
    py scripts/build_momentum_board.py [--date YYYY-MM-DD] [--top 25]
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel  # noqa: E402
from broker_alpha import build_events, score_brokers  # noqa: E402
from momentum_setup import is_momentum  # noqa: E402
from overlay_test import features  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SITE = "https://andrebenas77.github.io/idx-telegram-screener/momentum.html"

# Validated thresholds. Changing these invalidates the evidence above — re-run
# scripts/momentum_setup.py before touching them.
MIN_VALUE = 500e6
MIN_ADTV_PCT = 10.0
RVOL_MIN, RVOL_MAX = 1.5, 3.0
DD_MIN, RSI_MIN = -0.10, 55.0
EXHAUST_RVOL = 3.0

# Top N by ADTV counts as Tier A; the rest are second-liners.
# 50, not 100: the tracked universe is ~113 already-liquid names, so a top-100 boundary
# left Tier B with 12 stragglers and the split conveyed nothing. 50 actually separates
# "exitable in size" from "liquidity disappears when you need it" within this universe.
TIER_A_MAX = 50


def rupiah(v: float) -> str:
    a = abs(v)
    if a >= 1e12:
        return f"{v/1e12:,.2f}T"
    if a >= 1e9:
        return f"{v/1e9:,.1f}B"
    if a >= 1e6:
        return f"{v/1e6:,.0f}M"
    return f"{v:,.0f}"


def build(p: Panel, i: int, alpha: dict) -> tuple[list, list]:
    """Momentum candidates and exhaustion warnings for one session."""
    cands, exhaust = [], []
    for (sym, broker), series in p.flows.items():
        by_i = dict(series)
        net = by_i.get(i)
        if net is None or net < MIN_VALUE:
            continue
        a = (p.adtv.get(sym) or {}).get(i)
        if not a or net < (MIN_ADTV_PCT / 100.0) * a:
            continue
        if sum(1 for j in range(i - 2, i + 1) if by_i.get(j, 0) > 0) < 2:
            continue
        f = features(p, sym, i)
        if not f or f.get("rvol5") is None:
            continue

        row = {
            "symbol": sym, "broker": broker, "net": net,
            "adtv_pct": 100.0 * net / a,
            "rvol": f["rvol5"], "rsi": f["rsi"], "dd60": f["dd60"],
            # RAW close — the level actually printed, matching the highs and lows
            # beside it. The adjusted series is for returns only.
            "close": (p.raw_close.get(sym) or {}).get(i),
            "adtv": a,
            "broker_alpha": alpha.get(broker, {}).get("alpha"),
            "broker_rank": alpha.get(broker, {}).get("rank"),
            "broker_n": alpha.get(broker, {}).get("n"),
        }
        for k in ("hi_last", "lo_last", "hi_prev", "lo_prev", "hi_5d", "lo_5d",
                  "trend", "clv", "cmf20", "broke_5d_high"):
            row[k] = f.get(k)
        if is_momentum(f, RVOL_MIN, DD_MIN, RSI_MIN, RVOL_MAX):
            cands.append(row)
        elif f["rvol5"] >= EXHAUST_RVOL:
            exhaust.append(row)
    return cands, exhaust


def collapse(rows: list) -> list:
    """One row per symbol, aggregating the brokers accumulating it."""
    by_sym: dict[str, dict] = {}
    for r in rows:
        s = by_sym.setdefault(r["symbol"], {**r, "brokers": [], "net_total": 0.0})
        s["brokers"].append(r)
        s["net_total"] += r["net"]
    out = []
    for s in by_sym.values():
        s["brokers"].sort(key=lambda b: -b["net"])
        # Best broker rank present, so one good desk is visible even in a crowd.
        ranks = [b["broker_rank"] for b in s["brokers"] if b["broker_rank"]]
        s["best_rank"] = min(ranks) if ranks else None
        s["adtv_pct_total"] = 100.0 * s["net_total"] / s["adtv"] if s["adtv"] else 0
        out.append(s)
    return out


def rank_score(s: dict, n_brokers: int) -> float:
    """Accumulation size, tilted by broker quality — never gated by it."""
    base = s["adtv_pct_total"]
    if s["best_rank"] and n_brokers:
        # +/-20% tilt across the ranking. Deliberately modest: broker skill only
        # beat the baseline by ~2-3% out of sample.
        base *= 1.2 - 0.4 * (s["best_rank"] - 1) / max(1, n_brokers - 1)
    return base


def hl(h, l) -> str:
    if h is None or l is None:
        return '<span class="dimtx">–</span>'
    return f'{h:,.0f}<span class="dimtx"> / </span>{l:,.0f}'


def trend_cell(s: dict) -> str:
    t = s.get("trend")
    if not t:
        return '<span class="dimtx">–</span>'
    cls = {"HH/HL": "up", "LH/LL": "dn"}.get(t, "mid")
    star = ' <span class="brk" title="closed above the prior 5-session high">&#9650;</span>' \
        if s.get("broke_5d_high") else ""
    return f'<span class="tr {cls}">{t}</span>{star}'


def table(rows: list, n_brokers: int, empty: str) -> str:
    if not rows:
        return f'<p class="none">{empty}</p>'
    out = ['<table><thead><tr>'
           '<th>Ticker</th><th>Close</th>'
           '<th>Last H / L</th><th>Prior H / L</th><th>5-sess H / L</th>'
           '<th>Trend</th><th title="where the close sat in the session range">CLV</th>'
           '<th title="Chaikin Money Flow, 20 sessions">CMF</th>'
           '<th>Net accum</th><th>%ADTV</th><th>RVOL</th><th>RSI</th>'
           '<th>Brokers accumulating</th></tr></thead><tbody>']
    for s in rows:
        bks = ", ".join(
            f'<span class="bk{" good" if b["broker_rank"] and n_brokers and b["broker_rank"] <= n_brokers*0.2 else ""}" '
            f'title="rank {b["broker_rank"] or "n/a"} of {n_brokers}, {b["broker_n"] or 0} events">'
            f'{html.escape(b["broker"])} {rupiah(b["net"])}</span>'
            for b in s["brokers"][:4])
        clv = (f'{s["clv"]*100:.0f}%' if s.get("clv") is not None
               else '<span class="dimtx">–</span>')
        if s.get("cmf20") is not None:
            cmf = (f'<span class="{"up" if s["cmf20"] > 0 else "dn"}">'
                   f'{s["cmf20"]:+.2f}</span>')
        else:
            cmf = '<span class="dimtx">–</span>'
        out.append(
            f'<tr><td class="tk">{html.escape(s["symbol"])}</td>'
            f'<td class="num">{s["close"]:,.0f}</td>'
            f'<td class="num">{hl(s.get("hi_last"), s.get("lo_last"))}</td>'
            f'<td class="num">{hl(s.get("hi_prev"), s.get("lo_prev"))}</td>'
            f'<td class="num">{hl(s.get("hi_5d"), s.get("lo_5d"))}</td>'
            f'<td>{trend_cell(s)}</td>'
            f'<td class="num">{clv}</td>'
            f'<td class="num">{cmf}</td>'
            f'<td class="num">{rupiah(s["net_total"])}</td>'
            f'<td class="num">{s["adtv_pct_total"]:.0f}%</td>'
            f'<td class="num">{s["rvol"]:.2f}</td>'
            f'<td class="num">{s["rsi"]:.0f}</td>'
            f'<td class="bks">{bks}</td></tr>')
    out.append('</tbody></table>')
    return "".join(out)


def summary_text(session: str, a_rows: list, b_rows: list, exhaust: list,
                 site: str, top: int = 8) -> str:
    """Plain text for notify_telegram.py — no markdown, it is sent as-is."""
    def block(s):
        t = s.get("trend") or "–"
        clv = f'{s["clv"]*100:.0f}%' if s.get("clv") is not None else "–"
        cmf = f'{s["cmf20"]:+.2f}' if s.get("cmf20") is not None else "–"
        brk = " NEW-5d-HIGH" if s.get("broke_5d_high") else ""
        bk = ",".join(b["broker"] for b in s["brokers"][:3])
        l1 = (f'{s["symbol"]} {s["close"]:,.0f} | {rupiah(s["net_total"])} '
              f'{s["adtv_pct_total"]:.0f}%ADTV | RVOL {s["rvol"]:.2f} '
              f'RSI {s["rsi"]:.0f}')
        l2 = (f'  {t}{brk} CLV {clv} CMF {cmf} | H/L '
              f'{s.get("hi_last") or 0:,.0f}/{s.get("lo_last") or 0:,.0f} '
              f'prev {s.get("hi_prev") or 0:,.0f}/{s.get("lo_prev") or 0:,.0f} '
              f'| {bk}')
        return f"{l1}\n{l2}"

    parts = [f"IDX MOMENTUM - session {session}",
             f"{len(a_rows)} Tier A, {len(b_rows)} Tier B, "
             f"{len(exhaust)} exhaustion"]
    if a_rows:
        parts.append("\nTIER A (liquid)")
        parts += [block(s) for s in a_rows[:top]]
    if b_rows:
        parts.append("\nTIER B (second-liners)")
        parts += [block(s) for s in b_rows[:max(3, top // 2)]]
    if exhaust:
        parts.append("\nEXHAUSTION - avoid/exit (RVOL >= 3)")
        parts += [f'{s["symbol"]} {s["close"]:,.0f} RVOL {s["rvol"]:.2f} '
                  f'{rupiah(s["net_total"])}' for s in exhaust[:5]]
    parts.append(f"\nTrend/CLV/CMF are read-outs, not filters. Gross of costs.")
    parts.append(site)
    return "\n".join(parts)


CSS = """
:root{--bg:#0e1116;--card:#161b22;--line:#242c37;--tx:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--bad:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 6px}
.sub{color:var(--dim);font-size:13px;margin:0 0 6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin:14px 0}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--dim);font-weight:600;padding:8px 10px;
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
.tk{font-weight:700;color:var(--acc)}.num{text-align:right;font-variant-numeric:tabular-nums}
.bks{color:var(--dim);font-size:12px}
.bk{display:inline-block;margin-right:8px}.bk.good{color:var(--ok);font-weight:600}
.none{color:var(--dim);font-style:italic;margin:8px 0}
.dimtx{color:var(--dim)}
.up{color:var(--ok)}.dn{color:var(--bad)}.mid{color:var(--dim)}
.tr{font-weight:600;font-size:12px;letter-spacing:.02em}
.brk{color:var(--acc);font-size:10px}
.warn{border-left:3px solid var(--bad)}
.note{color:var(--dim);font-size:12.5px;margin:8px 0 0}
.ev{font-size:12.5px;color:var(--dim);margin-top:6px}
.ev b{color:var(--tx)}
.scroll{overflow-x:auto}
@media(max-width:640px){.wrap{padding:20px 12px}}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--summary", action="store_true",
                    help="print plain-text summary to stdout for notify_telegram.py")
    args = ap.parse_args()
    log = (lambda *a: None) if args.summary else print

    p = Panel().load()
    i = p.didx.get(args.date) if args.date else len(p.dates) - 1
    if i is None:
        print(f"{args.date} is not a trading day in the panel", file=sys.stderr)
        return 1
    session = p.dates[i]
    log(f"building board for {session}")

    # Broker ranking, scored on MEAN EXCESS — the objective that survived walk-forward.
    events = build_events(p, MIN_VALUE, MIN_ADTV_PCT)
    ranked = [r for r in score_brokers(events, by="mean") if not r["provisional"]]
    alpha = {r["broker"]: {**r, "rank": n} for n, r in enumerate(ranked, 1)}
    n_brokers = len(ranked)

    cands, exhaust = build(p, i, alpha)
    cands, exhaust = collapse(cands), collapse(exhaust)

    # Tier by liquidity: foreign flow is persistent in large caps and mean-reverting in
    # small ones, so blending them yields a useless average.
    adtv_rank = sorted(
        ((p.adtv.get(s) or {}).get(i, 0), s) for s in p.close if (p.adtv.get(s) or {}).get(i))
    tier_a = {s for _, s in adtv_rank[-TIER_A_MAX:]}

    for grp in (cands, exhaust):
        grp.sort(key=lambda s: -rank_score(s, n_brokers))
    a_rows = [s for s in cands if s["symbol"] in tier_a][:args.top]
    b_rows = [s for s in cands if s["symbol"] not in tier_a][:args.top]

    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = f"""<div class="wrap">
<h1>IDX Momentum Accumulation</h1>
<p class="sub">Session {html.escape(session)} &middot; generated {gen} &middot;
{len(cands)} candidates, {len(exhaust)} exhaustion warnings</p>
<div class="card ev">
<b>What this board is.</b> Brokers accumulating meaningfully (&ge;Rp500m and &ge;10% of
20-day ADTV, buying on &ge;2 of the last 3 sessions) in names that are <b>already
moving</b> &mdash; RVOL 1.5&ndash;3.0, within 10% of the 60-day high, RSI &ge; 55.
Walk-forward tested: <b>+0.96% (3d) / +1.40% (5d)</b> mean excess over the accumulation
baseline, positive in <b>4 of 4</b> sub-periods.<br>
<b>What it is not.</b> The original &ldquo;buy them while they&rsquo;re quiet&rdquo;
thesis was tested and failed &mdash; lift of &minus;0.04% / +0.01%, indistinguishable
from zero. Coiled and oversold setups are deliberately absent.<br>
<b>Broker quality tilts the ranking; it never gates it.</b> Broker skill survived
walk-forward only when ranked on mean excess, and only by ~2&ndash;3%.
Green brokers are top-quintile. Returns are gross of costs.
</div>
<div class="card ev">
<b>Reading the price columns.</b> All prices are the <b>last completed session</b> &mdash;
this runs before the open, so there is no &ldquo;today&rdquo; yet.
<b>Trend</b> is <span class="tr up">HH/HL</span> when the last session made both a higher
high and a higher low, <span class="tr dn">LH/LL</span> when it made neither, mixed
otherwise; <span class="brk">&#9650;</span> marks a close above the prior 5-session high.
<b>CLV</b> is where the close sat in the session&rsquo;s range &mdash; 100% is a close on
the high, 0% sold into the low. <b>CMF</b> is 20-session Chaikin Money Flow: positive
means volume transacted on strength, negative on weakness.<br>
These three are <b>read-outs, not filters</b> &mdash; nothing is excluded from the board
because of them. Note CMF <i>infers</i> from where each close sat in its range; it is not
a true volume-at-price profile, which would need intraday data unavailable before the
open. A session that opened weak, rallied and closed strong scores as accumulation even
if most volume changed hands near the low.
</div>

<h2>Tier A &mdash; liquid (top {TIER_A_MAX} by ADTV)</h2>
<p class="sub">Exitable in size.</p>
<div class="card scroll">{table(a_rows, n_brokers, "No candidates this session.")}</div>

<h2>Tier B &mdash; second-liners</h2>
<p class="sub">Larger edge historically, but liquidity disappears exactly when you need it.</p>
<div class="card scroll">{table(b_rows, n_brokers, "No candidates this session.")}</div>

<h2>Exhaustion &mdash; avoid / exit</h2>
<p class="sub">Same accumulation, but RVOL &ge; 3.0.</p>
<div class="card warn scroll">{table(exhaust[:args.top], n_brokers,
  "No exhaustion signals this session.")}
<p class="note">Blow-off volume inverted the edge in testing: &minus;1.85% (3d) /
&minus;3.82% (5d) lift, with a 5-day hit rate of 19%. Heavy accumulation here reads as
distribution into strength, not continuation.</p></div>
</div>"""

    DOCS.mkdir(parents=True, exist_ok=True)
    page = (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>IDX Momentum Accumulation — {html.escape(session)}</title>"
            f"<style>{CSS}</style></head><body>{body}</body></html>")
    out = DOCS / "momentum.html"
    out.write_text(page, encoding="utf-8")

    (PANEL / "momentum_board.json").write_text(
        json.dumps({"session": session, "candidates": cands, "exhaustion": exhaust,
                    "n_brokers_ranked": n_brokers},
                   ensure_ascii=False, indent=2, default=float), encoding="utf-8")

    if args.summary:
        print(summary_text(session, a_rows, b_rows, exhaust[:args.top], SITE))
        return 0

    log(f"  Tier A {len(a_rows)} | Tier B {len(b_rows)} | exhaustion {len(exhaust)}")
    log(f"  wrote {out}")
    for s in (a_rows + b_rows)[:8]:
        clv = f"{s['clv']*100:.0f}%" if s.get("clv") is not None else "-"
        bks = ",".join(b["broker"] for b in s["brokers"][:3])
        log(f"    {s['symbol']:<6} {rupiah(s['net_total']):>9} "
            f"{s['adtv_pct_total']:>5.0f}% ADTV  RVOL {s['rvol']:.2f} "
            f"RSI {s['rsi']:.0f}  {s.get('trend') or '-':<6} "
            f"CLV {clv:>4}  {bks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
