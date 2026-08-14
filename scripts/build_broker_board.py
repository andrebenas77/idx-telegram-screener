#!/usr/bin/env python3
"""Broker Behaviour board — who is on each side, and which complex a name belongs to.

    py scripts/build_broker_board.py [--date YYYY-MM-DD] [--top 20] [--summary]

DESCRIPTIVE ONLY. THIS PAGE MAKES NO PREDICTIVE CLAIM AND NEVER WILL.

    Five theses on IDX broker flow have failed a walk-forward: quiet accumulation,
    net-persistence, the coalition size floor, one-sidedness, and chaser composition.
    The last of those was THIS data, tested as an overlay on the momentum board, and its
    identity-shuffle null returned +-1.98pp — a randomly shuffled cohort produced spreads
    as large as the real one. There is no signal here. What there IS:

      - a broker's behavioural personality, which is genuinely persistent
        (disjoint-halves Spearman +0.79 lateness, +0.94 same-day)
      - who actually bought and sold a given name over the last 20 sessions
      - which verified complex a name belongs to

    That is worth looking at when reading a chart. It is not worth trading on its own, and
    the page says so in its own header rather than in a footnote.

WHY A DESCRIPTIVE PAGE IS STILL WORTH BUILDING
    The universe fix alone justifies it: `tickers.csv` was a Telegram alias dictionary, and
    the panel built from it reproduced Sectors' own top-10-by-value on 4 of 49 days. DSSA
    had been top-10 by value since 2026-06-03 and appeared on no board at any score. This
    page is built on the value-derived universe, so it shows what is actually being traded.
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

import broker_profile as bpf  # noqa: E402
from alpha_lib import PANEL, Panel  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
WIB = timezone(timedelta(hours=7))
SITE = "https://andrebenas77.github.io/idx-telegram-screener/brokers.html"

FLOW_WINDOW = 20

# Verified 2026-08-14: mean pairwise correlation of daily returns over 120 sessions,
# against a random-basket baseline of 0.262 (300 draws of 20 names, sd 0.035).
# "Specs" (ENRG/VKTR/BULL/BNBR/KOTA/JGLE) scored 0.342 — the 84th percentile of that null,
# i.e. indistinguishable from a random basket — and is deliberately NOT a group here.
GROUPS = [
    {"name": "PP complex", "members": ["PTRO", "BRPT", "TPIA", "CUAN"], "corr": 0.649,
     "note": "Tightest pair PTRO-BRPT 0.859. Group-specific broker: ZP."},
    {"name": "Bakrie / mining", "members": ["DEWA", "BUMI", "AMMN"], "corr": 0.685,
     "note": "Really DEWA-BUMI at 0.906 with AMMN attached at only ~0.57 — a pair plus a "
             "bystander. Group-specific broker: LG."},
    {"name": "BUVA / RAJA", "members": ["BUVA", "RAJA", "RATU"], "corr": 0.791,
     "note": "Correlation measured on BUVA-RAJA; RATU listed 2025-01-08 and is shown for "
             "completeness. RAJA carries a 5:1 split on 2026-07-16 — adjusted, not -80%."},
]


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


def pct(v, dp=1):
    return "-" if v is None else f"{v * 100:.{dp}f}%"


def bp(v):
    return "-" if v is None else f"{v * 10000:+.0f}"


def esc(s):
    return html.escape(str(s), quote=True)


def last_dense_session(p: Panel, min_share: float = 0.5) -> str:
    """The most recent session where a real share of the panel actually traded.

    NOT simply p.dates[-1]. Symbols are backfilled with different end dates — merging
    RATU and VKTR with a later `end` extended the panel by one day on which only those two
    have prices. Building a board on that date yields a page of blanks and a crash in the
    summary, with nothing to say it was the wrong day.
    """
    n_syms = len(p.close) or 1
    for j in range(len(p.dates) - 1, -1, -1):
        have = sum(1 for s in p.close if j in p.close[s])
        if have >= min_share * n_syms:
            return p.dates[j]
    return p.dates[-1]


def load_universe_top(session: str, top: int) -> list[str]:
    out = []
    for path in sorted(PANEL.glob("universe-*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["date"] == session:
                    out.append((int(r["rank_value"]), r["symbol"]))
    out.sort()
    return [s for _, s in out[:top]]


def window_net(p: Panel, sym: str, i: int, w: int = FLOW_WINDOW) -> dict[str, float]:
    out = {}
    for (s, b), series in p.flows.items():
        if s != sym:
            continue
        tot = sum(v for j, v in series if i - w < j <= i)
        if tot:
            out[b] = tot
    return out


def ret_over(p: Panel, sym: str, i: int, w: int):
    cl = p.close.get(sym) or {}
    j = i - w
    if i not in cl or j not in cl or cl[j] <= 0:
        return None
    return cl[i] / cl[j] - 1.0


def composition(nets: dict[str, float], scores: dict[str, float]):
    """Score-weighted composition of the flow: SUM(net*score) / SUM|net|.

    Positive = the net buying is coming from persistent chasers; negative = from persistent
    absorbers. Normalised by the gross so it describes WHO is on each side rather than HOW
    MUCH moved — the un-normalised form is a proxy for total net flow, which is exactly the
    quantity that has already failed four separate walk-forwards.
    """
    num = den = 0.0
    for b, net in nets.items():
        sc = scores.get(b)
        if sc is None:
            continue
        num += net * sc
        den += abs(net)
    return (num / den) if den else None


def tag(score):
    if score is None:
        return "", ""
    if score > 0.0020:
        return "chaser", "tg-ch"
    if score < -0.0020:
        return "absorber", "tg-ab"
    return "neutral", "tg-nu"


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
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.tk{font-weight:700;color:var(--acc)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.dimtx{color:var(--dim)}.up{color:var(--ok)}.dn{color:var(--bad)}.mid{color:var(--dim)}
.tg{font-size:10px;padding:1px 5px;border-radius:3px;margin-left:5px;white-space:nowrap}
.tg-ch{background:#3d2a12;color:#e3a008}.tg-ab{background:#12303d;color:#58a6ff}
.tg-nu{background:#22272e;color:var(--dim)}
.bk{display:inline-block;margin-right:9px;white-space:nowrap}
.note{color:var(--dim);font-size:12.5px;margin:8px 0 0}
.ev{font-size:12.5px;color:var(--dim);margin-top:6px}.ev b{color:var(--tx)}
.scroll{overflow-x:auto}
@media(max-width:640px){.wrap{padding:20px 12px}}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()

    p = Panel().load()
    if not p.dates:
        print("empty panel — run backfill_panel.py", file=sys.stderr)
        return 1
    session = args.date or last_dense_session(p)
    if session not in p.didx:
        print(f"{session} is not a session in the panel", file=sys.stderr)
        return 1
    i = p.didx[session]

    o = bpf.build_observations(p)
    grid_l = bpf.schedule(p, o, field="xr_trail")
    grid_s = bpf.schedule(p, o, field="xr_same")
    late = {b: v["score"] for b, v in bpf.scores_for(grid_l, i).items()}
    same = {b: v["score"] for b, v in bpf.scores_for(grid_s, i).items()}
    if not late:                    # before the burn-in ends, fall back to full sample
        late = {b: v["score"] for b, v in bpf.score(o, field="xr_trail").items()}
        same = {b: v["score"] for b, v in bpf.score(o, field="xr_same").items()}
    foreign = bpf.load_is_foreign()
    names = {}
    reg = ROOT / "reference" / "broker-registry.json"
    if reg.exists():
        names = json.loads(reg.read_text(encoding="utf-8")).get("names") or {}
    pas = bpf.passivity(p)

    universe = load_universe_top(session, args.top) or sorted(p.turnover)[:args.top]

    rows = []
    for sym in universe:
        nets = window_net(p, sym, i)
        if not nets:
            continue
        buyers = sorted((b for b in nets if nets[b] > 0), key=lambda b: -nets[b])[:3]
        sellers = sorted((b for b in nets if nets[b] < 0), key=lambda b: nets[b])[:3]
        rows.append({
            "symbol": sym,
            "close": (p.raw_close.get(sym) or {}).get(i),
            "r1": ret_over(p, sym, i, 1),
            "r20": ret_over(p, sym, i, FLOW_WINDOW),
            "comp": composition(nets, late),
            "buyers": [(b, nets[b]) for b in buyers],
            "sellers": [(b, nets[b]) for b in sellers],
        })
    rows.sort(key=lambda r: -(abs(r["comp"]) if r["comp"] is not None else -1))

    if args.summary:
        print(summary_text(session, rows, late))
        return 0

    gen = datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")

    def bkspan(b, v):
        lbl, cls = tag(late.get(b))
        f = " F" if foreign.get(b) else ""
        return (f"<span class='bk'>{esc(b)}{esc(f)} "
                f"<span class='dimtx'>{rupiah(v)}</span>"
                f"<span class='tg {cls}'>{lbl}</span></span>")

    body_rows = []
    for r in rows:
        c1 = "up" if (r["r1"] or 0) > 0 else ("dn" if (r["r1"] or 0) < 0 else "mid")
        c20 = "up" if (r["r20"] or 0) > 0 else ("dn" if (r["r20"] or 0) < 0 else "mid")
        comp = r["comp"]
        clbl, ccls = tag(comp)
        close = f"{r['close']:,.0f}" if r["close"] is not None else "-"
        compcell = "-" if comp is None else f"{comp * 10000:+.0f}"
        body_rows.append(
            f"<tr><td class='tk'>{esc(r['symbol'])}</td>"
            f"<td class='num'>{close}</td>"
            f"<td class='num {c1}'>{pct(r['r1'])}</td>"
            f"<td class='num {c20}'>{pct(r['r20'])}</td>"
            f"<td class='num'>{compcell}<span class='tg {ccls}'>{clbl}</span></td>"
            f"<td>{''.join(bkspan(b, v) for b, v in r['buyers']) or '-'}</td>"
            f"<td>{''.join(bkspan(b, v) for b, v in r['sellers']) or '-'}</td></tr>")
    flow_rows = "".join(body_rows)

    # broker personality table
    def spread_cell(b):
        rec = pas.get(b)
        if not rec or rec.get("spread_capture") is None:
            return "-"
        return f"{rec['spread_capture'] * 100:+.2f}%"

    ranked = sorted(late.items(), key=lambda kv: -kv[1])
    brk_rows = "".join(
        f"<tr><td class='tk'>{esc(b)}</td>"
        f"<td class='dimtx'>{esc(names.get(b, '')[:34])}</td>"
        f"<td class='num'>{bp(v)}</td>"
        f"<td class='num'>{bp(same.get(b))}</td>"
        f"<td class='num'>{'yes' if foreign.get(b) else ''}</td>"
        f"<td class='num'>{spread_cell(b)}</td></tr>"
        for b, v in ranked)

    grp_html = []
    for g in GROUPS:
        mem = []
        for m in g["members"]:
            r20 = ret_over(p, m, i, FLOW_WINDOW)
            cl = (p.raw_close.get(m) or {}).get(i)
            cls = "up" if (r20 or 0) > 0 else ("dn" if (r20 or 0) < 0 else "mid")
            mem.append(f"<span class='bk'><b class='tk'>{esc(m)}</b> "
                       + (f"{cl:,.0f} " if cl else "<span class='dimtx'>no data</span> ")
                       + (f"<span class='{cls}'>{pct(r20)}</span>" if r20 is not None
                          else "") + "</span>")
        grp_html.append(
            f"<div class='card'><b>{esc(g['name'])}</b> "
            f"<span class='dimtx'>&mdash; mean pairwise return correlation "
            f"{g['corr']:.3f} over 120 sessions, against a 0.262 random-basket "
            f"baseline</span><div style='margin:8px 0'>{''.join(mem)}</div>"
            f"<div class='ev'>{esc(g['note'])}</div></div>")

    body = f"""<main class="wrap">
<h1>IDX Broker Behaviour <span class="dimtx">&mdash; {esc(session)}</span></h1>
<p class="sub">Built {esc(gen)} &middot; top {args.top} by traded value &middot;
{FLOW_WINDOW}-session flow &middot;
<a href="momentum.html" style="color:var(--acc)">momentum board</a> &middot;
<a href="accumulation.html" style="color:var(--acc)">accumulation board</a> &middot;
<a href="index.html" style="color:var(--acc)">crowded board</a></p>

<div class="card banner">
<b>Descriptive only. This page makes no predictive claim, and it is not a signal.</b>
It shows who bought and sold, not what will happen next.
<div class="ev"><b>Five theses on IDX broker flow have failed a walk-forward:</b> quiet
accumulation (&minus;0.04/+0.01pp), net-persistence (&minus;0.06pp, n=8,675), the coalition
size floor (marginal events flipping sign), one-sidedness (+0.16pp, null-equal, gradient
running backwards), and chaser composition &mdash; the very column below. That last test
used exactly this data as an overlay on the momentum board, and its
<b>identity-shuffle null returned &plusmn;1.98pp</b>: a randomly shuffled broker cohort
produced tercile spreads as large as the real one. There is no edge here to trade.<br>
<b>What IS real:</b> a broker's behavioural personality persists &mdash; disjoint-halves
Spearman <b>+0.79</b> (lateness) and <b>+0.94</b> (same-day), the most stable measurement
in this project. So "who is on each side" is a reliable description of the present. It is
simply not a forecast.<br>
<b>For an actual signal use the momentum board</b>, the only rule here validated over the
full panel &mdash; and it waits for price confirmation.</div>
</div>

<h2>Who is on each side</h2>
<p class="sub">Net flow over the last {FLOW_WINDOW} sessions, largest three each way.
Sorted by how one-sided the composition is, not by size.</p>
<div class="card"><div class="scroll"><table>
<tr><th>Ticker</th><th class="num">Close</th><th class="num">&Delta;1d</th>
<th class="num">&Delta;20d</th><th class="num">Composition</th>
<th>Top net buyers</th><th>Top net sellers</th></tr>
{flow_rows}
</table></div></div>

<h2>Complexes</h2>
<p class="sub">Groups whose members genuinely move together, verified against a random
basket. Returns are {FLOW_WINDOW}-session, split-adjusted.</p>
{''.join(grp_html)}

<h2>Broker personality</h2>
<p class="sub">Estimated point-in-time on a trailing 250 sessions, re-estimated monthly.
Brokers below 3,000 observations are unscored rather than scored noisily.</p>
<div class="card"><div class="scroll"><table>
<tr><th>Broker</th><th>Name</th><th class="num">Lateness (bp)</th>
<th class="num">Same-day (bp)</th><th class="num">Foreign</th>
<th class="num">Spread capture</th></tr>
{brk_rows}
</table></div></div>

<div class="card">
<b>How to read this.</b>
<div class="ev">
<b>Lateness</b> asks whether a broker tends to arrive <i>after</i> a move has already
happened: mean trailing 5-day excess return on its net-buy days minus the same on its
net-sell days. Positive = late. It measures timing, so it is not confounded by whether a
desk crosses the spread.<br>
<b>Same-day</b> is the cruder version &mdash; flow aligned with <i>today's</i> move. It
cannot separate "chose to buy strength" from "crossed the spread", since a broker resting
on the bid gets filled as price falls. Shown because the two should agree in sign, and they
do (+0.59).<br>
<b>Spread capture</b> is (sell avg &minus; buy avg) on days a broker traded both sides.
Positive = it sold higher than it bought within the session, i.e. it provided liquidity.
Chasers capture less of it &mdash; they pay up.<br>
<b>Composition</b> is score-weighted flow, <code>&Sigma;(net &times; lateness) &divide;
&Sigma;|net|</code>, and it describes <b>which cohort is net accumulating</b>, not who
appears in the buyer column. Positive means the late money is building a position &mdash;
<i>either</i> because chasers are buying, <i>or</i> because absorbers are selling to them
(a negative net times a negative score is also positive). So a name can read
&ldquo;chaser&rdquo; while an absorber sits at the top of its buyer list: the tag beside
each broker is that broker's own persistent personality, independent of what it did in
this name this month. BBRI is the clearest example on the page.<br>
Normalised by the gross deliberately: the un-normalised form is mostly a proxy for total
net flow, which has already failed four walk-forwards.<br>
<b>The folk model is inverted.</b> Retail books are <i>contrarian</i>, not trend-chasing
&mdash; XL scores &minus;115bp on 60,912 observations (t = &minus;33.3), XC &minus;174bp.
Every trend-chaser in the top ranks is a foreign institution.<br>
<b>Deliberately absent:</b> net foreign flow as a screen. It was <i>negative</i> on both
decisive BREN days in August while foreign brokers were the accumulators, because the
headline metric aggregates foreign-<i>broker</i> net and misses foreign clients routed
through domestic brokers.
</div></div>

<p class="note">The panel carries only the top ~20 brokers per symbol, so a broker absent
from a name is small there rather than provably flat. Descriptive measures, not investment
advice. Broker codes are unmasked only after the close, so this is always the previous
session.</p>
</main>"""

    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>IDX Broker Behaviour &mdash; {esc(session)}</title>'
            f"<style>{CSS}</style></head><body>{body}</body></html>")

    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "brokers.html").write_text(page, encoding="utf-8")
    (PANEL / "broker_board.json").write_text(json.dumps({
        "generated_at": datetime.now(WIB).isoformat(), "session": session,
        "predictive": False, "n_names": len(rows), "n_brokers_scored": len(late),
        "rows": rows, "scores_lateness": late,
    }, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"broker board {session}: {len(rows)} names, {len(late)} brokers scored")
    print(f"wrote {DOCS / 'brokers.html'}")
    return 0


def summary_text(session: str, rows: list, late: dict, top: int = 6) -> str:
    """Plain text for notify_telegram.py — no markdown, it 400s on unescaped punctuation."""
    parts = [f"IDX BROKER BEHAVIOUR - session {session}",
             "DESCRIPTIVE ONLY - who bought and sold, not a forecast. Five flow theses",
             "have failed a walk-forward, this column included. Use the momentum board",
             "for a signal."]
    strong = [r for r in rows if r["comp"] is not None][:top]
    if strong:
        parts.append("\nMOST ONE-SIDED COMPOSITION (20d)")
        for r in strong:
            lbl, _ = tag(r["comp"])
            buy = ",".join(b for b, _ in r["buyers"][:2]) or "-"
            sell = ",".join(b for b, _ in r["sellers"][:2]) or "-"
            close = f"{r['close']:,.0f}" if r["close"] is not None else "-"
            parts.append(
                f"{r['symbol']} {close} {pct(r['r20'])} 20d | {lbl} "
                f"{r['comp'] * 10000:+.0f}bp | buy {buy} | sell {sell}")
    parts.append("\nRetail books are CONTRARIAN on this data, not trend-chasing. The "
                 "trend-chasers are foreign institutions.")
    parts.append(SITE)
    return "\n".join(parts)


if __name__ == "__main__":
    sys.exit(main())
