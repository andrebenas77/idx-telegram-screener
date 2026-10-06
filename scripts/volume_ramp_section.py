#!/usr/bin/env python3
"""VOLUME RAMP / SUPPORT: a read-out for the morning report. Standard library only.

WHAT IT IS. Four setups from research/idx-volume-ramp (2026-10-05/06): heavy volume in a falling
stock (BUILD-UP, REVERSAL), the first day off a limit-down lock (LOCK-BREAK) and DeepSeek's support
screen (SUPPORT). Over ten years none of them beat a random liquid stock after fees. It is here as
a watch-list of liquid names under heavy selling volume, never as a rule: hit rates above 50% with
a negative mean, because a miss costs more than a +3% touch pays.

OWNER'S SETTINGS (2026-10-06).
  * Liquid names only: MEDIAN daily traded value over the prior 20 sessions >= Rp5bn, signal day
    excluded. An average lets a thin name through on a few heavy days (EURO: average Rp4.7bn,
    median Rp0.3bn).
  * The report's regular list only (the panel pool plus EXTRA_WATCH). No market-wide pull.
  * GOTO is the one name printed without odds while it trades at or below Rp55, where the study
    has no record. No other name is screened for that line. GOTO always gets a line, so its
    absence is never silent: locked, off a lock, plain, above Rp55, or "no price bar".
  * No news column.
  * Not answered by the owner, so the author's defaults: the four setups only (no rule variants,
    no comparison rows, no DeepSeek tier letters) and no "thin, not listed" line.

THE NUMBERS IN THE HEADERS are constants from the ten-year run under the same Rp5bn median floor
over the whole market (921 names; research/idx-volume-ramp/daily_sample/out/floor_5.json). They
describe FIRST days in a setup; a later day in the same run is marked `rpt`. The count of "liquid
names" is the names that pass every screen: the median floor, the study's own filters (close above
Rp55, 15 traded sessions in 20, a Rp3bn average) and clean price data.

SHORT ON QUIET DAYS. With no name in a setup the section is three lines. The definitions and the
legend print only when a row does, so the message stays inside one Telegram chunk on most days.

A LOCK-BREAK IS SHOWN AS A LOCK-BREAK. A name off a limit-down lock that also fits the build-up or
the reversal is listed under LOCK-BREAK, the setup with the tail, and tagged with the other.

PHANTOM SESSIONS. Yahoo prints a bar for every name on some market holidays (2026-05-14, 05-15,
05-27 and 05-28: 162 of 162 names, volume zero, close unchanged). The report's coverage calendar
keeps such dates because the bars exist. Left in, they shift every 20-, 50- and 60-session window
for three months. real_sessions() drops any date on which under a fifth of the names traded, and
everything here is indexed over what is left.

PARITY. This is a port of vr_lib.features / states for one session. port_parity.py compares it
with the study engine name by name and session by session; it must agree before any change here.
Known limits, all of which leave a name OUT rather than print a wrong row:
  * Yahoo's 2y pull carries no split events. After a stock split whose restated history is not in
    whole rupiah, the price-data test trips and the name is left out for up to 60 sessions (CUAN
    after its 10:1 split of 2025-07-15). Lock tests on bars before a split use restated prices.
  * The session rule is the report's (60% of names have a bar) plus real_sessions (20% traded),
    not the study's vendor-hole rule. A null volume arrives here as zero.
"""
from __future__ import annotations

import math
import statistics

MED_FLOOR_IDR = 5e9        # owner's floor: prior-20-session MEDIAN traded value, signal day excluded
ADTV_FLOOR_IDR = 3e9       # the study's own floor: prior-20-session mean
MIN_TRADED = 15            # traded sessions required inside those 20
PRICE_FLOOR = 55.0         # the study covers closes above Rp55 only
VOL_X = 2.0                # today's volume over its prior-20-session average
RESERVE = 5                # sessions between two first days of the same setup in one name
BRK_JUMP, BRK_FLIP, BRK_LOOKBACK = 0.36, 0.15, 60
FLOOR_CHANGE = "2026-09-28"    # minimum price Rp50 -> Rp1
SPECIAL_BOARD = "2023-06-12"
ARB15_FROM = "2025-04-08"      # 15% lower limit; symmetric with the upper limit before that
LOOKBACK = 135             # sessions of history read per name
REAL_MIN_SHARE = 0.20      # a date on which fewer names than this traded is not a session
REPLAY = 30                # sessions replayed to tell a first day from a repeat day
LIST_CAP = 5
NO_ODDS_WATCH = ("GOTO",)

# Ten-year record, first days, Rp5bn median floor, whole market. touch / mean count each signal date once.
ODDS = {
    "universe": {"touch": 0.5303738763549927, "mean": -0.0035251755929223295},
    "B": {"n": 1351, "touch": 0.5804906004220218, "mean": -0.0031980362764835197, "tier": "LEVEL",
          "fell_p10": -0.13228819025717248, "touch_per_trade": 0.5958395245170877, "breakeven": 0.6132863067143199},
    "R": {"n": 839, "touch": 0.531298773690078, "mean": -0.009301603432032741, "tier": "WORSE",
          "fell_p10": -0.13825552661457283, "touch_per_trade": 0.5601907032181168, "breakeven": 0.6510048946972923},
    "L": {"n": 63, "touch": 0.7456140350877193, "mean": -0.020357029071902996, "tier": "TAIL",
          "fell_p10": -0.31081081081081086, "touch_per_trade": 0.7540983606557377, "breakeven": 0.8436520494531963},
    "S": {"n": 1170, "touch": 0.49387518575951406, "mean": -0.005025474090284894, "tier": "LEVEL",
          "fell_p10": -0.1140731469892922, "touch_per_trade": 0.511986301369863, "breakeven": 0.5689916716316293},
}
ARMS = ("B", "R", "L", "S")
WORD = {"B": "build-up", "R": "reversal", "L": "lock-break", "S": "support"}
NAME = {"B": "BUILD-UP", "R": "REVERSAL", "L": "LOCK-BREAK", "S": "SUPPORT"}
RULE = {
    "B": "= downtrend, volume up 2+ sessions running to 2x its 20-day avg, two down closes",
    "R": "= the same build-up, but today closed green",
    "L": "= first day off a limit-down lock of 2+ days",
    "S": "= volume over 2x its 20-day avg, close 0-2% above the prior 20-day low",
}
NO_ODDS = "no odds: study starts above Rp55"


def _tick(p: float) -> int:
    return 1 if p < 200 else 2 if p < 500 else 5 if p < 2000 else 10 if p < 5000 else 25


def _tick_ceil(x: float) -> float:
    t = _tick(x)
    return math.ceil(x / t - 1e-7) * t


def _snap(x: float) -> float:
    r = round(x)
    return float(r) if abs(x - r) <= 0.005 else x


def _is_int(x: float) -> bool:
    return abs(x - round(x)) <= 0.005


def real_sessions(p, syms, i: int) -> list:
    """Calendar indices up to i on which the market really traded, oldest first (LOOKBACK at most)."""
    out, k = [], i
    names = [s for s in syms if s in p.raw_close]
    while k >= 0 and len(out) < LOOKBACK:
        have = traded = 0
        for s in names:
            if k in p.raw_close[s]:
                have += 1
                traded += 1 if (p.volume[s].get(k) or 0) > 0 else 0
        if have and traded >= REAL_MIN_SHARE * have:
            out.append(k)
        k -= 1
    return out[::-1]


class _Name:
    """One name's sessions `ks` (real_sessions), indexed 0..n-1 with n-1 the signal day."""

    def __init__(self, p, sym: str, ks: list):
        g = lambda store: [(store.get(sym) or {}).get(k) for k in ks]
        self.o, self.h, self.l, self.c = g(p.open), g(p.high), g(p.low), g(p.raw_close)
        self.v, self.a = g(p.volume), g(p.close)
        self.dates = [p.dates[k] for k in ks]
        self.n = len(self.c)
        self.valid = [x is not None for x in self.c]
        self.traded = [self.valid[k] and (self.v[k] or 0) > 0 for k in range(self.n)]
        self.cp = [_snap(x) if x is not None else None for x in self.c]
        self._memo = {}
        self._brk = self._breaks()

    # ---- price-basis breaks (vr_lib.features, split factor taken as 1)
    def _breaks(self) -> list:
        out = [False] * self.n
        last_c = last_whole = None
        for k in range(self.n):
            if self.traded[k]:
                whole = all(_is_int(x) for x in (self.o[k], self.h[k], self.l[k], self.c[k]))
                if last_c is not None and last_c > 0:
                    jump = self.c[k] / last_c - 1
                    big = jump > BRK_JUMP if jump > 0 else -jump > BRK_JUMP
                    flip = whole != last_whole
                    gap_o = abs(self.o[k] / last_c - 1)
                    out[k] = (big and _snap(last_c) > 10) or (flip and not whole) or (flip and gap_o > BRK_FLIP)
                last_c, last_whole = self.c[k], whole
        return out

    def _m(self, key, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def data_ok(self, j: int) -> bool:
        def f():
            if j < 59 or not self.valid[j]:
                return False
            if any(self.a[k] is None for k in range(j - 59, j + 1)):
                return False
            if any(self.v[k] is None for k in range(j - 19, j + 1)):
                return False
            return not any(self._brk[k] for k in range(j - 59, j + 1))
        return self._m(("ok", j), f)

    def value_window(self, j: int):
        """Traded value of the 20 sessions before j, or None when a bar is missing."""
        def f():
            if j < 20 or any(not self.valid[k] for k in range(j - 20, j)):
                return None
            return [self.c[k] * self.v[k] for k in range(j - 20, j)]
        return self._m(("val", j), f)

    def elig(self, j: int) -> bool:
        def f():
            w = self.value_window(j)
            if not self.data_ok(j) or w is None:
                return False
            if sum(1 for k in range(j - 20, j) if self.traded[k]) < MIN_TRADED:
                return False
            if not self.cp[j] > PRICE_FLOOR:
                return False
            return statistics.fmean(w) >= ADTV_FLOOR_IDR and statistics.median(w) >= MED_FLOOR_IDR
        return self._m(("el", j), f)

    def v20(self, j: int):
        def f():
            if j < 20 or self.v[j] is None or any(self.v[k] is None for k in range(j - 20, j)):
                return None
            m = statistics.fmean(self.v[k] for k in range(j - 20, j))
            return self.v[j] / m if m > 0 else None
        return self._m(("v20", j), f)

    def up_days(self, j: int) -> int:
        """Sessions running, ending at j, on which volume beat the session before."""
        k, n = j, 0
        while k >= 1 and self.traded[k] and self.v[k - 1] is not None and self.v[k] > self.v[k - 1]:
            n += 1
            k -= 1
        return n

    def r1(self, j: int):
        if j < 1 or self.a[j] is None or self.a[j - 1] is None or self.a[j - 1] == 0:
            return None
        return self.a[j] / self.a[j - 1] - 1

    def down(self, j: int) -> bool:
        r = self.r1(j)
        return r is not None and r < 0

    def green(self, j: int) -> bool:
        r = self.r1(j)
        return bool(self.traded[j] and r is not None and r > 0 and self.c[j] > self.o[j])

    def downtrend(self, j: int) -> bool:
        def f():
            if j < 49 or any(self.a[k] is None for k in range(j - 49, j + 1)) or self.a[j - 20] is None:
                return False
            sma = statistics.fmean(self.a[k] for k in range(j - 49, j + 1))
            return self.a[j] < sma and self.a[j] < self.a[j - 20]
        return self._m(("D", j), f)

    def lock_dn(self, j: int) -> bool:
        """A flat bar on the lower auto-rejection price (vr_lib.limit_frames)."""
        def f():
            if j < 1 or not self.traded[j] or self.h[j] != self.l[j] or self.cp[j - 1] is None:
                return False
            prev, cp, d = self.cp[j - 1], self.cp[j], self.dates[j]
            if not cp < prev:
                return False
            new_rule = d >= FLOOR_CHANGE
            dn = 0.15 if d >= ARB15_FROM else (0.35 if prev <= 200 else 0.25 if prev <= 5000 else 0.20)
            sub50 = d >= SPECIAL_BOARD and not new_rule and (prev < 50 or cp < 50 or _snap(self.l[j]) < 50)
            if sub50:
                dn = 0.10
            arb = _tick_ceil(prev * (1 - dn))
            if new_rule or sub50:
                arb = max(prev - 1, 1) if prev <= 10 else arb
            else:
                arb = max(arb, 50.0)
            if _is_int(cp) and _is_int(prev):
                return abs(cp - arb) <= 0.005 or abs(cp - _tick_ceil(prev * 0.90)) <= 0.005
            return abs(cp / prev - 1 + dn) <= _tick(prev) / prev + 1e-4
        return self._m(("lk", j), f)

    def support(self, j: int):
        """(distance above the prior 20-day low, DeepSeek tier A-D) or None when not computable."""
        def f():
            if j < 50 or self.c[j] is None or self.o[j] is None:
                return None
            if any(self.l[k] is None for k in range(j - 20, j)) or any(self.c[k] is None for k in range(j - 50, j)):
                return None
            low = min(self.l[k] for k in range(j - 20, j))
            if not low > 0:
                return None
            up = self.c[j] > statistics.fmean(self.c[k] for k in range(j - 50, j))
            rej = self.c[j] > self.o[j]
            return self.c[j] / low - 1, ("A" if up and rej else "B" if up else "C" if rej else "D")
        return self._m(("sup", j), f)

    def state(self, arm: str, j: int) -> bool:
        def f():
            if j < 2 or not self.elig(j):
                return False
            v = self.v20(j)
            if arm == "L":
                return self.lock_dn(j - 1) and self.lock_dn(j - 2) and not self.lock_dn(j) and self.traded[j]
            if v is None:
                return False
            if arm == "S":
                s = self.support(j)
                return s is not None and 0 <= s[0] <= 0.02 and v > VOL_X
            ramp = self.up_days(j) >= 2 and v >= VOL_X
            if arm == "B":
                return ramp and self.downtrend(j) and self.down(j) and self.down(j - 1)
            return ramp and self.downtrend(j - 1) and self.down(j - 1) and self.green(j)        # R
        return self._m((arm, j), f)

    def first_day(self, arm: str) -> bool:
        """db_lib.events_from_state replayed over the last REPLAY sessions."""
        prev, last, hit = False, -10 ** 6, False
        for j in range(max(2, self.n - 1 - REPLAY), self.n):
            s = self.state(arm, j)
            hit = s and not prev and (j - last) >= RESERVE
            if hit:
                last = j
            if self.data_ok(j):
                prev = s
        return hit


def _lock_run(nm, j: int) -> int:
    """Limit-down locks in a row ending at j."""
    n = 0
    while j >= 1 and nm.lock_dn(j):
        n += 1
        j -= 1
    return n


def _watch(p, sym: str, ks: list, i: int, in_setup: bool):
    """What to say about a NO_ODDS_WATCH name. None when its normal row already says it."""
    if i not in (p.raw_close.get(sym) or {}):
        return {"sym": sym, "kind": "no_bar"}
    nm = _Name(p, sym, ks)
    j = nm.n - 1
    w = {"sym": sym, "close": nm.cp[j], "v20": nm.v20(j), "up": nm.up_days(j), "day": nm.r1(j),
         "value": nm.c[j] * (nm.v[j] or 0)}
    if nm.cp[j] <= PRICE_FLOOR:
        today, before = _lock_run(nm, j), _lock_run(nm, j - 1)
        w["kind"] = "locked" if today else "off_lock" if before >= 2 and nm.traded[j] else "plain"
        w["lock_days"] = today or before
        return w
    if in_setup:
        return None
    if nm.elig(j):
        w["kind"] = "above_no_setup"
        return w
    vw = nm.value_window(j)
    w["kind"] = "above_thin" if (vw is not None and statistics.median(vw) < MED_FLOOR_IDR) else "above_unscreenable"
    w["med"] = statistics.median(vw) if vw is not None else None
    return w


def screen(p, syms, i: int) -> dict:
    """Rows for session index i over `syms`. Pure: reads the panel, writes nothing."""
    rows, liquid, watch = [], 0, []
    ks = real_sessions(p, syms, i)
    if not ks or ks[-1] != i:               # the signal day is not a real session: say so, screen nothing
        return {"real": False, "rows": rows, "liquid": liquid, "watch": watch}
    for sym in sorted(set(syms)):
        if i not in (p.raw_close.get(sym) or {}):
            continue
        nm = _Name(p, sym, ks)
        j = nm.n - 1
        if not nm.elig(j):
            continue
        liquid += 1
        on = [a for a in ARMS if nm.state(a, j)]
        if not on:
            continue
        arm = "L" if "L" in on else on[0]                    # a lock-break is shown as a lock-break
        rows.append({"sym": sym, "arm": arm, "arms": on, "close": nm.cp[j], "v20": nm.v20(j), "up": nm.up_days(j),
                     "day": nm.r1(j), "med": statistics.median(nm.value_window(j)), "repeat": not nm.first_day(arm)})
    listed = {x["sym"] for x in rows}
    for sym in NO_ODDS_WATCH:
        w = _watch(p, sym, ks, i, sym in listed)
        if w:
            watch.append(w)
    return {"real": True, "rows": rows, "liquid": liquid, "watch": watch}


def _pc(x: float, d: int = 0) -> str:
    return "%.*f%%" % (d, 100 * x)


def _rp(v: float) -> str:
    return ("Rp%.1fb" if v < 1e10 else "Rp%.0fb") % (v / 1e9)


def _watch_line(w: dict, fmt_px) -> str:
    k = w["kind"]
    if k == "no_bar":
        return "  %s - no price bar for this session" % w["sym"]
    head = "  %s %s" % (w["sym"], fmt_px(w["close"]))
    day = "-" if w["day"] is None else "%+.1f%%" % (100 * w["day"])
    if k == "locked":
        return "%s | locked limit-down, day %d | day %s | %s | %s" % (head, w["lock_days"], day, _rp(w["value"]), NO_ODDS)
    if k == "off_lock":
        return "%s | first day off a %d-day limit-down lock | day %s | %s | %s" % (head, w["lock_days"], day, _rp(w["value"]), NO_ODDS)
    if k == "plain":
        return "%s | v20 %s | up %dd | day %s | %s | %s" % (head, "-" if w["v20"] is None else "%.1fx" % w["v20"], w["up"], day,
                                                           _rp(w["value"]), NO_ODDS)
    if k == "above_no_setup":
        return "%s | above Rp55: screened with the rest, in no setup today" % head
    if k == "above_thin":
        return "%s | above Rp55 but median day %s: under the Rp%.0fbn floor, not screened" % (head, _rp(w["med"]), MED_FLOOR_IDR / 1e9)
    return "%s | above Rp55 but not screenable today (price data)" % head


def section_lines(p, syms, i: int, fmt_px=None) -> list:
    """The section as plain-text lines for summary_text()."""
    fmt_px = fmt_px or (lambda v: "-" if v is None else "{:,.0f}".format(v))
    r = screen(p, syms, i)
    if not r["real"]:
        return ["VOLUME RAMP / SUPPORT - not available: %s is not a trading session in the price data" % p.dates[i]]
    u = ODDS["universe"]
    L = ["VOLUME RAMP / SUPPORT - liquid names under heavy volume (%d)" % len(r["rows"])]
    if not r["rows"]:
        L.append("  none of %d liquid names is in a build-up, reversal, lock-break or support setup." % r["liquid"])
        L += [_watch_line(w, fmt_px) for w in r["watch"]]
        return L
    L.append("  %d liquid names: regular list, median day >= Rp%.0fbn over the prior 20 sessions." % (r["liquid"], MED_FLOOR_IDR / 1e9))
    L.append("  Ten years, whole market, after fees: no setup beat a random liquid stock (touch %s, mean %+.2f%%)."
             % (_pc(u["touch"]), 100 * u["mean"]))
    for arm in ARMS:
        o = ODDS[arm]
        note = ("a tenth of trades fell %s or more" % _pc(-o["fell_p10"]) if o["tier"] == "TAIL"
                else "below a random stock" if o["tier"] == "WORSE" else "not different from a random stock")
        g = sorted((x for x in r["rows"] if x["arm"] == arm), key=lambda x: -x["med"])
        L.append("  %s - touch %s, mean %+.2f%%: %s (%d)" % (NAME[arm], _pc(o["touch"]), 100 * o["mean"], note, len(g)))
        if g:
            L.append("    " + RULE[arm])
        for x in g[:LIST_CAP]:
            tag = (["rpt"] if x["repeat"] else [])
            other = [WORD[a] for a in x["arms"] if a != arm]
            if other:
                tag.append("also " + ", ".join(other))
            L.append("    %-5s %8s | v20 %.1fx | up %dd | day %+.1f%% | med %s%s"
                     % (x["sym"], fmt_px(x["close"]), x["v20"] or 0.0, x["up"], 100 * (x["day"] or 0.0), _rp(x["med"]),
                        "".join(" | " + t for t in tag)))
        if len(g) > LIST_CAP:
            L.append("    +%d more" % (len(g) - LIST_CAP))
    L += [_watch_line(w, fmt_px) for w in r["watch"]]
    L.append("  touch = traded +3% above the next open within 5 sessions.")
    L.append("  mean = average result after fees, sold at +3% or else at the 5th close.")
    L.append("  To break even, touch must reach: "
             + ", ".join("%s %s" % (WORD[a], _pc(ODDS[a]["breakeven"])) for a in ARMS) + ".")
    L.append("  v20 = volume / its prior-20-session average (not rvol5). up Nd = volume up N sessions running.")
    L.append("  med = median daily value. rpt = not a first day; the ten-year numbers describe first days.")
    L.append("  Read-out, not a rule.")
    return L
