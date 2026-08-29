"""V0 — MEASUREMENT VALIDATION. The kill switch. Framework: reference/vpin.md section 5.

Validate the instrument before testing the hypothesis. Andersen & Bondarenko's charge is that
Bulk Volume Classification misclassifies more as volatility rises, that the misclassification
mechanically inflates the imbalance, and that the imbalance IS VPIN -- so BV-VPIN tracks
volatility BY CONSTRUCTION. They also find BVC inferior to a plain tick rule, and that BV-VPIN
and TR-VPIN can move in OPPOSITE directions.

IDX gives us something most markets do not: the vendor stamps the real aggressor side on every
print of a closed session. So the charge is directly testable here rather than assumed.

V0 is the ONLY fully-powered stage of this study. It correlates measures computed on the SAME
ticker-days, so it does not depend on the calendar-block count that limits V2. That asymmetry is
why it runs first and why it can give a clean answer.

KILL if Spearman(BV, TR) < 0.60, or if BV correlates more strongly with realized volatility than
with TR. Either reproduces Andersen-Bondarenko on IDX and voids the full-panel study.

Usage:
    py vpin_validate.py --plan                 # selection + cost estimate, spends NOTHING
    py vpin_validate.py --fetch --max-pages 120
    py vpin_validate.py --json ../data/panel/vpin_v0.json
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import intraday_lib as il
import tape_lib as tl
import vpin_lib as V
from alpha_lib import PANEL, Panel, panel_fingerprint

BUCKETS_PER_DAY = 24        # matches timeframe=15, the primary panel in vpin.md section 4
PAGE_ROWS = 150             # running-trade hard page size
ROLL_SESSIONS = 20          # trailing window for the rolling-sigma BVC arm


def load_freq(p: Panel) -> dict[tuple[str, str], float]:
    """{(sym, date): total prints} from the gross panel's buy_freq + sell_freq.

    Used to COST a tape pull before making it. Each side's freq counts the same prints from
    the other end, so total prints ~ sum(buy_freq) ~ sum(sell_freq); we take buy_freq. Without
    this the only way to learn a session is 300 pages is to spend 300 pages finding out.
    """
    out: dict[tuple[str, str], float] = defaultdict(float)
    for path in sorted(PANEL.glob("gross-*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    out[(r["symbol"], r["date"])] += float(r["buy_freq"] or 0)
                except (TypeError, ValueError):
                    pass
    return dict(out)


def select(p: Panel, freq: dict, m5_syms: set[str], n_per_stratum: int,
           max_pages: int) -> list[tuple[str, str, float, float]]:
    """Stratified sample of ticker-days: QUIET / NORMAL / VIOLENT by |daily return|.

    Both ends matter. The whole charge against BVC is that its error scales with volatility,
    so a sample drawn only from liquid calm days could not detect the failure it is looking
    for, and a sample drawn only from violent days could not tell BVC's error from a real
    toxicity spike.

    Costed and capped: a session estimated above `max_pages` is skipped, because one PTRO-like
    name runs past 300 pages and would eat the budget for the other 29.
    """
    rows = []
    for sym in sorted(m5_syms):
        closes = p.close.get(sym, {})
        idxs = sorted(closes)
        for n in range(1, len(idxs)):
            i, j = idxs[n - 1], idxs[n]
            date = p.dates[j]
            k = (sym, date)
            if k not in freq:
                continue
            est_pages = freq[k] / PAGE_ROWS
            if est_pages < 3 or est_pages > max_pages:
                continue          # too thin to bucket, or too expensive to justify
            c0, c1 = closes[i], closes[j]
            if c0 <= 0:
                continue
            rows.append((sym, date, abs(c1 / c0 - 1.0), est_pages))
    if not rows:
        return []
    rows.sort(key=lambda r: r[2])
    lo, hi = rows[: len(rows) // 3], rows[-len(rows) // 3:]
    mid = rows[len(rows) // 3: -len(rows) // 3]

    def spread(pool, k):
        """Even stride through the pool, so one symbol cannot dominate a stratum."""
        if not pool:
            return []
        step = max(1, len(pool) // k)
        picked, seen = [], defaultdict(int)
        for r in pool[::step]:
            if seen[r[0]] >= 2:
                continue
            picked.append(r)
            seen[r[0]] += 1
            if len(picked) >= k:
                break
        return picked

    return spread(lo, n_per_stratum) + spread(mid, n_per_stratum) + spread(hi, n_per_stratum)


def truncated(prints: list[dict], expected_prints: float) -> bool:
    """Did the tape pull stop early and get CACHED as if it were complete?

    running_trade_all() breaks the page loop on any falsy payload — a transient API failure
    mid-pagination therefore yields a partial tape, and the partial tape is then written to
    the per-(symbol,date) gz store. On every subsequent read it looks exactly like a
    complete session, and it is NOT harmless: a truncated tape covers only the opening
    minutes, where flow is most one-sided, so it scores as maximally toxic. In this run the
    three truncated sessions produced TR = 0.975 / 0.780 / 0.794 against a clean-sample
    median of 0.398, and they dragged Spearman(tick,TR) from +0.559 down to +0.210.

    Detected against the gross panel's own print count rather than a magic threshold. Half
    is deliberately lenient: RG-only filtering and NG exclusion mean the tape legitimately
    carries fewer prints than buy_freq implies.
    """
    return bool(expected_prints) and len(prints) < 0.5 * expected_prints


def tr_daily(prints: list[dict]) -> tuple[float | None, float]:
    """Mean bucket imbalance for one session from the REAL aggressor flag. Ground truth.

    RG only. NG is negotiated/crossed volume — pre-arranged, not liquidity taken from a book —
    so counting it as aggressive flow would inject a large one-sided block into a bucket and
    read as maximal toxicity on what is actually the least informative trade of the day.
    """
    rg = [x for x in prints if x.get("board") == "RG"]
    tot = sum(x["volume"] for x in rg)
    if tot <= 0:
        return None, 0.0
    bars = [(0.0, x["volume"]) for x in rg]           # volume only; sides come from the flag
    size = tot / BUCKETS_PER_DAY
    # walk the same clock by hand so each piece keeps its own print's aggressor
    buckets, cur, filled = [], [], 0.0
    for x in rg:
        rem = x["volume"]
        while rem > 0:
            take = min(size - filled, rem)
            cur.append((x["aggressor"], take))
            filled += take
            rem -= take
            if filled >= size - 1e-9:
                buckets.append(cur)
                cur, filled = [], 0.0
    ibs = [V.imbalance(*V.true_split(b)) for b in buckets]    # drop the partial tail
    ibs = [x for x in ibs if x is not None]
    return (statistics.fmean(ibs) if ibs else None), tot


def bar_daily(bars, prior_rel: list[float] | None = None) -> tuple:
    """(BV_session, BV_rolling, tick, realized vol, total volume) for one session.

    TWO BVC arms, declared in vpin.md section 4a before any result was seen:
      * BV_session uses sigma estimated WITHIN the session — the charitable implementation,
        and the one a screener would ship, because it adapts to the day.
      * BV_rolling uses sigma from a trailing 20-session window — the FAITHFUL reproduction
        of the estimator Andersen & Bondarenko attack, because an out-of-sample sigma cannot
        adapt and therefore lets their misclassification mechanism operate.
    If the two disagree, that disagreement IS the effect, measured on IDX.
    """
    closes = [b.c for b in bars]
    vols = [b.v for b in bars]
    tot = sum(vols)
    if tot <= 0 or len(closes) < 3:
        return None, None, None, 0.0
    dps = V.bar_deltas(closes)
    pairs = list(zip(dps, vols))
    sig = V.sigma_dp(pairs)
    buckets = V.volume_buckets(pairs, tot / BUCKETS_PER_DAY)
    if len(buckets) > BUCKETS_PER_DAY:
        buckets = buckets[:BUCKETS_PER_DAY]            # drop the partial tail
    lvl = statistics.fmean(closes)
    sig_roll = V.sigma_rolling(prior_rel or [], lvl)
    bv = [V.imbalance(*V.bvc_split(b, sig)) for b in buckets]
    bvr = ([V.imbalance(*V.bvc_split(b, sig_roll)) for b in buckets]
           if sig_roll > 0 else [])
    tk, carry = [], 1
    for b in buckets:
        x, y, carry = V.tick_rule_split(b, carry)
        tk.append(V.imbalance(x, y))
    bv = [x for x in bv if x is not None]
    bvr = [x for x in bvr if x is not None]
    tk = [x for x in tk if x is not None]
    return ((statistics.fmean(bv) if bv else None),
            (statistics.fmean(bvr) if bvr else None),
            (statistics.fmean(tk) if tk else None),
            V.realized_vol(closes), tot)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="select + cost only, spend nothing")
    ap.add_argument("--fetch", action="store_true", help="actually pull tape")
    ap.add_argument("--per-stratum", type=int, default=10)
    ap.add_argument("--max-pages", type=int, default=120)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()

    p = Panel().load()
    m5 = {f.name[3:-7] for f in (Path(__file__).resolve().parents[1] / "data" / "intraday")
          .glob("m5-*.csv.gz")} - {"COMPOSITE"}
    freq = load_freq(p)
    print(f"panel {len(p.close)} syms x {len(p.dates)} sessions | m5 symbols {len(m5)} "
          f"| gross ticker-days {len(freq):,}")

    picks = select(p, freq, m5, a.per_stratum, a.max_pages)
    if not picks:
        print("no ticker-days satisfy the cost band", file=sys.stderr)
        return 2
    est = sum(x[3] for x in picks)
    print(f"\nselected {len(picks)} ticker-days | estimated {est:,.0f} pages "
          f"({est / 30000:.1%} of monthly quota)")
    for sym, date, mv, pg in picks:
        print(f"    {sym:6} {date}  |ret| {mv:6.2%}  ~{pg:5.0f} pages")
    if a.plan or not a.fetch:
        print("\n--plan: nothing fetched. Re-run with --fetch to spend.")
        return 0

    # Same construction as fetch_intraday.client(): the browser UA and the IPv4 pin are
    # both load-bearing — Cloudflare answers 1010 to the default UA, and IPv6 to
    # api.invezgo.com is blackholed on this machine (43s/request vs 0.16s).
    # use_cache=False: running_trade_all keeps its OWN per-(symbol,date) gz store and
    # passes no_store=True, because the day-scoped JSON cache rewrites the whole file per
    # request and goes quadratic at tape scale.
    from invezgo_client import InvezgoClient
    from intraday_lib import BROWSER_UA, pin_ipv4
    pin_ipv4()
    c = InvezgoClient(user_agent=BROWSER_UA, use_cache=False, verbose=False)
    rows = []
    for sym, date, mv, pg in picks:
        raw = c.running_trade_all(sym, date, max_pages=a.max_pages)
        prints = tl.parse_prints(raw)
        if not prints:
            print(f"    {sym} {date}: no usable prints — skipped")
            continue
        if truncated(prints, freq.get((sym, date), 0)):
            store = (Path(__file__).resolve().parents[1] / "data" / "tape"
                     / date[:7] / f"{sym}-{date}.json.gz")
            store.unlink(missing_ok=True)
            print(f"    {sym:6} {date}  TRUNCATED ({len(prints):,} prints vs "
                  f"~{freq.get((sym, date), 0):,.0f} expected) — cache deleted, "
                  f"re-run to refetch")
            continue
        tr, tvol = tr_daily(prints)
        allb = il.read_bars(sym)
        bars = allb.get(date) or []
        # trailing window, STRICTLY before the session being classified — a rolling sigma
        # that peeked at today would defeat the whole point of the arm.
        prior_dates = [d for d in sorted(allb) if d < date][-ROLL_SESSIONS:]
        prior_rel = [x for d in prior_dates
                     for x in V.rel_deltas([b.c for b in allb[d]])]
        bv, bvr, tk, rv, bvol = bar_daily(bars, prior_rel)
        if None in (tr, bv, tk, rv):
            print(f"    {sym} {date}: incomplete (tr={tr} bv={bv} tk={tk} rv={rv}) — skipped")
            continue
        rows.append({"sym": sym, "date": date, "abs_ret": mv, "tr": tr, "bv": bv,
                     "bv_roll": bvr, "tick": tk, "rvol": rv, "tape_vol": tvol,
                     "bar_vol": bvol, "n_prior_sessions": len(prior_dates),
                     "vol_ratio": (tvol / bvol) if bvol else None, "n_prints": len(prints)})
        print(f"    {sym:6} {date}  TR {tr:.3f}  BVses {bv:.3f}  "
              f"BVroll {bvr if bvr is None else format(bvr, '.3f')}  tick {tk:.3f}  "
              f"rvol {rv:.4f}  prints {len(prints):,}")

    if len(rows) < 5:
        print("\ntoo few complete ticker-days to correlate", file=sys.stderr)
        return 2

    rows_r = [r for r in rows if r.get("bv_roll") is not None]
    tr = [r["tr"] for r in rows]
    bv = [r["bv"] for r in rows]
    tk = [r["tick"] for r in rows]
    rv = [r["rvol"] for r in rows]
    res = {
        "n": len(rows),
        "spearman_bv_tr": V.spearman(bv, tr),
        "spearman_tick_tr": V.spearman(tk, tr),
        "spearman_bv_rvol": V.spearman(bv, rv),
        "spearman_tr_rvol": V.spearman(tr, rv),
        "spearman_tick_rvol": V.spearman(tk, rv),
        "n_roll": len(rows_r),
        "spearman_bvroll_tr": V.spearman([r["bv_roll"] for r in rows_r],
                                         [r["tr"] for r in rows_r]),
        "spearman_bvroll_rvol": V.spearman([r["bv_roll"] for r in rows_r],
                                           [r["rvol"] for r in rows_r]),
        "spearman_bvroll_bvses": V.spearman([r["bv_roll"] for r in rows_r],
                                            [r["bv"] for r in rows_r]),
    }
    print(f"\n=== V0 RESULT (n={res['n']} ticker-days) ===")
    print(f"    Spearman(BV,   TR)    {res['spearman_bv_tr']:+.3f}   <- the validation")
    print(f"    Spearman(tick, TR)    {res['spearman_tick_tr']:+.3f}   <- lit says this beats BVC")
    print(f"    Spearman(BV,   rvol)  {res['spearman_bv_rvol']:+.3f}   <- the accusation")
    print(f"    Spearman(TR,   rvol)  {res['spearman_tr_rvol']:+.3f}")
    print(f"    Spearman(tick, rvol)  {res['spearman_tick_rvol']:+.3f}")
    if res["n_roll"] >= 5:
        def fmt(x): return "  n/a " if x is None else f"{x:+.3f}"
        print("")
        print(f"    --- rolling-sigma arm (the faithful A&B estimator), "
              f"n={res['n_roll']}")
        print(f"    Spearman(BVroll, TR)    {fmt(res['spearman_bvroll_tr'])}")
        print(f"    Spearman(BVroll, rvol)  {fmt(res['spearman_bvroll_rvol'])}"
              f"   <- the accusation, unneutralised")
        print(f"    Spearman(BVroll, BVses) {fmt(res['spearman_bvroll_bvses'])}"
              f"   <- do the two sigma choices even agree?")

    verdict = []
    if res["spearman_bv_tr"] is None or res["spearman_bv_tr"] < 0.60:
        verdict.append(f"KILL: Spearman(BV,TR) = {res['spearman_bv_tr']} < 0.60 — "
                       "bar-based VPIN does not track true toxicity on IDX")
    if (res["spearman_bv_rvol"] or 0) > (res["spearman_bv_tr"] or 0):
        verdict.append("KILL: BV-VPIN tracks realized VOLATILITY more closely than it tracks "
                       "true aggressor VPIN — Andersen-Bondarenko reproduced on IDX")
    print("\n=== V0 VERDICT ===")
    print("\n".join(verdict) if verdict else
          "PASS - the cheap arm tracks ground truth; proceed to V1.")

    if a.json:
        a.json.write_text(json.dumps(
            {"panel_fingerprint": panel_fingerprint(), "buckets_per_day": BUCKETS_PER_DAY,
             "rows": rows, "result": res, "verdict": verdict or ["PASS"]}, indent=1),
            encoding="utf-8")
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
