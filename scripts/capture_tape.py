#!/usr/bin/env python3
"""Archive broker-tagged tape from Invezgo, one request per name per board.

TWO MEASURED FACTS SHAPE THIS SCRIPT (see reference/invezgo.md for the full probe):

1. **IDX masks broker codes during the session.** Live tape returns buyer/seller as "--"
   with empty dom flags; the SAME ticks on a closed session return real codes (CC, YU, ZP)
   with F/D flags. This is exchange policy, not a vendor limit. So the entire value of this
   feed is only available AFTER the close — the default date is the last CLOSED session, and
   the script refuses to silently archive anonymised ticks.

2. **`limit` caps at 150 rows/page**, so a full BBCA session is ~139 requests. Rather than
   page all of it, `orderby=VOLUME&sort=DESC` returns the 150 largest tickets in ONE
   request. Bandarmology lives in the big prints; retail one-lots are noise. Use `--full`
   when you genuinely need every fill.

The NG (negotiated) board is captured alongside RG because it is where crossing trades
settle — same broker on both sides, e.g. CC->CC for 19.7m shares. That is the crossing/block
detector the v4 plan deferred for want of a source, and it costs 1 request per name.

Tape is a forward-capture asset: every session not captured is sample never recovered.

Usage:
    py scripts/capture_tape.py                        # last closed session, crowded names
    py scripts/capture_tape.py --date 2026-08-05
    py scripts/capture_tape.py --codes BBCA,GOTO
    py scripts/capture_tape.py --full BBCA --budget 200
    py scripts/capture_tape.py --budget 3             # exercise the abort path
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from invezgo_client import InvezgoClient, RequestBudgetExceeded, MAX_LIMIT  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BUILD = ROOT / "build"
HISTORY = DATA / "history.csv"
TAPE = DATA / "tape"
WIB = timezone(timedelta(hours=7))

# Broker codes are disclosed after the close. 16:15 is the post-trading finish; allow a
# margin before trusting same-day data.
DISCLOSURE_HOUR = 17

FIELDS = ["date", "code", "board", "time", "price", "volume", "value",
          "buyer", "seller", "buyer_dom", "seller_dom", "type", "avg_price",
          "is_crossing"]


def last_closed_session(now: datetime | None = None) -> str:
    """Most recent session whose broker codes should be disclosed."""
    now = now or datetime.now(WIB)
    d = now
    if now.weekday() < 5 and now.hour >= DISCLOSURE_HOUR:
        return d.strftime("%Y-%m-%d")
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def crowded_names(limit: int) -> list[str]:
    """Most-mentioned tickers from the latest session in history.csv.

    Ranking by crowdedness puts the budget where the tape is most decision-relevant: these
    are the names the screener may flag for exit, and the tape names who is actually selling.
    """
    if not HISTORY.exists():
        return []
    try:
        rows = list(csv.DictReader(HISTORY.read_text(encoding="utf-8").splitlines()))
    except Exception as e:
        print(f"[tape] history.csv unreadable ({e}) — use --codes", file=sys.stderr)
        return []
    if not rows:
        return []
    latest = max(r["date"] for r in rows if r.get("date"))
    todays = [r for r in rows if r.get("date") == latest]
    todays.sort(key=lambda r: int(r.get("posts") or 0), reverse=True)
    seen, out = set(), []
    for r in todays:
        t = (r.get("ticker") or "").strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def extract_rows(resp) -> tuple[list[dict], int | None]:
    """Pull ticks and page count out of the envelope.

    The Go SDK documents `total_page`, the Python SDK types say `totalPage`, and the payload
    may or may not nest under `data`. Accept every combination rather than guess.
    """
    if not isinstance(resp, dict) or not resp:
        return [], None
    body = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    total = body.get("totalPage", body.get("total_page"))
    rows = body.get("data")
    if not isinstance(rows, list):
        rows = resp.get("data") if isinstance(resp.get("data"), list) else []
    return [r for r in rows if isinstance(r, dict)], (total if isinstance(total, int) else None)


def masked_fraction(rows: list[dict]) -> float:
    """Share of ticks with no broker attribution. ~1.0 means an in-session pull."""
    if not rows:
        return 0.0
    masked = sum(1 for r in rows if str(r.get("buyer") or "--").strip() in ("--", ""))
    return masked / len(rows)


def to_record(code: str, date: str, t: dict) -> dict:
    price, vol = t.get("price"), t.get("volume")
    value = (price * vol) if isinstance(price, (int, float)) and isinstance(vol, (int, float)) else None
    buyer, seller = t.get("buyer"), t.get("seller")
    # Same broker both sides = a crossing. The signal the v4 plan wanted a source for.
    crossing = bool(buyer and seller and buyer == seller and buyer not in ("--", ""))
    return {
        "date": date, "code": code,
        "board": t.get("board"), "time": t.get("time"),
        "price": price, "volume": vol, "value": value,
        "buyer": buyer, "seller": seller,
        "buyer_dom": t.get("buyer_dom"), "seller_dom": t.get("seller_dom"),
        "type": t.get("type"), "avg_price": t.get("avg_price"),
        "is_crossing": crossing,
    }


def write_gz(code: str, date: str, records: list[dict]) -> Path:
    out_dir = TAPE / date[:7]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{code}-{date}.csv.gz"
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(records)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Archive Invezgo broker-tagged tape")
    ap.add_argument("--date", default=None,
                    help="session YYYY-MM-DD (default: last CLOSED session)")
    ap.add_argument("--codes", default=None, help="comma-separated; overrides history.csv")
    ap.add_argument("--top-n", type=int, default=25, help="how many crowded names to cover")
    ap.add_argument("--boards", default="RG,NG",
                    help="boards to capture; NG carries the crossings")
    ap.add_argument("--budget", type=int, default=120, help="hard request ceiling")
    ap.add_argument("--full", default=None,
                    help="codes to page in FULL (~1 req per 150 fills; BBCA is ~139)")
    ap.add_argument("--full-max-pages", type=int, default=200)
    ap.add_argument("--allow-masked", action="store_true",
                    help="archive even if broker codes are masked (in-session pull)")
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent requests; the API parallelizes cleanly (~45s each)")
    args = ap.parse_args()

    date = args.date or last_closed_session()
    boards = [b.strip().upper() for b in args.boards.split(",") if b.strip()]

    targets = ([c.strip().upper() for c in args.codes.split(",") if c.strip()]
               if args.codes else crowded_names(args.top_n))
    full_codes = [c.strip().upper() for c in (args.full or "").split(",") if c.strip()]

    if not targets and not full_codes:
        print("[tape] nothing to capture — no --codes and history.csv gave no names",
              file=sys.stderr)
        return 1

    c = InvezgoClient(date=date, budget=args.budget)
    if not c.enabled:
        print("[tape] INVEZGO_API_KEY not set — nothing captured.", file=sys.stderr)
        return 2

    print(f"[tape] session {date} | boards {'+'.join(boards)} | "
          f"{len(targets)} names{' + ' + str(len(full_codes)) + ' full' if full_codes else ''} "
          f"| budget {args.budget}", file=sys.stderr)

    captured: list[dict] = []
    skipped: list[dict] = []
    masked_warned = False
    aborted = None

    try:
        for code in full_codes:
            records, page, total = [], 1, None
            while page <= args.full_max_pages:
                resp = c.running_trade(code, date=date, page=page, limit=MAX_LIMIT)
                if resp is None:
                    skipped.append({"code": code, "mode": "full", "reason": "request failed"})
                    break
                rows, total = extract_rows(resp)
                if not rows:
                    break
                records.extend(to_record(code, date, t) for t in rows)
                if total is not None and page >= total:
                    break
                page += 1
            if records:
                p = write_gz(code, date, records)
                captured.append({"code": code, "mode": "full", "ticks": len(records),
                                 "pages": page, "total_pages": total, "file": str(p)})
                print(f"[tape] {code:<6} FULL  {len(records):>6} ticks / {page} pages",
                      file=sys.stderr)

        # ~45s of server time per request, but concurrent requests complete in the time of
        # one (measured), so the wall clock scales with --workers, not with name count.
        def capture_one(code: str) -> dict | None:
            nonlocal masked_warned
            records: list[dict] = []
            page_counts: dict[str, int | None] = {}
            for board in boards:
                resp = c.top_tickets(code, date=date, n=MAX_LIMIT, market=board)
                if resp is None:
                    skipped.append({"code": code, "board": board, "reason": "request failed"})
                    continue
                rows, total = extract_rows(resp)
                page_counts[board] = total
                if not rows:
                    continue

                mf = masked_fraction(rows)
                if mf > 0.5 and not args.allow_masked:
                    if not masked_warned:
                        masked_warned = True
                        print(f"[tape] SKIPPING masked data: {mf:.0%} of {code} {board} ticks "
                              f"have no broker code.\n       IDX masks brokers DURING the "
                              f"session — this archive would be worthless.\n       Capture "
                              f"after {DISCLOSURE_HOUR}:00 WIB, or pass --allow-masked.",
                              file=sys.stderr)
                    skipped.append({"code": code, "board": board, "reason": "brokers masked"})
                    continue

                records.extend(to_record(code, date, t) for t in rows)

            if not records:
                if not any(s.get("code") == code for s in skipped):
                    skipped.append({"code": code, "reason": "no data (204?)"})
                return None

            p = write_gz(code, date, records)
            crossings = sum(1 for r in records if r["is_crossing"])
            biggest = max((r["volume"] or 0) for r in records)
            print(f"[tape] {code:<6} {len(records):>4} tickets | {crossings:>3} crossings | "
                  f"largest {biggest:>12,} sh", file=sys.stderr)
            return {"code": code, "mode": "top", "ticks": len(records),
                    "crossings": crossings, "max_ticket_shares": biggest,
                    "session_pages": page_counts, "file": str(p)}

        todo = [c_ for c_ in targets if c_ not in full_codes]
        if args.workers > 1 and len(todo) > 1:
            # as_completed, not ex.map: map() surfaces results in submission order, so one
            # worker hitting the budget ceiling discards results from workers that already
            # finished and wrote their files — the manifest would disagree with the disk.
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = {ex.submit(capture_one, code): code for code in todo}
                for fut in as_completed(futures):
                    try:
                        res = fut.result()
                    except RequestBudgetExceeded as e:
                        aborted = aborted or str(e)
                        continue
                    if res:
                        captured.append(res)
        else:
            for code in todo:
                try:
                    res = capture_one(code)
                except RequestBudgetExceeded as e:
                    aborted = aborted or str(e)
                    break
                if res:
                    captured.append(res)

    except RequestBudgetExceeded as e:
        # Raised outside the worker loops (e.g. during --full paging).
        aborted = aborted or str(e)

    if aborted:
        print(f"[tape] BUDGET STOP: {aborted}", file=sys.stderr)

    # Reconcile: every requested name must end up either captured or explained. Workers
    # that never got to run leave no trace of their own, so account for them here.
    done = {x["code"] for x in captured}
    for code in targets + full_codes:
        if code not in done and not any(s.get("code") == code for s in skipped):
            skipped.append({"code": code,
                            "reason": "budget exhausted" if aborted else "not attempted"})

    c.report()

    manifest = {
        "date": date, "boards": boards,
        "generated_at": datetime.now(WIB).isoformat(),
        "requests_made": c.requests_made, "budget": args.budget,
        "aborted": aborted, "captured": captured, "skipped": skipped,
        "errors": c.errors, "no_content": len(c.no_content),
    }
    BUILD.mkdir(parents=True, exist_ok=True)
    mpath = BUILD / f"tape-{date}.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    total_cross = sum(x.get("crossings", 0) for x in captured)
    print(f"\ncaptured {len(captured)} names ({total_cross} crossings), "
          f"skipped {len(skipped)}, {c.requests_made} requests")
    print(f"manifest: {mpath}")
    # Partial capture is a success: a budget stop must never fail the daily build.
    return 0


if __name__ == "__main__":
    sys.exit(main())
