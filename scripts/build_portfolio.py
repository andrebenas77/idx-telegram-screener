#!/usr/bin/env python3
"""The book as a page, for a phone. Written to a PRIVATE repo, never to docs/.

The public site publishes derived market signals. This publishes lots, rupiah and
equity, and `.gitignore` states the rule in as many words: *"Nothing the trade layer
writes may land in docs/ — the delivery channel is Telegram and local JSON."* That rule
is not relaxed here; a second, private repo is added beside it.

Three independent guards, because the failure is unrecoverable — a book on a public,
indexed Pages site cannot be unpublished:
  1. this file refuses to write anywhere inside the public repo's docs/ tree
  2. publish_portfolio.sh refuses unless the git toplevel matches BOOK_REPO_DIR
  3. and refuses unless the remote matches the configured private remote
Any one of them is enough; all three exist because the cost of being wrong is permanent.

**Rendered server-side, deliberately.** Yahoo sends no CORS headers, so a browser fetch
from *.github.io is blocked outright — client-side is not merely riskier, it does not
work without a proxy, and a proxy is a third party learning which tickers you hold. It
would also break the house rule at assets/template.html:193 that a page "must never reach
out to anything external", which on a page listing real positions means an outbound
request per ticker is a disclosure of the book on every view.

Read surface only: no buttons, no forms. Writes happen in Telegram, where there is a
confirmation step. A "close position" button is an unconfirmed write from a device that
autocompletes.

Usage:
    py scripts/build_portfolio.py --out /path/to/idx-book/docs/index.html
    py scripts/build_portfolio.py --market-hours-only ...   # for the timer
"""
from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_prices  # noqa: E402
import position_book as pb  # noqa: E402
import trade_bot  # noqa: E402
from alpha_lib import Panel  # noqa: E402
from build_momentum_board import CSS  # noqa: E402
from trade_lib import (SHARES_PER_LOT, atr_series, config_from_env,  # noqa: E402
                       low_n_prior, market_exposure, portfolio_heat, round_tick,
                       split_lots)

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCS = ROOT / "docs"
WIB = live_prices.WIB

EXTRA_CSS = """
.chip{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;font-weight:700}
.chip.ok{background:#12331d;color:var(--ok)}.chip.bad{background:#3a1618;color:var(--bad)}
.chip.warn{background:#3a2e10;color:#d29922}.chip.mid{background:#20262e;color:var(--dim)}
.kv{display:flex;flex-wrap:wrap;gap:18px;font-size:13px;color:var(--dim)}
.kv b{color:var(--tx);font-weight:600}
.banner{border-left:3px solid #d29922;padding-left:12px;color:#d29922;font-size:13px}
.banner.bad{border-color:var(--bad);color:var(--bad)}
"""


def _refuse_public(out: Path) -> None:
    try:
        out.resolve().relative_to(PUBLIC_DOCS.resolve())
    except ValueError:
        return
    raise SystemExit(
        f"[!!] refusing to write the portfolio into the PUBLIC docs/ tree:\n     {out}\n"
        f"     That page carries lots, rupiah and equity. It belongs in the private repo.")


def market_window_ok() -> tuple:
    """(ok, why). The timer is coarse; the real window is decided here.

    IDX's lunch break and its public holidays are not expressible in an OnCalendar, and a
    hardcoded holiday list goes stale the first year nobody updates it. So the session is
    bounded by the clock and the HOLIDAY test is "did anything print" — the same
    absence-is-the-signal reasoning idx-trade-preclose.service already documents.
    """
    now = datetime.now(WIB)
    if now.weekday() >= 5:
        return False, "weekend"
    hm = now.hour * 60 + now.minute
    if hm < 8 * 60 + 55:
        return False, "before the pre-opening auction"
    if hm > 16 * 60 + 30:
        return False, "after the close"
    return True, ""


def chip(text: str, kind: str) -> str:
    return f'<span class="chip {kind}">{html.escape(text)}</span>'


def build(state: dict, p: Panel, marks: dict, cfg) -> str:
    i = len(p.dates) - 1 if p.dates else None
    positions = sorted(state["positions"].values(), key=lambda x: x["symbol"])
    eq = state["equity_idr"]
    now = datetime.now(WIB)

    newest = max((q.get("ts", 0) for q in marks.values()), default=0)
    stale = live_prices.is_stale({"ts": newest}) if newest else True
    closed_mkt = live_prices.market_looks_closed(marks) if marks else True
    if closed_mkt:
        mark_note = "closing marks — the session has ended"
    elif stale:
        mark_note = "marks are over 30 minutes old"
    else:
        mark_note = "live, delayed ~15 min (Yahoo)"

    act = pb.last_activity()
    wd = act.get("weekdays")
    banners = []
    if wd is not None and wd > 2:
        banners.append(
            f'<p class="banner bad">The ledger has not been touched in {wd} trading '
            f'sessions (last fill {html.escape((act.get("ts") or "")[:16].replace("T", " "))}). '
            f'If you have traded since, every figure on this page is wrong.</p>')
    if p.dates and i is not None:
        pstale = trade_bot.panel_staleness(p)
        if pstale:
            banners.append(f'<p class="banner">{html.escape(pstale[0])} '
                           f'Stops and E2 floors below come from that session.</p>')

    rows = []
    unreal_total = 0.0
    for pos in positions:
        sym = pos["symbol"]
        q = marks.get(sym)
        mark = (q or {}).get("px") or (p.raw_close.get(sym, {}) or {}).get(i)
        entry, stop, lots = pos["entry_px"], pos["stop_px"], pos["lots"]
        r_ps = entry - stop
        R = (mark - entry) / r_ps if (mark and r_ps) else None
        pnl = (mark - entry) * lots * SHARES_PER_LOT if mark else None
        unreal_total += pnl or 0
        a = atr_series(p, sym).get(i) if i is not None else None
        lo5 = low_n_prior(p, sym, i, cfg.struct_lookback) if i is not None else None
        tgt = round_tick(entry + cfg.scale_level_r * r_ps, "up") if r_ps else None
        sold, _keep = split_lots(lots, cfg.scale_fraction)

        # Status, most severe first. A position can be several of these at once and the
        # worst one is the one that decides what you do.
        last_close = (p.raw_close.get(sym, {}) or {}).get(i)
        prev_floor = low_n_prior(p, sym, i - 1, cfg.struct_lookback) if i else None
        if mark and mark <= stop:
            st = chip("STOP BREACHED", "bad")
        elif last_close is not None and prev_floor and last_close < prev_floor:
            st = chip("E2 FIRED", "bad")
        elif tgt and mark and mark >= tgt:
            st = chip("SCALE REACHED", "ok")
        elif lo5 and mark and mark < lo5:
            st = chip("E2 WATCH", "warn")
        else:
            st = chip("ON TRACK", "mid")

        cls = "up" if (R or 0) > 0 else ("dn" if (R or 0) < 0 else "mid")
        rows.append(
            f'<tr><td class="tk">{html.escape(sym)}</td>'
            f'<td class="num">{lots:,}</td>'
            f'<td class="num">{entry:,.0f}</td>'
            f'<td class="num">{mark:,.0f}</td>' if mark else
            f'<tr><td class="tk">{html.escape(sym)}</td><td class="num">{lots:,}</td>'
            f'<td class="num">{entry:,.0f}</td><td class="num dimtx">—</td>')
        rows[-1] += (
            f'<td class="num {cls}">{R:+.2f}R</td>' if R is not None else '<td class="num dimtx">—</td>')
        rows[-1] += (
            f'<td class="num {cls}">{pnl / 1e6:+,.1f}m</td>' if pnl is not None else '<td class="num dimtx">—</td>')
        rows[-1] += (
            f'<td class="num">{stop:,.0f}</td>'
            f'<td class="num {"dn" if mark and mark <= stop else "dimtx"}">'
            f'{((mark - stop) / mark):+.1%}</td>' if mark else '<td class="num">—</td>')
        rows[-1] += (
            f'<td class="num">{lo5:,.0f}</td>' if lo5 else '<td class="num dimtx">—</td>')
        rows[-1] += (
            f'<td class="num dimtx">{((mark - lo5) / a):.1f}</td>'
            if (lo5 and mark and a) else '<td class="num dimtx">—</td>')
        rows[-1] += (
            f'<td class="num">{tgt:,.0f}<span class="dimtx"> ({sold:,})</span></td>'
            if tgt and sold else '<td class="num dimtx">—</td>')
        rows[-1] += f'<td>{st}</td></tr>'

    closed_rows = "".join(
        f'<tr><td class="tk">{html.escape(str(c.get("symbol")))}</td>'
        f'<td class="dimtx">{html.escape(str(c.get("opened")))} &rarr; '
        f'{html.escape(str(c.get("closed")))}</td>'
        f'<td class="num">{(c.get("lots_initial") or 0):,}</td>'
        f'<td class="num">{(c.get("entry_px") or 0):,.0f}</td>'
        f'<td class="num">{(c.get("exit_px") or 0):,.0f}</td>'
        f'<td class="num {"up" if (c.get("realised_r_total") or 0) > 0 else "dn"}">'
        f'{(c.get("realised_r_total") or 0):+.2f}R</td>'
        f'<td class="num {"up" if (c.get("realised_idr") or 0) > 0 else "dn"}">'
        f'{(c.get("realised_idr") or 0) / 1e6:+,.1f}m</td>'
        f'<td class="dimtx">{html.escape(str(c.get("exit_reason") or ""))[:60]}</td></tr>'
        for c in state["closed"]) or \
        '<tr><td colspan="8" class="none">nothing closed yet</td></tr>'

    heat = portfolio_heat(positions, eq)
    body = f"""<div class="wrap">
<h1>Position book</h1>
<p class="sub">{now:%Y-%m-%d %H:%M} WIB &middot; {html.escape(mark_note)}
&middot; regenerated every 15 min while the market is open</p>
{''.join(banners)}
<div class="card"><div class="kv">
<span>equity <b>Rp{eq / 1e9:.2f}bn</b></span>
<span>open <b>{len(positions)}/{cfg.max_open}</b></span>
<span>heat <b>{heat:.2%}</b> of {cfg.heat_cap_pct:.1%}</span>
<span>beta-gross <b>{market_exposure(positions, eq):.2f}</b> of {cfg.beta_gross_cap:.2f}</span>
<span>realised <b>Rp{state['realised_idr'] / 1e6:+,.1f}m</b></span>
<span>unrealised <b class="{'up' if unreal_total > 0 else 'dn'}">Rp{unreal_total / 1e6:+,.1f}m</b></span>
</div></div>

<h2>Open</h2>
<div class="card scroll"><table><thead><tr>
<th>Ticker</th><th>Lots</th><th>Entry</th><th>Mark</th><th>R</th><th>P&amp;L</th>
<th>Stop</th><th title="distance from the mark to the stop">Gap</th>
<th title="a CLOSE below this fires E2">E2 floor</th>
<th title="how many ATR the mark sits above the E2 floor">ATR</th>
<th title="sell this many lots at this price">Scale</th><th>Status</th>
</tr></thead><tbody>{''.join(rows) or '<tr><td colspan="12" class="none">flat</td></tr>'}
</tbody></table></div>
<p class="note">E2 fires only on a <b>close</b> below the 5-session low; an intraday
probe is not a trigger. R is frozen at open and never recomputed from a moved stop.
The scale leg is a measured <b>cost</b> (&minus;32bp), bought for regret control, not an edge.</p>

<h2>Closed</h2>
<div class="card scroll"><table><thead><tr>
<th>Ticker</th><th>Held</th><th>Lots</th><th>Entry</th><th>Exit</th><th>R</th>
<th>P&amp;L</th><th>Why</th></tr></thead><tbody>{closed_rows}</tbody></table></div>

<p class="note">Private. Generated by build_portfolio.py &mdash; do not edit by hand.
Marks are Yahoo, ~15 minutes delayed. This page is a read surface: trades are recorded
in Telegram, where there is a confirmation step.</p>
</div>"""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Position book &mdash; {now:%Y-%m-%d %H:%M}</title>'
            f'<style>{CSS}{EXTRA_CSS}</style></head><body>{body}</body></html>')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--market-hours-only", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    _refuse_public(out)

    state = pb.rebuild()
    if a.market_hours_only:
        ok, why = market_window_ok()
        if not ok:
            print(f"outside the session ({why}) — not regenerating")
            return 0
        if not state["positions"]:
            print("book is flat — nothing to publish")
            return 0

    cfg, _ = config_from_env()
    cfg.equity_idr = state["equity_idr"] or cfg.equity_idr
    p = Panel()
    p.load_prices()
    marks = live_prices.quotes(sorted(state["positions"])) if state["positions"] else {}

    if a.market_hours_only and marks and live_prices.market_looks_closed(marks):
        # Every mark hours old during what the clock calls a session: a public holiday.
        # Publishing a page stamped "live" would be a lie the clock cannot detect.
        print("every mark is stale — the market appears closed (holiday?); not publishing")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(state, p, marks, cfg), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB, "
          f"{len(state['positions'])} open, {len(state['closed'])} closed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
