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
import math
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
                    except (TypeError, ValueError):
                        continue
                    if cl <= 0:
                        continue
                    # Turnover uses RAW price x volume: that is the actual rupiah
                    # traded on the day. Adjusted prices are for returns only — using
                    # them here would understate pre-split liquidity by the split ratio.
                    raw[r["symbol"]][r["date"]] = (cl, rawcl * vol, vol, hi, lo, rawcl)
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
        return (f"{len(self.dates)} trading days | {len(self.close)} symbols | "
                f"{len({b for _, b in self.flows})} brokers | "
                f"{sum(len(v) for v in self.flows.values()):,} flow rows | "
                f"{len(self.bench)} benchmark days")
