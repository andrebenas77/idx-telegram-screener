#!/usr/bin/env python3
"""Shared loading and statistics for the Broker Alpha work.

Kept separate from the scoring so the same forward-return machinery can be pointed at
ANY signal — the v3 screener's Retail trap / Smart money flags included — rather than
being welded to the broker leaderboard.

Two conventions here are load-bearing and easy to get wrong:

1. **Entry lag.** Flow for day t is only published after that day's close (Invezgo
   refreshes EOD at 18:00 WIB), so a strategy cannot trade on it until t+1. Every
   forward return is therefore measured from `close_adj[t+1]`, not `close_adj[t]`.
   Measuring from t is the classic way to manufacture an edge that does not exist.
2. **Market adjustment.** Returns are excess over IHSG. Over this window the index
   fell 10.9%, so raw returns would rank brokers largely by beta.

Dates are carried as integer indices into a global trading-day list — with ~881k flow
rows and no pandas available, string keys everywhere would be needlessly heavy.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

PANEL = Path(__file__).resolve().parent.parent / "data" / "panel"


# ------------------------------------------------------------------------- statistics

def wilson_lower(hits: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for a proportion.

    Ranking on the raw hit rate lets a broker with 4 lucky trades top the board. The
    Wilson lower bound penalises small samples automatically, so 3/4 ranks below
    60/100 as it should.
    """
    if n <= 0:
        return 0.0
    p = hits / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def median(xs) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def block_ci(values, n_boot: int = 2000, block: int | None = None,
             seed: int = 7) -> dict:
    """Circular moving-block bootstrap CI for the mean of a TIME-ORDERED series.

    Never a z-test. Momentum events cluster heavily in time and share market
    exposure, so independence is badly violated and a naive standard error is far
    too small — the same data gave z = -3.47 ("decisive") against a block bootstrap
    reading of 5.2% ("a 1-in-20 stretch, unremarkable"). dedup_audit.block_bootstrap
    applies the same reasoning to the "is the recent window unusual" question; this
    is the interval form, for putting a band on any mean or any PAIRED difference.

    `values` MUST already be in time order. A shuffled input silently degrades this
    to an iid bootstrap and reports a band roughly sqrt(block) times too narrow —
    the failure looks like a result, not an error.

    Blocks wrap circularly so every observation has equal weight; with truncation the
    first and last `block` points are undersampled and the tails get pulled inward.
    """
    xs = [float(v) for v in values if v is not None]
    n = len(xs)
    if n < 8:
        return {"n": n, "mean": statistics.fmean(xs) if xs else 0.0,
                "lo95": None, "hi95": None, "se": None, "block": None}
    if block is None:
        block = max(1, math.ceil(n ** (1 / 3)))
    block = max(1, min(block, n))
    nblocks = math.ceil(n / block)
    rng = random.Random(seed)
    total = nblocks * block
    means = []
    for _ in range(n_boot):
        acc = 0.0
        for _ in range(nblocks):
            s = rng.randrange(n)
            for t in range(block):
                acc += xs[(s + t) % n]

        means.append(acc / total)
    means.sort()
    return {"n": n, "mean": statistics.fmean(xs),
            "lo95": means[int(0.025 * n_boot)],
            "hi95": means[min(n_boot - 1, int(0.975 * n_boot))],
            "se": statistics.pstdev(means), "block": block}


def ci_clear_of_zero(ci: dict) -> bool:
    """True when the 95% band excludes zero — i.e. the sign is readable.

    Used as the ship/kill gate everywhere in the exit work, so that "improved by
    +0.3pp" cannot be reported as a win when the band runs -1.1pp to +1.7pp.
    """
    lo, hi = ci.get("lo95"), ci.get("hi95")
    return lo is not None and hi is not None and (lo > 0 or hi < 0)


def panel_fingerprint(panel_dir: Path = PANEL) -> dict:
    """Identity of the data a result was computed on.

    A golden file is only comparable to a run over the SAME panel. The panel grew
    from 112 to 161 symbols mid-project and the stored trade_backtest.json (n=915)
    silently stopped describing the code that produced it. Every result JSON carries
    this, and --regress refuses to diff across a mismatch.
    """
    h = hashlib.sha256()
    files = sorted(panel_dir.glob("prices-*.csv.gz")) + \
        sorted(panel_dir.glob("flows-*.csv.gz"))
    for f in files:
        st = f.stat()
        h.update(f"{f.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return {"n_price_files": len(list(panel_dir.glob("prices-*.csv.gz"))),
            "n_flow_files": len(list(panel_dir.glob("flows-*.csv.gz"))),
            "sha": h.hexdigest()[:12]}


def summarise(excess: list[float]) -> dict:
    """Hit rate (+ Wilson bound), mean and median excess for a set of events."""
    n = len(excess)
    hits = sum(1 for x in excess if x > 0)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": hits / n if n else 0.0,
        "wilson": wilson_lower(hits, n),
        "mean_excess": mean(excess),
        "median_excess": median(excess),
    }


# ------------------------------------------------------------------------------- data

class Panel:
    """The backfilled panel, indexed for fast event construction."""

    def __init__(self, panel_dir: Path = PANEL):
        self.dir = panel_dir
        self.dates: list[str] = []
        self.didx: dict[str, int] = {}
        self.close: dict[str, dict[int, float]] = {}      # sym -> {i: adj close}
        self.turnover: dict[str, dict[int, float]] = {}   # sym -> {i: close*volume}
        self.volume: dict[str, dict[int, float]] = {}     # sym -> {i: shares}
        # RAW high/low/close, i.e. the levels actually printed on the day. Price
        # STRUCTURE (higher highs, higher lows, where the close sat in the range) must
        # be judged on what traded, not on a back-adjusted series — adjustment rescales
        # history every time a corporate action lands and would silently rewrite past
        # highs. Adjusted closes stay reserved for RETURNS.
        self.high: dict[str, dict[int, float]] = {}
        self.low: dict[str, dict[int, float]] = {}
        self.raw_close: dict[str, dict[int, float]] = {}
        # RAW open. Present in prices-*.csv.gz on every row and, until 2026-08-15,
        # never parsed — so a stop gapped through overnight was filled at the PRIOR
        # CLOSE as a proxy. Measured across 29,028 stop episodes that proxy is
        # optimistic on 8.9% of them and pessimistic on 0.0%: a one-sided flattery of
        # exactly the violent days a stop exists for. Clamped into [low, high]; see
        # n_open_clamped.
        self.open: dict[str, dict[int, float]] = {}
        self.n_open_clamped = 0
        self.n_open_rows = 0
        self.adtv: dict[str, dict[int, float]] = {}       # sym -> {i: 20d mean turnover}
        self.bench: dict[int, float] = {}
        self.flows: dict[tuple[str, str], list[tuple[int, float]]] = {}

    # ---- prices

    def load_prices(self) -> None:
        raw: dict[str, dict[str, tuple]] = defaultdict(dict)
        alldates = set()
        for f in sorted(self.dir.glob("prices-*.csv.gz")):
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        cl = float(r["close_adj"])
                        vol = float(r["volume"] or 0)
                        rawcl = float(r["close"])
                        hi = float(r["high"]) if r.get("high") else None
                        lo = float(r["low"]) if r.get("low") else None
                        op = float(r["open"]) if r.get("open") else None
                    except (TypeError, ValueError):
                        continue
                    if cl <= 0:
                        continue
                    if op is not None and op <= 0:
                        op = None
                    # Turnover uses RAW price x volume: that is the actual rupiah
                    # traded on the day. Adjusted prices are for returns only — using
                    # them here would understate pre-split liquidity by the split ratio.
                    # `op` is APPENDED at index 6 — v[3]/v[4]/v[5] are load-bearing
                    # below and renumbering them would silently swap high for low.
                    raw[r["symbol"]][r["date"]] = (cl, rawcl * vol, vol, hi, lo, rawcl, op)
                    alldates.add(r["date"])

        self.dates = sorted(alldates)
        self.didx = {d: i for i, d in enumerate(self.dates)}

        for sym, series in raw.items():
            cl = {self.didx[d]: v[0] for d, v in series.items()}
            tn = {self.didx[d]: v[1] for d, v in series.items()}
            self.close[sym] = cl
            self.turnover[sym] = tn
            self.volume[sym] = {self.didx[d]: v[2] for d, v in series.items()}
            self.high[sym] = {self.didx[d]: v[3] for d, v in series.items()
                              if v[3] is not None}
            self.low[sym] = {self.didx[d]: v[4] for d, v in series.items()
                             if v[4] is not None}
            self.raw_close[sym] = {self.didx[d]: v[5] for d, v in series.items()}
            # Opens, CLAMPED into the session's own [low, high]. 1.75% of rows print an
            # open outside that range (a pre-opening auction artefact — the same
            # phenomenon intraday_lib documents from the other side). The alternative,
            # dropping them back to the prior close, re-imports the optimistic fill on
            # precisely the gap days this series exists to price honestly. Clamping is
            # the conservative reading and it is counted, not hidden.
            ops = {}
            for d, v in series.items():
                if v[6] is None:
                    continue
                i = self.didx[d]
                o, hi_, lo_ = v[6], v[3], v[4]
                if hi_ is not None and lo_ is not None and hi_ >= lo_:
                    c = min(max(o, lo_), hi_)
                    if c != o:
                        self.n_open_clamped += 1
                    o = c
                self.n_open_rows += 1
                ops[i] = o
            self.open[sym] = ops
            # Trailing 20-session average turnover, strictly BEFORE the day itself so
            # the liquidity filter never peeks at the day being evaluated.
            idxs = sorted(tn)
            adtv, window = {}, []
            for i in idxs:
                if window:
                    adtv[i] = sum(window) / len(window)
                window.append(tn[i])
                if len(window) > 20:
                    window.pop(0)
            self.adtv[sym] = adtv

    def load_benchmark(self, name: str = "ihsg") -> None:
        p = self.dir / f"benchmark-{name}.csv"
        with p.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                i = self.didx.get(r["date"])
                if i is not None:
                    try:
                        self.bench[i] = float(r["close"])
                    except (TypeError, ValueError):
                        pass

    def load_flows(self) -> None:
        acc: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
        for f in sorted(self.dir.glob("flows-*.csv.gz")):
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    i = self.didx.get(r["date"])
                    if i is None:
                        continue
                    try:
                        acc[(r["symbol"], r["broker"])].append((i, float(r["net_value"])))
                    except (TypeError, ValueError):
                        pass
        self.flows = {k: sorted(v) for k, v in acc.items()}

    def load(self, benchmark: str = "ihsg") -> "Panel":
        self.load_prices()
        self.load_benchmark(benchmark)
        self.load_flows()
        return self

    # ---- returns

    def excess_return(self, sym: str, i: int, k: int, entry_lag: int = 1):
        """Excess-over-IHSG return of holding `sym` for k sessions.

        Signal is observed on day i; entry is at the close of day i+entry_lag, exit k
        sessions later. Returns None when any leg is missing, so gaps drop the event
        rather than silently distorting it.
        """
        a, b = i + entry_lag, i + entry_lag + k
        cl = self.close.get(sym)
        if not cl or a not in cl or b not in cl:
            return None
        if a not in self.bench or b not in self.bench:
            return None
        stock = cl[b] / cl[a] - 1
        market = self.bench[b] / self.bench[a] - 1
        return stock - market

    def describe(self) -> str:
        # Open coverage is printed because a silently EMPTY open series would not
        # raise anywhere — it would just fall back to the prior-close proxy on every
        # gap and reproduce the old numbers, which reads as "the change did nothing".
        n_open = sum(len(v) for v in self.open.values())
        n_close = sum(len(v) for v in self.raw_close.values())
        cov = 100.0 * n_open / n_close if n_close else 0.0
        return (f"{len(self.dates)} trading days | {len(self.close)} symbols | "
                f"{len({b for _, b in self.flows})} brokers | "
                f"{sum(len(v) for v in self.flows.values()):,} flow rows | "
                f"{len(self.bench)} benchmark days | "
                f"opens {n_open:,} ({cov:.1f}%), {self.n_open_clamped:,} clamped")
