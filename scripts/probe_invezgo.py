#!/usr/bin/env python3
"""Phase 0 probe — establish what the Invezgo subscription actually gives us.

Nothing in v4 can be scoped until these are answered with real numbers, so this runs
first and blocks the rest of the build:

  1. Real daily quota and subscription scope.
  2. How far back history actually goes for chart / stalker / summary / inventory.
  3. Whether intraday accepts a date (i.e. can intraday ever be backtested?).
  4. Batch behaviour: does it work, and what is the real symbol ceiling?
  5. What the `market` board filter values return (RG / TN / NG).
  6. THE GATE: does Invezgo's broker data reconcile with Sectors on a known day?

Everything is written schema-agnostically. We have the endpoint paths and parameter
names from the Go SDK source but not the response shapes, so the probe walks whatever
JSON comes back rather than assuming field names — that way an unexpected schema
produces a finding instead of a traceback.

Costs roughly 15 requests. Raw responses land in build/probe/ for inspection.

Usage:
    py scripts/probe_invezgo.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from invezgo_client import (  # noqa: E402
    InvezgoClient, MARKET_CASH, MARKET_NEGOTIATED, MARKET_REGULAR,
)

OUT_DIR = Path(__file__).resolve().parent.parent / "build" / "probe"

# --- The parity gate -------------------------------------------------------------
# Ground truth pulled from the Sectors MCP on 2026-08-05 (fetch-broker-activity, RX,
# 2026-08-04). If Invezgo disagrees materially on these, the project stops until it is
# explained — every downstream signal is built on this data being right.
PARITY_DATE = "2026-08-04"
PARITY_SYMBOL = "BBCA"
PARITY_BROKER = "RX"
#
# UNITS: Sectors reports VOLUME IN LOTS, Invezgo reports it IN SHARES. 1 IDX lot = 100
# shares. The expectations below are stated in Invezgo's units (shares) with the
# conversion applied explicitly, because encoding the known convention is honest whereas
# loosening the tolerance until it passes is not. Rupiah values and frequencies need no
# conversion. Get this wrong and every %ADTV threshold is off by 100x.
LOT = 100
PARITY_EXPECTED = {
    "bval": 32_581_162_500,
    "bfreq": 802,
    "bvol_shares": 51_013 * LOT,          # Sectors blot 51,013
    "bavg_per_share": 6386.8352,
    "sval": 181_350_000,
    "sfreq": 1,
    "svol_shares": 279 * LOT,             # Sectors slot 279
    "nval": 32_399_812_500,
    "nvol_shares": 50_734 * LOT,          # Sectors nlot 50,734
}
# Sectors does not say which board its figures cover, so try every value of `market`
# and report which one reconciles. That answers the parity question and the
# "what does market= actually mean" question in the same pass.
PARITY_TOLERANCE = 0.005  # 0.5%

# No trailing \b: Invezgo returns ISO timestamps like "2024-08-06T00:00:00.000Z", and
# there is no word boundary between "06" and "T", so a trailing \b silently matches
# nothing and history looks empty.
DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})")


# ---------------------------------------------------------------- generic JSON walkers

def walk(node):
    """Yield every dict nested anywhere inside a JSON structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def find_dates(node) -> list[str]:
    """Every YYYY-MM-DD found anywhere in the payload, deduped and sorted.

    Used to measure real history depth without knowing which key holds the date.
    """
    found = set()

    def rec(n):
        if isinstance(n, str):
            found.update(DATE_RE.findall(n))
        elif isinstance(n, dict):
            for k, v in n.items():
                found.update(DATE_RE.findall(str(k)))
                rec(v)
        elif isinstance(n, list):
            for v in n:
                rec(v)

    rec(node)
    return sorted(found)


# Key names a broker code might hide behind. `kode` is the Indonesian spelling and does
# NOT contain the substring "code" — an early version missed it and would have reported
# "no RX row found" against a perfectly good payload.
BROKER_KEY_HINTS = ("broker", "code", "kode", "member", "ab", "id")


def find_row(node, value: str, hints: tuple[str, ...] = BROKER_KEY_HINTS) -> dict | None:
    """First dict that identifies itself as `value` (case-insensitive).

    Two passes, because we do not know the schema:
      1. Prefer a dict where a key resembling one of `hints` holds the value — that is
         almost certainly the identifying field.
      2. Fall back to ANY dict holding the value as a string value under any key. A
         broker row must mention its own code somewhere, whatever the key is called.
    """
    target = value.strip().upper()

    for d in walk(node):
        for k, v in d.items():
            if not isinstance(v, str) or v.strip().upper() != target:
                continue
            if any(h in str(k).lower() for h in hints):
                return d

    for d in walk(node):
        for v in d.values():
            if isinstance(v, str) and v.strip().upper() == target:
                return d
    return None


def numeric_fields(d: dict) -> dict:
    """Numeric fields, coercing numeric STRINGS.

    Invezgo returns every figure as a string (`"buy_value": "32581162500"`), so an
    isinstance check alone finds nothing and the parity gate reports a false failure.
    """
    out = {}
    for k, v in d.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str):
            try:
                out[k] = float(v)
            except ValueError:
                pass
    return out


def describe(node, depth: int = 0) -> str:
    """One-line shape summary of an unknown payload."""
    if isinstance(node, dict):
        keys = list(node.keys())
        head = ", ".join(keys[:8]) + ("…" if len(keys) > 8 else "")
        return f"dict({len(keys)} keys: {head})"
    if isinstance(node, list):
        inner = describe(node[0], depth + 1) if node else "empty"
        return f"list[{len(node)}] of {inner}"
    return type(node).__name__


def save(name: str, payload) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (OUT_DIR / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ! could not save {name}: {e}")


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def rel(actual, expected) -> float:
    if expected in (0, None) or actual is None:
        return float("inf")
    return abs(float(actual) - float(expected)) / abs(float(expected))


# ---------------------------------------------------------------------------- probes

def probe_quota(c: InvezgoClient) -> None:
    section("1. QUOTA AND SUBSCRIPTION SCOPE")
    print("Three different tier vocabularies exist in Invezgo's own material "
          "(Free/Starter/Pro/Advance on the site, Basic/Standard/Professional/"
          "Enterprise in the SDK README, MAX/ELITE in batch.go). None reconcile — "
          "so this endpoint is the only trustworthy answer.\n")

    usage = c.api_usage()
    print(f"  /usage/api        -> {describe(usage)}")
    if usage:
        save("usage", usage)
        for d in walk(usage):
            nums = numeric_fields(d)
            if nums:
                print(f"    numbers: {nums}")
                break
    else:
        print("    (no data — check the key)")

    scope = c.membership_scope()
    print(f"  /membership/scope -> {describe(scope)}")
    if scope:
        save("membership_scope", scope)
        print(f"    {json.dumps(scope, ensure_ascii=False)[:600]}")


def probe_history(c: InvezgoClient) -> None:
    section("2. HISTORY DEPTH  (decides whether the backtest is 2 years or 2 weeks)")
    today = date.today()
    far_back = (today - timedelta(days=730)).isoformat()
    end = today.isoformat()
    print(f"Asking for {far_back} -> {end} (730 days) and measuring what comes back.\n")

    checks = [
        ("stock_chart", lambda: c.stock_chart(PARITY_SYMBOL, far_back, end)),
        ("broker_stalker_list", lambda: c.broker_stalker_list(PARITY_BROKER, far_back, end)),
        ("broker_summary_stock", lambda: c.broker_summary_stock(PARITY_SYMBOL, far_back, end)),
        ("inventory_chart", lambda: c.inventory_chart(PARITY_SYMBOL, far_back, end)),
    ]

    for name, fn in checks:
        payload = fn()
        if payload is None:
            print(f"  {name:<22} FAILED or empty")
            continue
        save(f"history_{name}", payload)
        dates = find_dates(payload)
        if dates:
            span = "?"
            try:
                d0 = date.fromisoformat(dates[0])
                d1 = date.fromisoformat(dates[-1])
                span = f"{(d1 - d0).days} calendar days"
            except Exception:
                pass
            print(f"  {name:<22} {len(dates):>4} distinct dates | "
                  f"{dates[0]} -> {dates[-1]} ({span})")
            if dates[0] > far_back:
                print(f"  {'':<22} ^ TRUNCATED — asked from {far_back}")
        else:
            print(f"  {name:<22} no dates found | shape: {describe(payload)}")


def probe_intraday(c: InvezgoClient) -> None:
    section("3. INTRADAY — does it accept a date? (can intraday ever be backtested?)")
    print("The SDK signature is (code, market) with no from/to, which suggests today-\n"
          "only. If a `date` param is silently ignored, the two payloads below will be\n"
          "identical and intraday stays forward-capture only.\n")

    live = c.intraday(PARITY_SYMBOL)
    print(f"  no date   -> {describe(live)}")
    save("intraday_nodate", live)

    dated = c.get(f"/analysis/intraday/{PARITY_SYMBOL}",
                  {"market": MARKET_REGULAR, "date": PARITY_DATE})
    print(f"  date={PARITY_DATE} -> {describe(dated)}")
    save("intraday_dated", dated)

    if live is None and dated is None:
        print("\n  VERDICT: both empty — likely outside market hours. Re-run during a "
              "session before concluding anything.")
    elif dated is None:
        print("\n  VERDICT: date rejected -> intraday is TODAY-ONLY. Forward-capture "
              "only, as the plan assumes.")
    elif json.dumps(live, sort_keys=True) == json.dumps(dated, sort_keys=True):
        print("\n  VERDICT: date IGNORED (identical payloads) -> TODAY-ONLY. "
              "Forward-capture only, as the plan assumes.")
    else:
        print("\n  VERDICT: date IS honoured -> intraday CAN be backfilled. "
              "This upgrades the plan: microstructure features become backtestable.")


def probe_batch(c: InvezgoClient) -> None:
    section("4. BATCH — does it work, and what is the real symbol ceiling?")
    print("batch.go comments say 'max 3 for MAX, 10 for ELITE' and do not enforce it "
          "client-side.\nMeasuring rather than trusting.\n")

    for symbols in (["BBCA", "BBRI", "BMRI"],
                    ["BBCA", "BBRI", "BMRI", "TLKM", "ASII",
                     "UNVR", "ICBP", "INDF", "KLBF", "PGAS"]):
        payload = c.intraday_batch(symbols)
        n = len(symbols)
        if payload is None:
            print(f"  {n:>2} symbols -> rejected/empty")
            continue
        returned = {
            str(v).upper().removesuffix(".JK")
            for d in walk(payload)
            for k, v in d.items()
            if ("symbol" in str(k).lower() or "code" in str(k).lower())
            and isinstance(v, str)
        }
        hit = returned & set(symbols)
        print(f"  {n:>2} symbols -> {describe(payload)} | matched {len(hit)}/{n}")
        save(f"batch_{n}", payload)

    print("\n  NOTE: each batch call counted as exactly 1 request by the client. "
          "Reconcile the\n  final request total below against /usage/api to confirm "
          "the server agrees.")


def probe_boards_and_parity(c: InvezgoClient) -> None:
    section("5 + 6. BOARD FILTER  and  THE PARITY GATE")
    print(f"Sectors ground truth — {PARITY_BROKER} on {PARITY_SYMBOL} {PARITY_DATE}:")
    for k, v in PARITY_EXPECTED.items():
        print(f"    {k:<16} {v:,}" if isinstance(v, int) else f"    {k:<16} {v}")
    print("\nSectors does not state which board its figures cover, so every value of "
          "`market`\nis tried — that settles the board question and the parity "
          "question together.\n")

    # No "all" — the API rejects it with 422: "Market must be one of: RG, NG, TN".
    results = {}
    for market in (MARKET_REGULAR, MARKET_CASH, MARKET_NEGOTIATED):
        payload = c.broker_summary_stock(
            PARITY_SYMBOL, PARITY_DATE, PARITY_DATE, market=market)
        save(f"parity_{market}", payload)
        if payload is None:
            print(f"  market={market:<4} -> empty/failed")
            continue

        row = find_row(payload, PARITY_BROKER)
        if not row:
            print(f"  market={market:<4} -> {describe(payload)} | "
                  f"no {PARITY_BROKER} row found")
            continue

        nums = numeric_fields(row)
        results[market] = nums
        print(f"  market={market:<4} -> {PARITY_BROKER} row: {nums}")

    print("\n  --- reconciliation ---")
    if not results:
        print("  NO ROWS FOUND ON ANY BOARD. Either the schema differs from every "
              "guess above\n  (inspect build/probe/parity_*.json by hand) or the data "
              "is not there.\n  DO NOT PROCEED past Phase 0 on this result.")
        return

    best, best_score = None, None
    for market, nums in results.items():
        matched, checked = [], []
        used: set[str] = set()
        for field, expected in PARITY_EXPECTED.items():
            # Match on the numeric value regardless of what the field is called, but
            # never let one response key satisfy two expected fields — otherwise a
            # payload full of 1s and 0s scores well by coincidence and the gate
            # rubber-stamps bad data.
            for k, v in nums.items():
                if k in used:
                    continue
                if rel(v, expected) <= PARITY_TOLERANCE:
                    matched.append(f"{field}~{k}")
                    used.add(k)
                    break
            checked.append(field)
        score = len(matched) / len(checked)
        print(f"  market={market:<4} matched {len(matched)}/{len(checked)} "
              f"fields ({score:.0%})  {matched}")
        if best_score is None or score > best_score:
            best, best_score = market, score

    print()
    if best_score and best_score >= 0.7:
        print(f"  GATE PASSED — market={best} reconciles with Sectors "
              f"({best_score:.0%} of fields within {PARITY_TOLERANCE:.1%}).")
        print(f"  Use market={best} as the panel default and record it in "
              f"reference/broker-alpha.md.")
    else:
        print(f"  GATE FAILED — best was market={best} at {best_score:.0%}.")
        print("  STOP. Do not build the panel on this data until the discrepancy is "
              "explained.\n  Inspect build/probe/parity_*.json and compare field by "
              "field; a units mismatch\n  (lots vs shares, rupiah vs thousands) is the "
              "most likely benign explanation.")


def main() -> int:
    c = InvezgoClient(use_cache=True)
    if not c.enabled:
        print("INVEZGO_API_KEY is not set.\n")
        print("Set it for this machine with:")
        print('    setx INVEZGO_API_KEY "<your key>"')
        print("then restart the app so the process picks up the new environment.\n")
        print("It also has to go in the VPS systemd EnvironmentFile — not just "
              "secrets/.env.\nThe v3 deployment silently blanked an entire column "
              "because SECTORS_API_KEY was\nread from os.environ but only lived in "
              "secrets/.env.")
        return 2

    print(f"Probing Invezgo — cache date {c.date}, output -> {OUT_DIR}")

    probe_quota(c)
    probe_history(c)
    probe_intraday(c)
    probe_batch(c)
    probe_boards_and_parity(c)

    section("REQUEST ACCOUNTING")
    c.report()
    print(f"  client counted {c.requests_used} requests "
          f"({c.cache_hits} served from cache)")
    if c.errors:
        print("\n  errors:")
        for e in c.errors:
            print(f"    - {e}")
    print("\n  Re-check /usage/api now and confirm the server's decrement matches "
          "the count\n  above. If batch calls decrement by more than 1, the quota "
          "budget needs re-basing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
