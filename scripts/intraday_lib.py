#!/usr/bin/env python3
"""5-minute bar mechanics: VWAP, opening range, and the entry gates.

The daily backtest established that a wider stop and a close-based structure exit are
worth +0.82pp of excess return on a like-for-like universe. What it could NOT test is
*when* to buy, because a daily bar has no inside. That is what this module is for.

The motivating observation, from 2026-08-10: IHSG gapped up, made its high at 09:31
and closed at the session low. GGRM's high printed at 09:00, ISAT's at 09:00, ERAA's
at 09:28 — and in all three cases the high of the day WAS the high of the opening
range. They spent 61% / 80% / 85% of the session below VWAP. On 2026-08-07, by
contrast, GGRM broke well above its opening range and spent 4% of the session below
VWAP. One mechanism separates them: *if the high of the day was set in the first
thirty minutes, that is distribution, and it should not be bought.*

Three things this module has to get right:

1. **The index has no volume.** Yahoo serves IHSG with volume 0 on every bar, so a
   volume-weighted VWAP is undefined for it — the cumulative divisor is zero and
   every "close > VWAP" test silently evaluates against the close itself, which is
   never strictly greater. That bug rejected the winning trade on the first replay.
   The regime gate uses TWAP. `vwap_series` refuses to guess and returns None.

2. **Pre-open bars are not trades.** The feed starts at 08:55 with the pre-open
   auction. Letting that bar seed VWAP anchors the whole session to a single
   uncontested print.

3. **The signal bar is not the fill.** Entry is the NEXT bar's open plus a tick. This
   is the intraday form of the entry-lag discipline in alpha_lib.py:10-14; filling at
   the signal bar's close books a move that was not available when the signal formed.

Lunch is absent rather than empty: a full IDX session is 68 five-minute bars running
08:55 to 16:10, with no bars printed across the midday break. VWAP accumulates across
the gap; elapsed-bar fractions count bars that exist, not clock time.
"""
from __future__ import annotations

import csv
import gzip
import socket
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
INTRADAY = ROOT / "data" / "intraday"

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

OPEN_HHMM = "09:00"
OR_END = "09:30"
LAST_ENTRY = "14:30"
PRECLOSE = "15:35"


def pin_ipv4() -> None:
    """Force IPv4 for the whole process.

    Invezgo's multi-time endpoint sits behind a Cloudflare edge that answers 1010 to
    IPv6 clients from here. This is a process-GLOBAL monkeypatch on urllib3 — call it
    from a script main(), NEVER at import time, or every requests call in the 07:00
    daily build silently becomes IPv4-only too.
    """
    import urllib3.util.connection as u3c
    u3c.allowed_gai_family = lambda: socket.AF_INET


class Bar(NamedTuple):
    date: str      # "2026-08-10"
    hhmm: str      # "09:05" — the bar's CLOSE time
    o: float
    h: float
    l: float
    c: float
    v: float


# ------------------------------------------------------------------------------ store

def bars_path(symbol: str) -> Path:
    return INTRADAY / f"m5-{symbol.upper()}.csv.gz"


def write_bars(symbol: str, bars: list[Bar]) -> Path:
    INTRADAY.mkdir(parents=True, exist_ok=True)
    p = bars_path(symbol)
    with gzip.open(p, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "hhmm", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.date, b.hhmm, b.o, b.h, b.l, b.c, b.v])
    return p


def read_bars(symbol: str, include_auction: bool = False) -> dict[str, list[Bar]]:
    """{date: [Bar, ...]} for one symbol, time-ordered.

    `include_auction=False` (the default, and what every analysis path wants) drops
    the 08:55 pre-opening bar. That bar is a single uncontested auction print, often
    on very large size — letting it seed VWAP anchors the entire session to a price
    nobody could trade against, and it is not part of the continuous session whose
    behaviour the entry gates are about.

    `include_auction=True` is for RECONCILIATION only. The daily panel's high and low
    DO include the auction: measured across 12,425 sessions, the 08:55 print is the
    day's high or low often enough that excluding it makes 5% of sessions disagree
    with the panel by exactly one tick. Both readings are correct; they answer
    different questions, and conflating them makes a clean feed look broken.
    """
    p = bars_path(symbol)
    if not p.exists():
        return {}
    days: dict[str, list[Bar]] = {}
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                b = Bar(r["date"], r["hhmm"], float(r["open"]), float(r["high"]),
                        float(r["low"]), float(r["close"]), float(r["volume"] or 0))
            except (TypeError, ValueError):
                continue
            if not include_auction and b.hhmm < OPEN_HHMM:
                continue
            days.setdefault(b.date, []).append(b)
    return {d: sorted(v, key=lambda x: x.hhmm) for d, v in days.items()}


def parse_payload(rows) -> list[Bar]:
    """Invezgo multi-time rows -> Bars. Timestamps are ALREADY WIB; never convert.

    Deduplicates by (date, hhmm): the endpoint has been observed returning repeated
    timestamps on some ranges, and a duplicated bar double-counts its volume in VWAP.
    """
    seen: dict[tuple[str, str], Bar] = {}
    for r in rows or []:
        ts = r.get("date") or r.get("datetime") or r.get("time")
        if not ts or len(ts) < 16:
            continue
        try:
            b = Bar(ts[:10], ts[11:16], float(r["open"]), float(r["high"]),
                    float(r["low"]), float(r["close"]), float(r.get("volume") or 0))
        except (TypeError, ValueError, KeyError):
            continue
        seen[(b.date, b.hhmm)] = b
    return [seen[k] for k in sorted(seen)]


# --------------------------------------------------------------------------- features

def vwap_series(bars: list[Bar]):
    """Anchored session VWAP on typical price. None when the session has no volume.

    Returning None rather than falling back to price is deliberate: an index has zero
    volume, and a silent fallback makes `close > vwap` evaluate as `close > close`,
    which is never true. That is a rejection of every candidate, and it looks exactly
    like a working filter.
    """
    if not bars or not any(b.v for b in bars):
        return None
    out, cpv, cv, last = [], 0.0, 0.0, bars[0].c
    for b in bars:
        tp = (b.h + b.l + b.c) / 3
        cpv += tp * b.v
        cv += b.v
        last = cpv / cv if cv else last
        out.append(last)
    return out


def twap_series(bars: list[Bar]) -> list[float]:
    """Cumulative mean of closes. The regime proxy for a volume-less index."""
    out, cum = [], 0.0
    for i, b in enumerate(bars, 1):
        cum += b.c
        out.append(cum / i)
    return out


def reference_series(bars: list[Bar]) -> list[float]:
    """VWAP where volume exists, TWAP where it does not."""
    return vwap_series(bars) or twap_series(bars)


def opening_range(bars: list[Bar], end_hhmm: str = OR_END):
    """{'hi','lo','n'} over the bars closing at or before `end_hhmm`."""
    w = [b for b in bars if b.hhmm <= end_hhmm]
    if not w:
        return None
    return {"hi": max(b.h for b in w), "lo": min(b.l for b in w), "n": len(w)}


def frac_below(bars: list[Bar], ref: list[float], upto: int) -> float:
    """Share of elapsed bars that CLOSED below the then-current reference line.

    This single number separated GGRM on 2026-08-07 (4%) from ISAT on 2026-08-10
    (85%). It is a running measure — each bar is compared against the VWAP as it
    stood at that bar, not against the final one.
    """
    n = min(upto + 1, len(bars), len(ref))
    if n <= 0:
        return 0.0
    return sum(1 for k in range(n) if bars[k].c < ref[k]) / n


def intraday_clv(bars: list[Bar], upto: int | None = None):
    """(last close - low) / (high - low) over bars 0..upto.

    None when high == low — a limit-locked ARA/ARB session. Same convention as
    overlay_test.structure(), which refuses to invent a neutral 0.5 for the most
    decisive sessions on IDX.
    """
    w = bars[:(upto + 1)] if upto is not None else bars
    if not w:
        return None
    h, l = max(b.h for b in w), min(b.l for b in w)
    if h == l:
        return None
    return (w[-1].c - l) / (h - l)


def bar_at(bars: list[Bar], hhmm: str) -> int | None:
    """Index of the last bar closing at or before hhmm."""
    idx = None
    for i, b in enumerate(bars):
        if b.hhmm <= hhmm:
            idx = i
        else:
            break
    return idx


def session_summary(bars: list[Bar], upto_hhmm: str | None = None) -> dict:
    ref = reference_series(bars)
    i = (bar_at(bars, upto_hhmm) if upto_hhmm else len(bars) - 1)
    if i is None or i < 0:
        return {}
    orng = opening_range(bars)
    return {"n_bars": len(bars), "close": bars[i].c,
            "ref": ref[i], "is_vwap": vwap_series(bars) is not None,
            "vs_ref": bars[i].c / ref[i] - 1 if ref[i] else 0.0,
            "frac_below": frac_below(bars, ref, i),
            "clv": intraday_clv(bars, i),
            "or_hi": orng["hi"] if orng else None,
            "or_lo": orng["lo"] if orng else None,
            "hod": max(b.h for b in bars[:i + 1]),
            "hod_hhmm": max(bars[:i + 1], key=lambda b: b.h).hhmm,
            "hod_in_or": (max(b.h for b in bars[:i + 1]) <= orng["hi"]) if orng else None}


# ------------------------------------------------------------------------ entry gates

ALL_GATES = ("G1", "G2", "G3", "G4", "G5")


def evaluate_entry(bars: list[Bar], idx_bars: list[Bar], t_min: str,
                   atr_pct: float | None = None, cfg=None,
                   gates: tuple[str, ...] = ALL_GATES) -> dict:
    """First bar at or after `t_min` where every gate holds. Fill on the NEXT bar.

    G1 regime   index close > its reference AND <=30% of its bars below it
    G2 break    close > opening-range high        <- the binding gate in every
                                                     observed distribution session
    G3 vwap     close > session VWAP
    G4 tape     <=30% of elapsed bars closed below VWAP
    G5 no-chase close <= or_hi * (1 + 0.5*atr_pct)

    Returns the trigger, the fill, and — when nothing triggers — WHICH gate bound,
    because "no entry" without a reason is not a testable statement.
    """
    out = {"entry": False, "hhmm": None, "signal_px": None, "fill_px": None,
           "or_hi": None, "binding": None, "gate_fails": {}}
    if not bars:
        return out
    orng = opening_range(bars)
    if not orng:
        return out
    out["or_hi"] = orng["hi"]

    ref = reference_series(bars)
    iref = reference_series(idx_bars) if idx_bars else None
    fails = {"G1": 0, "G2": 0, "G3": 0, "G4": 0, "G5": 0}
    n_eval = 0

    for i, b in enumerate(bars):
        if b.hhmm < t_min or b.hhmm > LAST_ENTRY:
            continue
        n_eval += 1
        g1 = True
        if idx_bars and iref:
            k = bar_at(idx_bars, b.hhmm)
            if k is None:
                g1 = False
            else:
                g1 = idx_bars[k].c > iref[k] and frac_below(idx_bars, iref, k) <= 0.30
        g2 = b.c > orng["hi"]
        g3 = b.c > ref[i]
        g4 = frac_below(bars, ref, i) <= 0.30
        g5 = True if not atr_pct else b.c <= orng["hi"] * (1 + 0.5 * atr_pct)
        live = {"G1": g1, "G2": g2, "G3": g3, "G4": g4, "G5": g5}
        for name, ok in live.items():
            if not ok:
                fails[name] += 1
        if all(live[g] for g in gates):
            nxt = bars[i + 1] if i + 1 < len(bars) else None
            out.update(entry=True, hhmm=b.hhmm, signal_px=b.c,
                       fill_px=(nxt.o if nxt else b.c), fill_hhmm=(nxt.hhmm if nxt else b.hhmm),
                       frac_below=frac_below(bars, ref, i), ref=ref[i])
            return out
    out["gate_fails"] = fails
    out["n_eval"] = n_eval
    if n_eval:
        out["binding"] = max(fails, key=fails.get)
    return out
