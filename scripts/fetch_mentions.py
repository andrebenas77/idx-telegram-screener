#!/usr/bin/env python3
"""
Read recent messages from your Telegram channels, count ticker mentions, and
record today's counts. Requires a one-time login first (see tg_login.py).

    py scripts/fetch_mentions.py              # uses fetch_lookback_hours from config
    py scripts/fetch_mentions.py --since 48h  # override lookback window
    py scripts/fetch_mentions.py --date 2026-07-24   # force the session date label

Outputs:
    build/mentions-YYYY-MM-DD.json     detail for spot-checking (git-ignored)
    data/history.csv                   appended daily: date,ticker,posts,channels
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / "secrets" / ".env"
SESSION = ROOT / "secrets" / "screener"
CONFIG = ROOT / "reference" / "config.json"
CHANNELS = ROOT / "reference" / "channels.txt"
TICKERS = ROOT / "reference" / "tickers.csv"
BUILD = ROOT / "build"
HISTORY = ROOT / "data" / "history.csv"

WIB = timezone(timedelta(hours=7))  # Asia/Jakarta, no DST


def load_env(path):
    if not path.exists():
        sys.exit(f"Missing {path} — copy secrets/.env.example to secrets/.env and fill it in.")
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_channels(path):
    if not path.exists():
        sys.exit(f"Missing {path}")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    if not out:
        sys.exit("No channels listed in reference/channels.txt — add at least one and retry.")
    return out


def normalize_title(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_channel(line):
    """Return (identifier, display_label, kind) where kind is username|id|title."""
    s = line.strip()
    low = s.lower()
    if low.startswith("name:") or low.startswith("title:"):
        q = s.split(":", 1)[1].strip()
        return q, q, "title"
    if "t.me/c/" in low:
        digits = re.sub(r"\D", "", low.split("t.me/c/", 1)[1].split("/")[0])
        if digits:
            return int("-100" + digits), f"c/{digits}", "id"
    if "t.me/" in low:
        name = s.split("t.me/", 1)[1].strip("/").split("/")[0].lstrip("@")
        return name, "@" + name, "username"
    if s.startswith("@"):
        return s[1:], s, "username"
    if re.fullmatch(r"-?100\d+", s) or re.fullmatch(r"-\d+", s):
        return int(s), s, "id"
    return s, "@" + s, "username"


def load_tickers(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("ticker"):
                continue
            aliases = [a.strip() for a in (r.get("aliases") or "").split("|") if a.strip()]
            rows.append({
                "ticker": r["ticker"].strip().upper(),
                "company": (r.get("company") or "").strip(),
                "aliases": aliases,
                "sector": (r.get("sector") or "").strip(),
                "liquid": str(r.get("liquid", "0")).strip() in ("1", "true", "True"),
                "ambiguous": str(r.get("ambiguous", "0")).strip() in ("1", "true", "True"),
            })
    return rows


def compile_matchers(tickers):
    matchers = []
    for t in tickers:
        code = t["ticker"]
        pats = [re.compile(r"\$" + re.escape(code), re.IGNORECASE)]  # cashtag, any case
        if not t["ambiguous"]:
            # bare code, must be UPPERCASE and a standalone token (cuts false positives)
            pats.append(re.compile(r"(?<![A-Za-z0-9$])" + re.escape(code) + r"(?![A-Za-z0-9])"))
        for alias in t["aliases"]:
            pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])",
                                   re.IGNORECASE))
        matchers.append((code, pats))
    return matchers


def tickers_in(text, matchers):
    found = set()
    for code, pats in matchers:
        for p in pats:
            if p.search(text):
                found.add(code)
                break
    return found


def parse_since(s, default_hours):
    if not s:
        return default_hours
    s = s.strip().lower()
    if s.endswith("h"):
        return float(s[:-1])
    if s.endswith("d"):
        return float(s[:-1]) * 24
    return float(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="lookback window, e.g. 30h or 2d (default from config)")
    ap.add_argument("--date", help="session date label YYYY-MM-DD (default: today WIB)")
    args = ap.parse_args()

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    lookback = parse_since(args.since, config.get("fetch_lookback_hours", 30))
    session_date = args.date or datetime.now(WIB).strftime("%Y-%m-%d")

    env = load_env(ENV)
    api_id, api_hash = env.get("TELEGRAM_API_ID"), env.get("TELEGRAM_API_HASH")
    if not (api_id and api_hash):
        sys.exit("Fill TELEGRAM_API_ID / TELEGRAM_API_HASH in secrets/.env")

    channels = load_channels(CHANNELS)
    tickers = load_tickers(TICKERS)
    companies = {t["ticker"]: t["company"] for t in tickers}
    matchers = compile_matchers(tickers)

    try:
        from telethon.sync import TelegramClient
    except ImportError:
        sys.exit("Telethon not installed. Run:  py -m pip install telethon")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)
    posts = defaultdict(int)                      # ticker -> message count
    chan_map = defaultdict(set)                   # ticker -> set of channel labels
    messages_scanned = 0
    channels_ok = []
    channels_failed = []

    print(f"Session date {session_date} (WIB) | lookback {lookback:g}h | {len(channels)} channels")
    client = TelegramClient(str(SESSION), int(api_id), api_hash)
    client.connect()  # connect only — never auto-prompt during an automated run
    if not client.is_user_authorized():
        client.disconnect()
        sys.exit("Not logged in. Run once (in your own terminal):  py scripts/tg_login.py")
    dialog_idx = None       # normalized title -> entity (built lazily for name/private channels)
    dialog_titles = []

    def build_dialog_index():
        idx, titles = {}, []
        for d in client.iter_dialogs():
            if getattr(d, "is_channel", False) or getattr(d, "is_group", False):
                t = d.title or ""
                idx[normalize_title(t)] = d.entity
                titles.append(t)
        return idx, titles

    try:
        for line in channels:
            ident, label, kind = parse_channel(line)
            try:
                if kind == "title":
                    if dialog_idx is None:
                        dialog_idx, dialog_titles = build_dialog_index()
                    key = normalize_title(ident)
                    ent = dialog_idx.get(key)
                    if ent is None:  # fall back to a unique substring match
                        cand = [e for nk, e in dialog_idx.items() if key and (key in nk or nk in key)]
                        ent = cand[0] if len(cand) == 1 else None
                    if ent is None:
                        channels_failed.append(label)
                        print(f"  [!!] name '{ident}' not found in your joined channels. Yours are:")
                        for t in sorted(dialog_titles):
                            print(f"         - {t}")
                        continue
                    label = getattr(ent, "title", None) or label
                else:
                    try:
                        ent = client.get_entity(ident)
                    except Exception:
                        ent = ident
                count_here = 0
                for msg in client.iter_messages(ent):
                    if msg.date is None:
                        continue
                    if msg.date < cutoff:
                        break
                    text = msg.message or ""
                    if not text:
                        continue
                    messages_scanned += 1
                    count_here += 1
                    for code in tickers_in(text, matchers):
                        posts[code] += 1
                        chan_map[code].add(label)
                channels_ok.append(label)
                print(f"  [ok] {label:<28} {count_here} msgs in window")
            except Exception as e:
                channels_failed.append(label)
                print(f"  [!!] {label:<28} FAILED: {e}")
    finally:
        client.disconnect()

    total_ticker_posts = sum(posts.values())
    per_ticker = []
    for code in sorted(posts, key=lambda c: (-posts[c], c)):
        per_ticker.append({
            "ticker": code,
            "company": companies.get(code, ""),
            "posts": posts[code],
            "channels": len(chan_map[code]),
            "channel_list": sorted(chan_map[code]),
            "share": round(posts[code] / total_ticker_posts, 4) if total_ticker_posts else 0.0,
        })

    BUILD.mkdir(exist_ok=True)
    detail = {
        "date": session_date,
        "generated_at": datetime.now(WIB).isoformat(timespec="seconds"),
        "lookback_hours": lookback,
        "messages_scanned": messages_scanned,
        "total_ticker_posts": total_ticker_posts,
        "channels_scanned": channels_ok,
        "channels_failed": channels_failed,
        "tickers": per_ticker,
    }
    outjson = BUILD / f"mentions-{session_date}.json"
    outjson.write_text(json.dumps(detail, indent=2, ensure_ascii=False), encoding="utf-8")

    # Update history.csv idempotently for this session date
    HISTORY.parent.mkdir(exist_ok=True)
    rows = []
    if HISTORY.exists():
        with open(HISTORY, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("date") != session_date]
    for t in per_ticker:
        rows.append({"date": session_date, "ticker": t["ticker"],
                     "posts": t["posts"], "channels": t["channels"]})
    rows.sort(key=lambda r: (r["date"], r["ticker"]))
    with open(HISTORY, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "ticker", "posts", "channels"])
        w.writeheader()
        w.writerows(rows)

    print(f"\nScanned {messages_scanned} messages across {len(channels_ok)} channels.")
    if channels_failed:
        print(f"Failed channels: {', '.join(channels_failed)}")
    print(f"Distinct tickers mentioned: {len(per_ticker)} | total ticker-posts: {total_ticker_posts}")
    print("\nTop 15 today:")
    print(f"  {'TICKER':<8}{'POSTS':>6}{'CHANS':>7}  COMPANY")
    for t in per_ticker[:15]:
        print(f"  {t['ticker']:<8}{t['posts']:>6}{t['channels']:>7}  {t['company']}")
    print(f"\nDetail -> {outjson}")
    print(f"History -> {HISTORY}")
    print("Next:  py scripts/build_screener.py")


if __name__ == "__main__":
    main()
