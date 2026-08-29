#!/usr/bin/env python3
"""The flow-direction board -- thesis #13. Framework: reference/flow-direction.md section 6.

A FOURTH board, and it is NOT VALIDATED. `flowdir_measure.py` passed 1 of its 4 gating checks:
the instrument does not demonstrably beat two prices off the daily bar, its single-session
reliability is below the declared bar, and the desk rate-of-change variable does not exist. The
board still ships, labelled on every row and in the page header, because the point of it is the
forward RECORD it accumulates -- the dataset that would eventually settle those questions does
not exist yet, and cannot start existing until something writes it down.

It never feeds `trade_plan.py` and never sizes a position.

THREE THINGS THIS FILE GUARANTEES

1. **It cannot disturb the momentum board.** md5 of `build_momentum_board.py`,
   `momentum_board.json`, `docs/momentum.html` and `docs/index.html` is taken before anything is
   computed and re-checked before anything is written. A single byte of difference aborts the
   write.
2. **It cannot silently report an absence of DATA as an absence of OPPORTUNITY.** The m5 store
   and the daily panel end on different dates, and the gap is currently more than two weeks. The
   session is chosen as the last one BOTH cover, panel coverage is gated at 50%, and the
   staleness in sessions is printed, written into the JSON and rendered at the top of the page.
   `momentum_board.json` once reported "no candidates" for days because the panel's last session
   held 2 bars of 161.
3. **It cannot look ahead.** Every series is trailing-only: the per-bucket volume seasonal uses
   the previous 60 sessions strictly before the scored one, the within-symbol median likewise,
   and the pooled quintile threshold comes from the previous 60 sessions across all symbols --
   never from the cross-section being scored.

Usage:
    py scripts/build_flow_board.py --dry-run
    py scripts/build_flow_board.py [--date YYYY-MM-DD] [--top 30]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import flowdir_lib as F  # noqa: E402
import intraday_lib as il  # noqa: E402
from alpha_lib import PANEL, Panel, panel_fingerprint  # noqa: E402
from momentum_setup import is_momentum  # noqa: E402
from overlay_test import features  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
INTRADAY = ROOT / "data" / "intraday"
BOARD = PANEL / "flow_board.json"
RECORD = PANEL / "flow_record.csv"
MEASURE = PANEL / "flowdir_measure.json"
SITE = "https://andrebenas77.github.io/idx-telegram-screener/flow.html"

# Thresholds, all from reference/flow-direction.md section 6. Declared before this file existed.
MIN_COVERAGE = 0.50         # same floor build_momentum_board.py uses
QUINTILE = 0.80             # sflow_rel top quintile
POOL_WIN = 60               # trailing sessions the pooled threshold is drawn from
RSI_MIN = 55.0
RVOL_COIL_MAX = 1.5
DD_MIN = -0.10
COIL_RANGE_PCTILE = 1.0 / 3.0

# The four artefacts that must not move. Verified before and after every run.
GUARDED = ["scripts/build_momentum_board.py", "data/panel/momentum_board.json",
           "docs/momentum.html", "docs/index.html"]

RECORD_COLS = ["date", "session_i", "symbol", "cell", "sflow", "sflow_seas", "sflow5",
               "sflow_rel", "d_sflow", "coil", "range_pct", "vol_pctile", "rsi", "cmf20",
               "clv", "rvol5", "dd60", "trend", "close", "adtv", "on_momentum_board",
               "q80_threshold", "panel_fingerprint", "written_at"]


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
        raise SystemExit("REFUSING TO WRITE: these files changed during the run: "
                         + ", ".join(moved))


# --------------------------------------------------------------------------- flow series

def m5_symbols() -> list[str]:
    out = []
    for f in sorted(INTRADAY.glob("m5-*.csv.gz")):
        s = f.name[3:-7]
        if s != "COMPOSITE":
            out.append(s)
    return out


def flow_series(sym: str):
    """(dates, sflow_seas, sflow_raw) for one symbol, trailing-only throughout.

    The seasonal is rebuilt at every session from the previous 60 sessions and never from the
    whole sample: thesis #12 lost its instrument to exactly that look-ahead, dropping from
    rho +0.512 to +0.384 once it was removed.
    """
    store = {d: F.continuous(v) for d, v in il.read_bars(sym).items()}
    dates = sorted(d for d, v in store.items() if len(v) >= 3)
    seas_series, raw_series = [], []
    for k, d in enumerate(dates):
        raw_series.append(F.sflow(store[d]))
        prior = dates[max(0, k - F.SEAS_WIN):k]
        if len(prior) < 20:
            seas_series.append(None)
            continue
        seas = F.trailing_seasonal(store, prior)
        seas_series.append(F.sflow_seasonal(store[d], seas) if seas else None)
    return dates, seas_series, raw_series


def derived(seas_series, k: int):
    """(sflow5, sflow_rel, d_sflow) at position k of the symbol's own m5 date list."""
    s5 = F.mean_k(seas_series, k)
    if s5 is None:
        return None, None, None
    smooth = [F.mean_k(seas_series, j) for j in range(len(seas_series))]
    rel = F.rel_to_median(smooth, k)
    d = F.roc([F.rel_to_median(smooth, j) for j in range(len(smooth))], k)
    return s5, rel, d


# --------------------------------------------------------------------------- classification

def coiled(f: dict, range_cut) -> bool:
    """Price under a lid: a tight 20-day range for this name, quiet volume, near the highs.

    `range_cut` is the bottom tercile of the symbol's OWN range_pct history. An absolute range
    threshold would select the sleepiest large caps every session and call it a signal.
    """
    if f.get("rvol5") is None or f.get("dd60") is None or range_cut is None:
        return False
    return (f["range_pct"] <= range_cut and f["rvol5"] < RVOL_COIL_MAX
            and f["dd60"] >= DD_MIN)


def classify(f: dict, rel, q80, mom: bool, range_cut) -> str | None:
    """CONFIRM / DIVERGENCE / DIVERGENCE-WEAK, per section 6 of the pre-registration.

    DIVERGENCE-WEAK is not a leftover bucket. It is the declared CONTROL for H-C -- the desk
    claim that buying pressure under a lid only pays when RSI and money flow are already strong.
    Without it, a strong-RSI-only result would be the forbidden post-hoc promotion of a
    conditioning variable; with it declared in advance, the comparison is a pre-registered test.
    """
    if rel is None or q80 is None or rel < q80:
        return None
    if mom:
        return "CONFIRM"
    if not coiled(f, range_cut):
        return None
    strong = (f.get("rsi") is not None and f["rsi"] >= RSI_MIN
              and f.get("cmf20") is not None and f["cmf20"] > 0)
    return "DIVERGENCE" if strong else "DIVERGENCE-WEAK"


def range_tercile(p: Panel, sym: str, i: int, look: int = 120):
    """Bottom tercile of this symbol's own 20-day range history, from sessions up to i."""
    vals = []
    cl = p.close.get(sym) or {}
    for j in range(i - look + 1, i + 1):
        w = [cl[t] for t in range(j - 19, j + 1) if t in cl]
        if len(w) < 20 or not cl.get(j):
            continue
        vals.append((max(w) - min(w)) / cl[j])
    if len(vals) < 30:
        return None
    vals.sort()
    return vals[int(COIL_RANGE_PCTILE * (len(vals) - 1))]


# --------------------------------------------------------------------------- how to read

def worked_example(p: Panel, series: dict, session: str, rows: list) -> dict | None:
    """Everything the explainer needs, recomputed from THIS session rather than hardcoded.

    The example is regenerated on every run against whichever name currently tops the board, so
    the page can never end up teaching arithmetic that no longer matches the numbers beside it.
    A worked example that drifts from the thing it explains is worse than none.

    The five-session panel is the load-bearing part: it shows session flow beside the plain
    open-to-close return, which is the fastest way to see that the two are not the same number.
    """
    pick = None
    for want in ("DIVERGENCE", "CONFIRM", "DIVERGENCE-WEAK"):
        for r in rows:
            if r["cell"] == want:
                pick = r
                break
        if pick:
            break
    if not pick or pick["symbol"] not in series:
        return None
    sym = pick["symbol"]
    dates, seas_series, raw_series = series[sym][0], series[sym][1], series[sym][2]
    idx = series[sym][3]
    k = idx.get(session)
    if k is None or k < 4:
        return None

    store = {d: F.continuous(v) for d, v in il.read_bars(sym).items()}
    prior = dates[max(0, k - F.SEAS_WIN):k]
    seas = F.trailing_seasonal(store, prior)
    bars = store.get(session) or []

    sample, num, den = [], 0.0, 0.0
    for b in bars:
        c = F.clv(b.h, b.l, b.c)
        w = seas.get(b.hhmm)
        if c is None or not w or w <= 0 or b.v <= 0:
            continue
        x = 2.0 * c - 1.0
        ww = b.v / w
        num += x * ww
        den += ww
        sample.append({"hhmm": b.hhmm, "h": b.h, "l": b.l, "c": b.c, "clv": c,
                       "x": x, "v": b.v, "w": ww, "contrib": x * ww})
    if len(sample) < 6:
        return None

    five = []
    for j in range(k - 4, k + 1):
        d = dates[j]
        i = p.didx.get(d)
        o = (p.open.get(sym) or {}).get(i) if i is not None else None
        cl = (p.raw_close.get(sym) or {}).get(i) if i is not None else None
        five.append({"date": d, "sflow": seas_series[j],
                     "ret": (cl / o - 1.0) if o and cl and o > 0 else None})

    smooth = [F.mean_k(seas_series, j) for j in range(len(seas_series))]
    prior60 = [x for x in smooth[max(0, k - 60):k] if x is not None]
    med = statistics.median(prior60) if prior60 else None
    return {"symbol": sym, "cell": pick["cell"], "n_bars": len(sample),
            "bars": sample[:2] + sample[-2:], "num": num, "den": den,
            "sflow_s": (num / den if den else None), "five": five,
            "sflow5": pick["sflow5"], "median60": med, "n_prior": len(prior60),
            "rel": pick["sflow_rel"]}


def how_to_read(ex, q80, banner) -> str:
    """The explainer block. Written for someone reading the board cold, three months from now."""
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def n(v, f="%+.3f"):
        return "-" if v is None else f % v

    h = ["<h2>How to read this</h2><div class='card'>"]
    h.append("<p class='note'><b>1. The atom.</b> For every 5-minute bar, "
             "<code>clv = (close &minus; low) / (high &minus; low)</code> &mdash; where in its own "
             "range the bar closed. Rescaled to <code>2&middot;clv &minus; 1</code> it runs "
             "&minus;1 (closed on the low) to +1 (closed on the high). The session figure is the "
             "average of those, weighted by each bar&rsquo;s volume <i>divided by what that time "
             "of day normally does for that stock</i>. IDX volume is U-shaped; without that "
             "normalisation the open and the close would decide every reading.</p>")

    if ex:
        h.append("<p class='note'><b>2. Worked example &mdash; %s, this session, %d usable "
                 "bars.</b></p><div class='scroll'><table><tr><th>time</th>"
                 "<th class='num'>high</th><th class='num'>low</th><th class='num'>close</th>"
                 "<th class='num'>clv</th><th class='num'>2clv&minus;1</th>"
                 "<th class='num'>volume</th><th class='num'>seas. wt</th>"
                 "<th class='num'>contribution</th></tr>"
                 % (esc(ex["symbol"]), ex["n_bars"]))
        for j, b in enumerate(ex["bars"]):
            if j == 2:
                h.append("<tr><td class='dimtx'>&hellip;</td><td colspan='8'></td></tr>")
            h.append("<tr><td>%s</td><td class='num'>%.0f</td><td class='num'>%.0f</td>"
                     "<td class='num'>%.0f</td><td class='num'>%.2f</td>"
                     "<td class='num'>%+.2f</td><td class='num'>%s</td>"
                     "<td class='num'>%.2f</td><td class='num'>%+.2f</td></tr>"
                     % (esc(b["hhmm"]), b["h"], b["l"], b["c"], b["clv"], b["x"],
                        "{:,.0f}".format(b["v"]), b["w"], b["contrib"]))
        h.append("</table></div>")
        h.append("<p class='ev'>sflow&middot;s = sum of contributions / sum of weights = "
                 "<b>%.2f / %.2f = %s</b></p>" % (ex["num"], ex["den"], n(ex["sflow_s"], "%+.4f")))

        h.append("<p class='note'><b>3. It is not the price move.</b> The same five sessions, "
                 "flow beside the plain open-to-close return:</p><div class='scroll'>"
                 "<table><tr><th>date</th><th class='num'>open &rarr; close</th>"
                 "<th class='num'>sflow&middot;s</th></tr>")
        for r in ex["five"]:
            h.append("<tr><td>%s</td><td class='num %s'>%s</td><td class='num %s'>%s</td></tr>"
                     % (esc(r["date"]),
                        "up" if (r["ret"] or 0) > 0 else "dn",
                        "-" if r["ret"] is None else "%+.2f%%" % (100 * r["ret"]),
                        "up" if (r["sflow"] or 0) > 0 else "dn", n(r["sflow"], "%+.3f")))
        h.append("</table></div>")
        h.append("<p class='ev'>A day can rise hard on mild flow (it gapped up, then each bar "
                 "closed near its own low) or fall on strong flow (price pinned, volume printing "
                 "in the upper half of bars). That second case is the divergence this board "
                 "exists to look for.</p>")

        h.append("<p class='note'><b>4. From one session to the sorter.</b> "
                 "<code>sflow5</code> is the mean of those five = <b>%s</b>. "
                 "<code>rel</code> subtracts this stock&rsquo;s <i>own</i> median sflow5 over the "
                 "prior %d sessions (<b>%s</b>), giving <b>%s</b> &mdash; above this "
                 "session&rsquo;s top-quintile cut of %s. <b>rel is the only number that decides "
                 "membership.</b> It asks whether the tape is unusual <i>for this name</i>, not "
                 "whether it is high against other names, which would only rank stocks by their "
                 "permanent character.</p>"
                 % (n(ex["sflow5"], "%+.4f"), ex["n_prior"], n(ex["median60"], "%+.4f"),
                    n(ex["rel"], "%+.4f"), n(q80, "%+.4f")))

    h.append("<p class='note'><b>5. What these numbers do not say.</b></p><ul class='note'>")
    h.append("<li><b>Never read one session.</b> Split a session&rsquo;s bars into odds and "
             "evens and the two halves agree only %s. A single sflow is mostly noise; only the "
             "5-session mean carries much (implied %s), which is why the board sorts on it.</li>"
             % (n(banner.get("reliability_daily"), "%.3f"),
                n(banner.get("reliability_sflow5"), "%.3f")))
    h.append("<li><b>The edge over &ldquo;did it go up&rdquo; is unproven.</b> The instrument "
             "beats the best price-only rival by %s, but the date-clustered 10th percentile is "
             "%s on an effective 20 tape-days. Probably real; not established.</li>"
             % (n(banner.get("increment")), n(banner.get("increment_lo10"))))
    h.append("<li><b>There is no &ldquo;rising&rdquo; reading.</b> The daily level has lag-1 "
             "autocorrelation %s &mdash; white noise. A rel that grew since last week is a "
             "difference-filter artefact, not a build.</li>"
             % n(banner.get("roc_level_acf_lag1")))
    h.append("</ul>")

    h.append("<p class='note'><b>6. This is not evidence of accumulation.</b> Trend (HH/HL), RSI, "
             "CMF, CLV and sflow are <i>all</i> computed from price bars. Higher-high/higher-low "
             "says the price went up. RSI says the price went up. CMF says it closed in the upper "
             "half of <i>daily</i> ranges; sflow says the upper half of <i>5-minute</i> ranges. "
             "Four views of one fact at four resolutions &mdash; stacking them feels like "
             "corroboration and is largely the same evidence counted again. "
             "<code>overlay_test.structure()</code> says as much about CMF in its own docstring: "
             "it infers from where the close sits, and is not a volume-at-price profile.</p>")
    h.append("<p class='note'>For actual accumulation evidence, two things already exist and "
             "neither is on this page: the <b>momentum board&rsquo;s Net accum / %ADTV / Brokers "
             "accumulating</b> columns, which are real rupiah from real broker codes and are "
             "independent of price by construction; and <b>/check in the bot</b>, which is the "
             "vendor&rsquo;s true aggressor flag &mdash; definitive, and 65&ndash;300 requests per "
             "stock-day, which is why it is not a board. This board sits between them: cheaper "
             "than the tape, finer than the daily bar, and unproven.</p>")
    h.append("<p class='note'><b>7. So use rel as a reason to look, never a reason to size.</b> "
             "CONFIRM is the only cell with inherited support, because it sits inside the "
             "validated momentum gate. DIVERGENCE against DIVERGENCE-WEAK is a live experiment: "
             "the weak cell is the declared control, and the comparison answers itself from the "
             "record rather than from argument.</p>")
    h.append("</div>")
    return "".join(h)


# --------------------------------------------------------------------------- rendering

def verdict_banner() -> dict:
    """The gate cascade result, read off disk so the page can never claim more than was measured."""
    try:
        m = json.loads(MEASURE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "UNMEASURED", "failed": None,
                "line": "flowdir_measure.py has not been run; nothing here is validated."}
    failed = m.get("failed") or []
    g = m.get("gates", {})
    g2 = (g.get("gate2_increment") or {}).get("gating") or {}
    g1 = g.get("gate1_reliability") or {}
    g3 = g.get("gate3_roc_exists") or {}
    return {
        "status": "REFUTED" if failed else "DESCRIPTIVE",
        "failed": failed,
        "n_failed": len(failed),
        "reliability_daily": g1.get("reliability_within"),
        "reliability_sflow5": g1.get("reliability_sflow5_implied"),
        "increment": g2.get("increment"), "increment_lo10": g2.get("lo10"),
        "roc_level_acf_lag1": (g3.get("level_acf_lag1_5") or [None])[0],
        "line": ("%d of 4 gating checks failed. The instrument does not demonstrably beat two "
                 "prices off the daily bar, and the rate-of-change variable does not exist. "
                 "Descriptive only." % len(failed)) if failed else
               "All gating checks passed. Still unvalidated for return: no forward record yet.",
    }


def render(session, stale, rows, banner, counts, q80, m5_share=None, example=None) -> str:
    from build_momentum_board import CSS      # one stylesheet, not two that drift apart

    def esc(x):
        return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def num(v, fmt="%.2f"):
        return "-" if v is None else fmt % v

    head = ["<div class='wrap'>",
            "<h1>IDX flow-direction board</h1>",
            "<p class='sub'>Session %s &middot; thesis #13 &middot; signed aggressor pressure "
            "from 5-minute bars</p>" % esc(session)]

    head.append("<div class='card warn'><b>NOT VALIDATED &mdash; %s.</b><p class='note'>%s</p>"
                % (esc(banner["status"]), esc(banner["line"])))
    bits = []
    if banner.get("reliability_daily") is not None:
        bits.append("single-session reliability %.3f (bar 0.30); the 5-session sorter implies "
                    "%.3f" % (banner["reliability_daily"], banner["reliability_sflow5"] or 0))
    if banner.get("increment") is not None:
        bits.append("increment over the best price-only rival %+.3f, date-clustered 10th "
                    "percentile %+.3f" % (banner["increment"], banner["increment_lo10"] or 0))
    if banner.get("roc_level_acf_lag1") is not None:
        bits.append("lag-1 autocorrelation of the daily level %+.3f, so there is no build to "
                    "differentiate and no rate-of-change column is shown"
                    % banner["roc_level_acf_lag1"])
    if bits:
        head.append("<p class='ev'>" + esc("; ".join(bits)) + ".</p>")
    head.append("<p class='note'>This board never feeds the trade plan and never sizes a "
                "position. It exists to accumulate a forward record.</p></div>")

    if stale > 0:
        head.append("<div class='card warn'><b>STALE INPUT.</b><p class='note'>The 5-minute "
                    "store ends %s sessions before the daily panel does. This board scores the "
                    "last session BOTH cover at full breadth, so it is %s sessions behind the "
                    "momentum board. %s of the m5 universe printed bars on this session. "
                    "Refresh the m5 store before reading it as current.</p></div>"
                    % (stale, stale,
                       "unknown share" if m5_share is None else "%.0f%%" % (100 * m5_share)))

    head.append("<div class='card'><p class='note'>Cells are pre-registered. "
                "<b>CONFIRM</b>: on the momentum board with flow in the top quintile. "
                "<b>DIVERGENCE</b>: price under a lid, flow in the top quintile, RSI &ge; 55 and "
                "CMF &gt; 0. <b>DIVERGENCE-WEAK</b>: the same without the RSI/CMF leg &mdash; it "
                "is the declared control, not a weaker signal. Top-quintile cut this session "
                "%s.</p></div>" % num(q80, "%+.4f"))

    body = list(head)
    for cell in ("CONFIRM", "DIVERGENCE", "DIVERGENCE-WEAK"):
        sel = [r for r in rows if r["cell"] == cell]
        body.append("<h2>%s <span class='dimtx' style='font-size:13px'>(%d)</span></h2>"
                    % (esc(cell), len(sel)))
        if not sel:
            body.append("<p class='none'>none this session</p>")
            continue
        body.append("<div class='card scroll'><table><tr>"
                    "<th>Ticker</th><th class='num'>Close</th>"
                    "<th class='num' title='seasonally normalised session flow'>sflow&middot;s</th>"
                    "<th class='num'>sflow5</th><th class='num'>rel</th>"
                    "<th class='num'>RSI</th><th class='num'>CMF</th>"
                    "<th class='num'>RVOL5</th><th class='num'>DD60</th>"
                    "<th class='num'>range%</th><th>Trend</th><th>Status</th></tr>")
        # sflow.s is the SEASONALLY NORMALISED session value, which is what the board sorts on
        # via sflow5 and sflow_rel. The raw volume-weighted figure is kept in flow_record.csv
        # under `sflow`; showing one under the other name would misreport the sorter.

        for r in sel:
            body.append(
                "<tr><td class='tk'>%s</td><td class='num'>%s</td><td class='num'>%s</td>"
                "<td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td>"
                "<td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td>"
                "<td class='num'>%s</td><td class='tr'>%s</td>"
                "<td class='dimtx' style='font-size:11px'>%s</td></tr>"
                % (esc(r["symbol"]), num(r["close"], "%.0f"),
                   num(r["sflow_seas"], "%+.3f"), num(r["sflow5"], "%+.3f"),
                   num(r["sflow_rel"], "%+.3f"), num(r["rsi"], "%.0f"),
                   num(r["cmf20"], "%+.2f"), num(r["rvol5"], "%.2f"),
                   num(r["dd60"], "%+.3f"),
                   num(r["range_pct"], "%.3f"), esc(r.get("trend") or "-"),
                   esc(banner["status"])))
        body.append("</table></div>")

    body.append(how_to_read(example, q80, banner))
    body.append("<p class='note'>Counts: " + esc(json.dumps(counts)) + "</p>")
    body.append("<p class='note'>Every row is written to <code>flow_record.csv</code> with no "
                "outcome attached. Forward returns are joined later, so the record cannot look "
                "ahead. Run <code>flowdir_power.py</code> to see when it may be read.</p>")
    body.append("<p class='note'>%s</p></div>" % esc(SITE))
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>IDX flow-direction board</title>"
            "<style>%s</style></head><body>%s</body></html>" % (CSS, "".join(body)))


# --------------------------------------------------------------------------- record

def append_record(rows, session, i, fp) -> int:
    """Append one row per candidate. Append-only, de-duplicated on (date, symbol).

    No outcome is written. Forward returns are joined later from the panel, which is what makes
    look-ahead impossible by construction rather than by discipline.
    """
    seen = set()
    if RECORD.exists():
        with RECORD.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                seen.add((r.get("date"), r.get("symbol")))
    new = [r for r in rows if (session, r["symbol"]) not in seen]
    if not new:
        return 0
    fresh = not RECORD.exists()
    with RECORD.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=RECORD_COLS, extrasaction="ignore")
        if fresh:
            w.writeheader()
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in new:
            r = dict(r)
            r.update({"date": session, "session_i": i, "panel_fingerprint": fp,
                      "written_at": stamp})
            w.writerow(r)
    return len(new)


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="render and report, write nothing")
    a = ap.parse_args()

    before = fingerprints()
    missing = [k for k, v in before.items() if v is None]
    if missing:
        print("[warn] guarded file absent: " + ", ".join(missing))

    p = Panel()
    p.load_prices()
    syms = m5_symbols()
    print("panel %d symbols, %d sessions | m5 store %d symbols"
          % (len(p.close), len(p.dates), len(syms)))

    # ---- series first, so the session can be chosen from what actually exists
    series = {}
    for s in syms:
        dates, seas, raw = flow_series(s)
        if dates:
            series[s] = (dates, seas, raw, {d: k for k, d in enumerate(dates)})
    m5_last = max((v[0][-1] for v in series.values()), default=None)
    print("m5 last session %s | panel last session %s" % (m5_last, p.dates[-1]))

    # How many of the m5-store symbols actually printed bars on a given date. The daily panel
    # and the m5 store thin out on DIFFERENT dates, so a panel-only coverage gate is not enough:
    # the first dry-run of this script selected 2026-08-19 and scored 2 symbols of 111, because
    # exactly two names had been refreshed past 2026-08-10. That is an absence of DATA wearing
    # the costume of an absence of OPPORTUNITY, and it is the same failure momentum_board.json
    # shipped for days on a panel session holding 2 bars of 161.
    m5_cov = {}
    for _s, (_d, _q, _r, _ix) in series.items():
        for _dd in _d:
            m5_cov[_dd] = m5_cov.get(_dd, 0) + 1
    n_m5 = max(len(series), 1)

    # ---- choose a session both stores cover, with real panel coverage behind it
    def coverage(d):
        i = p.didx.get(d)
        if i is None or not p.close:
            return None
        return sum(1 for s in p.close if i in p.close[s]) / len(p.close)

    session = a.date
    if session is None:
        for d in reversed(p.dates):
            if m5_last and d > m5_last:
                continue
            cov = coverage(d)
            if cov is None or cov < MIN_COVERAGE:
                continue
            if m5_cov.get(d, 0) / n_m5 < MIN_COVERAGE:
                continue
            session = d
            break
    if session is None or session not in p.didx:
        print("[!!] no session is covered by both the panel and the m5 store")
        return 2
    i = p.didx[session]
    stale = len(p.dates) - 1 - i
    m5_share = m5_cov.get(session, 0) / n_m5
    print("scoring %s (panel index %d, panel coverage %.0f%%, m5 coverage %.0f%% of %d symbols, "
          "%d sessions behind the panel)"
          % (session, i, 100 * (coverage(session) or 0), 100 * m5_share, n_m5, stale))
    if m5_share < MIN_COVERAGE:
        print("[!!] m5 coverage below the %.0f%% floor on the requested date -- the board would "
              "report an absence of data as an absence of opportunity" % (100 * MIN_COVERAGE))
        return 2

    # ---- pooled top-quintile threshold from the PREVIOUS 60 sessions, never this one
    pool = []
    for s, (dates, seas, raw, idx) in series.items():
        smooth = [F.mean_k(seas, j) for j in range(len(seas))]
        for k, d in enumerate(dates):
            if d >= session:
                continue
            if len(dates) - 1 - k > POOL_WIN * 2:
                continue
            v = F.rel_to_median(smooth, k)
            if v is not None:
                pool.append(v)
    q80 = None
    if len(pool) >= 200:
        pool.sort()
        q80 = pool[int(QUINTILE * (len(pool) - 1))]
    print("pooled top-quintile cut from %d trailing observations: %s"
          % (len(pool), "n/a" if q80 is None else "%+.4f" % q80))

    # ---- score
    rows, counts = [], {"CONFIRM": 0, "DIVERGENCE": 0, "DIVERGENCE-WEAK": 0,
                        "eligible": 0, "no_features": 0, "no_flow": 0}
    for s, (dates, seas, raw, idx) in series.items():
        k = idx.get(session)
        if k is None:
            continue
        f = features(p, s, i)
        if f is None:
            counts["no_features"] += 1
            continue
        s5, rel, d = derived(seas, k)
        if rel is None:
            counts["no_flow"] += 1
            continue
        counts["eligible"] += 1
        mom = is_momentum(f, 1.5, DD_MIN, RSI_MIN, 3.0)
        cut = range_tercile(p, s, i)
        cell = classify(f, rel, q80, mom, cut)
        if cell is None:
            continue
        counts[cell] += 1
        rows.append({"symbol": s, "cell": cell, "sflow": raw[k], "sflow_seas": seas[k],
                     "sflow5": s5, "sflow_rel": rel, "d_sflow": d,
                     "coil": coiled(f, cut), "range_pct": f.get("range_pct"),
                     "vol_pctile": f.get("vol_pctile"), "rsi": f.get("rsi"),
                     "cmf20": f.get("cmf20"), "clv": f.get("clv"), "rvol5": f.get("rvol5"),
                     "dd60": f.get("dd60"), "trend": f.get("trend"),
                     "close": (p.raw_close.get(s) or {}).get(i),
                     "adtv": (p.adtv.get(s) or {}).get(i),
                     "on_momentum_board": mom, "q80_threshold": q80})
    rows.sort(key=lambda r: (-(r["sflow_rel"] or 0)))
    rows = rows[:a.top] if a.top else rows

    banner = verdict_banner()
    print("cells: " + json.dumps(counts))
    print("verdict: %s -- %s" % (banner["status"], banner["line"]))
    for r in rows[:10]:
        print("   %-6s %-16s rel %+0.3f  sflow5 %+0.3f  RSI %3.0f  CMF %+0.2f  RVOL %.2f"
              % (r["symbol"], r["cell"], r["sflow_rel"] or 0, r["sflow5"] or 0,
                 r["rsi"] or 0, r["cmf20"] or 0, r["rvol5"] or 0))

    if a.dry_run:
        assert_unmoved(before)
        print("\n--dry-run: nothing written. Guarded files verified unchanged.")
        return 0

    assert_unmoved(before)
    fp = (panel_fingerprint() or {}).get("sha")
    example = worked_example(p, series, session, rows)
    page = render(session, stale, rows, banner, counts, q80, m5_share, example)
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "flow.html").write_text(page, encoding="utf-8")
    BOARD.write_text(json.dumps(
        {"session": session, "generated_at": datetime.now(timezone.utc).isoformat(),
         "validated": False, "verdict": banner, "panel_fingerprint": panel_fingerprint(),
         "sessions_behind_panel": stale, "q80_threshold": q80,
         "m5_coverage": m5_share, "m5_last_session": m5_last, "panel_last_session": p.dates[-1],
         "n_pool_observations": len(pool), "counts": counts, "rows": rows},
        ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    n = append_record(rows, session, i, fp)
    assert_unmoved(before)

    print("\nwrote %s" % (DOCS / "flow.html"))
    print("wrote %s" % BOARD)
    print("appended %d new rows to %s" % (n, RECORD))
    print("guarded files verified byte-identical before and after.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
