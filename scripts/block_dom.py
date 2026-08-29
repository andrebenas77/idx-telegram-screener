#!/usr/bin/env python3
"""H2/H3 — block dominance and retail churn: does WHO is buying sort returns?

Thesis #10. Framework and PASS BAR pre-registered in `reference/blockdom.md`, written before
this file existed.

    H2  stock-days bought in LARGE tickets outperform stock-days bought in many small ones
    H3  stock-days with a high share of HIGH-FREQUENCY small-ticket buying underperform

H3 is not the negation of H2: H2 asks whether block buyers help, H3 whether churn hurts. A
day can have both and on IDX usually does.

THE CEILING IS DECLARED BEFORE THE NUMBERS. The gross panel is 109 sessions ~= 4 blocks
against MIN_BLOCKS_INFERENTIAL = 15, so every result here is DESCRIPTIVE. A pass cannot
ship; it can only justify paying to extend the panel. A fail is a real refutation.

Conditioning on momentum candidates is FORBIDDEN (blockdom.md sec 5): n~236 in this window,
MDE ~3.9pp against a 1.37pp base effect.

Reads the panel and the gross partition; touches nothing the momentum board imports.

    py scripts/block_dom.py
    py scripts/block_dom.py --quick
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_lib import PANEL, Panel, panel_fingerprint  # noqa: E402
import lift_lib as LL  # noqa: E402
import trade_backtest as TB  # noqa: E402

N_QUINTILES = 5
HORIZONS = (3, 5, 10)
PRIMARY_K = 5
TICKET_LOOKBACK = 20
TICKET_MIN_HIST = 10

BAR_Q5MQ1_PP = 1.0
BAR_MONOTONE_STEPS = 4
BAR_NULL_PP = 0.3
BAR_FOLDS = 3
BAR_CHECK0 = 0.005
MIN_FOLD_N = 50

# reference/brokers.csv behavioural taxonomy, group == "retail"
RETAIL = {"XL", "XC", "YP", "KK", "XA"}

FEATURES = (
    ("H2_ticket_rel", "ticket_rel", +1),   # higher ticket = better, per H2
    ("H3_churn", "churn_share", -1),       # higher churn  = worse,  per H3
    ("H3b_retail", "retail_share", -1),    # robustness, identity-based
)


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ gross panel

def load_gross() -> dict[tuple[str, str], dict]:
    """{(date, symbol): aggregates} from data/panel/gross-*.csv.gz.

    buy_value is IDR and buy_freq is a trade COUNT, so their ratio is the average ticket.
    Verified on a real row: 12 lots x 100 shares x 9,275 = 11,130,000 = buy_value.
    """
    per: dict[tuple[str, str], list] = {}
    for f in sorted(glob.glob(str(PANEL / "gross-*.csv.gz"))):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    bv, bf = float(r["buy_value"]), float(r["buy_freq"])
                except (TypeError, ValueError):
                    continue
                if bv <= 0 or bf <= 0:
                    continue
                per.setdefault((r["date"], r["symbol"]), []).append((r["broker"], bv, bf))

    out = {}
    for key, brokers in per.items():
        tot_v = sum(b[1] for b in brokers)
        tot_f = sum(b[2] for b in brokers)
        if tot_v <= 0 or tot_f <= 0:
            continue
        churn_v = 0.0
        for code, bv, bf in brokers:
            # slice_z = ln(freq_share / value_share); positive = spending more trades than
            # rupiah share implies = the crowd (accumulation.md sec 7b, sign already corrected)
            z = math.log((bf / tot_f) / (bv / tot_v))
            if z > 0:
                churn_v += bv
        retail_v = sum(bv for code, bv, _ in brokers if code in RETAIL)
        out[key] = {
            "ticket": tot_v / tot_f,
            "churn_share": churn_v / tot_v,
            "retail_share": retail_v / tot_v,
            "n_brokers": len(brokers),
            "tot_bval": tot_v,
        }
    return out


def build_rows(p: Panel, gross: dict) -> list[dict]:
    """Attach ticket_rel (stock-normalised) and forward returns."""
    di = {d: i for i, d in enumerate(p.dates)}
    by_sym: dict[str, list[tuple[int, dict]]] = {}
    for (date, sym), agg in gross.items():
        i = di.get(date)
        if i is None:
            continue
        by_sym.setdefault(sym, []).append((i, agg))

    rows = []
    for sym, obs in by_sym.items():
        obs.sort(key=lambda t: t[0])
        for n, (i, agg) in enumerate(obs):
            hist = [o[1]["ticket"] for o in obs[max(0, n - TICKET_LOOKBACK):n]]
            if len(hist) < TICKET_MIN_HIST:
                continue
            med = statistics.median(hist)
            if med <= 0:
                continue
            row = {"symbol": sym, "i": i, "ticket_rel": agg["ticket"] / med,
                   "churn_share": agg["churn_share"], "retail_share": agg["retail_share"],
                   "ticket": agg["ticket"]}
            ok = False
            for k in HORIZONS:
                x = p.excess_return(sym, i, k, entry_lag=1)
                row[f"x{k}"] = x
                if k == PRIMARY_K and x is not None:
                    ok = True
            if ok:
                rows.append(row)
    return rows


# ------------------------------------------------------------------ shared stats

def breakpoints(values: list[float], n: int = N_QUINTILES) -> list[float]:
    s = sorted(values)
    return [s[int(len(s) * j / n)] for j in range(1, n)]


def bucket(score: float, cuts: list[float]) -> int:
    q = 1
    for c in cuts:
        if score >= c:
            q += 1
    return min(q, N_QUINTILES)


def q5_minus_q1(obs: list[tuple]) -> float | None:
    hi = [x for q, x in obs if q == N_QUINTILES]
    lo = [x for q, x in obs if q == 1]
    if not hi or not lo:
        return None
    return (statistics.fmean(hi) - statistics.fmean(lo)) * 100.0


def gradient(rows: list[dict], qkey: str, k: int) -> list[dict]:
    out = []
    for q in range(1, N_QUINTILES + 1):
        xs = [r[f"x{k}"] for r in rows if r.get(qkey) == q and r.get(f"x{k}") is not None]
        out.append({"q": q, "n": len(xs),
                    "mean_pp": (statistics.fmean(xs) * 100) if xs else None,
                    "median_pp": (statistics.median(xs) * 100) if xs else None,
                    "hit": (sum(1 for x in xs if x > 0) / len(xs)) if xs else None})
    return out


def monotone_steps(grad: list[dict]) -> int:
    steps = 0
    for a, b in zip(grad, grad[1:]):
        if a["mean_pp"] is None or b["mean_pp"] is None:
            continue
        if b["mean_pp"] >= a["mean_pp"]:
            steps += 1
    return steps


def feature_shift_null(rows: list[dict], field: str, sign: int, k: int,
                       draws: int, seed: int = 7) -> dict:
    """Shift each symbol's feature series against its own returns. See effort.py for why the
    label-shuffle null is not run."""
    by_sym: dict[str, list[dict]] = {}
    for r in rows:
        if r.get(f"x{k}") is not None:
            by_sym.setdefault(r["symbol"], []).append(r)
    for v in by_sym.values():
        v.sort(key=lambda r: r["i"])

    rng = random.Random(seed)
    vals = []
    for _ in range(draws):
        shifted = []
        for ser in by_sym.values():
            n = len(ser)
            if n < 2:
                continue
            off = rng.randrange(n)
            for j, host in enumerate(ser):
                shifted.append((sign * ser[(j + off) % n][field], host[f"x{k}"]))
        if len(shifted) < 100:
            continue
        cuts = breakpoints([s for s, _ in shifted])
        v = q5_minus_q1([(bucket(s, cuts), x) for s, x in shifted])
        if v is not None:
            vals.append(v)
    if not vals:
        return {"draws": 0}
    return {"draws": len(vals), "mean_pp": statistics.fmean(vals),
            "sd_pp": statistics.pstdev(vals) if len(vals) > 1 else 0.0}


def folds(p: Panel, rows: list[dict], qkey: str, k: int, n: int = 4) -> list[dict]:
    idxs = [r["i"] for r in rows]
    lo, hi = min(idxs), max(idxs) + 1
    size = (hi - lo) / n
    out = []
    for j in range(n):
        a, b = lo + int(j * size), lo + int((j + 1) * size)
        obs = [(r[qkey], r[f"x{k}"]) for r in rows
               if a <= r["i"] < b and r.get(f"x{k}") is not None]
        out.append({"fold": j + 1, "from": p.dates[a],
                    "to": p.dates[min(b, len(p.dates) - 1)], "n": len(obs),
                    "q5mq1_pp": q5_minus_q1(obs) if len(obs) >= MIN_FOLD_N else None})
    return out


def momentum_candidates(p: Panel, lo_i: int, hi_i: int) -> list[dict]:
    import momentum_diagnose as MD
    mom, _ = MD.build_all(p)
    seen, out = set(), []
    for sym, i, *_ in mom:
        if not (lo_i <= i <= hi_i) or (sym, i) in seen:
            continue
        seen.add((sym, i))
        out.append({"symbol": sym, "i": i})
    return out


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=str(PANEL / "block_dom.json"))
    args = ap.parse_args()
    n_boot = 200 if args.quick else 2000
    n_null = 50 if args.quick else 200

    log("=" * 78)
    log("H2/H3 -- BLOCK DOMINANCE AND RETAIL CHURN.  Pre-registered: reference/blockdom.md")
    log("=" * 78)

    p = Panel()
    p.load()
    gross = load_gross()
    rows = build_rows(p, gross)
    if not rows:
        log("no rows -- gross panel did not join to the price panel")
        return 3

    idxs = [r["i"] for r in rows]
    lo_i, hi_i = min(idxs), max(idxs)
    log(f"gross panel: {len(gross)} symbol-days -> {len(rows)} with ticket_rel and a {PRIMARY_K}d return")
    log(f"window: {p.dates[lo_i]} .. {p.dates[hi_i]}  ({len({r['i'] for r in rows})} sessions, "
        f"{len({r['symbol'] for r in rows})} symbols)")

    # ---- check 0 first, restricted to THIS window
    cands = momentum_candidates(p, lo_i, hi_i)
    c0 = TB.check_zero(p, cands, k=PRIMARY_K, bar=BAR_CHECK0)
    pooled = c0.get("pooled", {})
    log(f"\ncheck 0 (in-window): momentum lift {100 * (pooled.get('lift') or 0):+.2f}pp "
        f"on n={pooled.get('n')} -> {'PASS' if c0.get('ok') else 'FAIL'}")
    if not c0.get("ok"):
        log("  [!!] the known-good rule does NOT work in this window. Stopping.")
        return 4

    nblocks_all = LL.blocks_with_treatment(idxs)
    log(f"blocks: {nblocks_all} -> "
        f"{'INFERENTIAL' if LL.is_inferential(nblocks_all) else 'DESCRIPTIVE (declared in advance)'}")

    uncond = [r[f"x{PRIMARY_K}"] for r in rows if r.get(f"x{PRIMARY_K}") is not None]
    log(f"within-population baseline (n={len(uncond)}): {100 * statistics.fmean(uncond):+.3f}pp")

    results = {}
    for label, field, sign in FEATURES:
        qkey = f"q_{label}"
        cuts = breakpoints([sign * r[field] for r in rows])
        for r in rows:
            r[qkey] = bucket(sign * r[field], cuts)

        grad = gradient(rows, qkey, PRIMARY_K)
        per_date: dict[int, list] = {}
        for r in rows:
            x = r.get(f"x{PRIMARY_K}")
            if x is not None:
                per_date.setdefault(r["i"], []).append((r[qkey], x))
        boot = LL.date_block_bootstrap(per_date, q5_minus_q1, n_boot=n_boot)
        null = feature_shift_null(rows, field, sign, PRIMARY_K, n_null)
        fr = folds(p, rows, qkey, PRIMARY_K)
        q5mq1 = (grad[-1]["mean_pp"] - grad[0]["mean_pp"]) \
            if grad[-1]["mean_pp"] is not None and grad[0]["mean_pp"] is not None else None
        steps = monotone_steps(grad)
        folds_pos = sum(1 for f in fr if (f["q5mq1_pp"] or 0) > 0)
        band_clear = (boot.get("lo") is not None
                      and (boot["lo"] > 0 or boot["hi"] < 0))

        results[label] = {
            "field": field, "sign": sign, "gradient": grad, "q5mq1_pp": q5mq1,
            "monotone_steps": steps, "bootstrap": boot, "null": null, "folds": fr,
            "folds_positive": folds_pos, "band_clear": band_clear,
            "other_k": {f"k{k}": gradient(rows, qkey, k) for k in HORIZONS if k != PRIMARY_K},
        }

        log("\n" + "-" * 78)
        log(f"{label}   score = {'+' if sign > 0 else '-'}{field}   "
            f"(Q5 = the end H{label[1]} says is BETTER)")
        log("-" * 78)
        log(f"{'quintile':>10} {'n':>7} {'mean pp':>10} {'median pp':>11} {'hit':>7}")
        for g in grad:
            log(f"{'Q' + str(g['q']):>10} {g['n']:>7} "
                f"{(g['mean_pp'] if g['mean_pp'] is not None else float('nan')):>+10.3f} "
                f"{(g['median_pp'] if g['median_pp'] is not None else float('nan')):>+11.3f} "
                f"{(g['hit'] or 0) * 100:>6.1f}%")
        lo_s = f"{boot['lo']:+.2f}" if boot.get("lo") is not None else "n/a"
        hi_s = f"{boot['hi']:+.2f}" if boot.get("hi") is not None else "n/a"
        log(f"\nQ5-Q1 = {q5mq1:+.3f}pp   band [{lo_s}, {hi_s}]   monotone {steps}/{N_QUINTILES - 1}")
        log(f"null: {null.get('mean_pp', float('nan')):+.3f}pp "
            f"(sd {null.get('sd_pp', float('nan')):.3f}, {null.get('draws')} draws)")
        for f in fr:
            v = f"{f['q5mq1_pp']:+.3f}pp" if f["q5mq1_pp"] is not None else "n/a (thin)"
            log(f"  fold {f['fold']}  {f['from']} .. {f['to']}  n={f['n']:>5}  {v}")

    # ------------------------------------------------------------------ verdict
    log("\n" + "=" * 78)
    verdicts = {}
    for label, _, _ in FEATURES:
        r = results[label]
        checks = [
            (f"1 Q5-Q1 >= +{BAR_Q5MQ1_PP}pp", r["q5mq1_pp"] is not None and r["q5mq1_pp"] >= BAR_Q5MQ1_PP),
            ("1b band clear of zero", r["band_clear"]),
            (f"2 monotone >= {BAR_MONOTONE_STEPS}/{N_QUINTILES - 1}",
             r["monotone_steps"] >= BAR_MONOTONE_STEPS),
            (f"3 null within +/-{BAR_NULL_PP}pp", abs(r["null"].get("mean_pp", 99)) <= BAR_NULL_PP),
            (f"4 folds positive >= {BAR_FOLDS}/4", r["folds_positive"] >= BAR_FOLDS),
            ("5 check 0", bool(c0.get("ok"))),
        ]
        v = "PASS" if all(ok for _, ok in checks) else "FAIL"
        verdicts[label] = v
        r["checks"] = {lab: ok for lab, ok in checks}
        r["verdict"] = v
        log(f"\n{label}: {v}")
        for lab, ok in checks:
            log(f"  [{'PASS' if ok else 'FAIL'}]  {lab}")

    # sec 7 consistency clauses
    h2, h3, h3b = (results["H2_ticket_rel"]["q5mq1_pp"], results["H3_churn"]["q5mq1_pp"],
                   results["H3b_retail"]["q5mq1_pp"])
    notes = []
    if h2 is not None and h3 is not None and (h2 > 0) != (h3 > 0):
        notes.append("H2 and H3 DISAGREE IN SIGN -- per blockdom.md sec 7 neither is read; the "
                     "variable is tracking something other than who is buying.")
    if h3 is not None and h3b is not None and (h3 > 0) != (h3b > 0):
        notes.append("churn_share and retail_share disagree in sign -- the identity list in "
                     "brokers.csv is doing the work, not the market.")
    for n in notes:
        log(f"\n[!] {n}")

    log(f"\nCEILING: {nblocks_all} blocks. Even a PASS here is DESCRIPTIVE and cannot ship; it")
    log("         would only justify paying to extend the gross panel. Declared in advance.")
    log("=" * 78)

    payload = {
        "study": "H2/H3 block dominance and retail churn",
        "preregistered": "reference/blockdom.md",
        "panel_fingerprint": panel_fingerprint(),
        "window": {"from": p.dates[lo_i], "to": p.dates[hi_i],
                   "sessions": len({r["i"] for r in rows}),
                   "symbols": len({r["symbol"] for r in rows}), "n_rows": len(rows)},
        "check_zero": c0,
        "blocks": nblocks_all,
        "inferential": LL.is_inferential(nblocks_all),
        "baseline_within_pp": 100 * statistics.fmean(uncond),
        "results": results,
        "verdicts": verdicts,
        "consistency_notes": notes,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2, default=float), encoding="utf-8")
    log(f"\nwrote {args.out}")
    return 0 if all(v == "PASS" for v in verdicts.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
