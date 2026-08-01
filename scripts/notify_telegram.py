#!/usr/bin/env python3
"""
Send a message to your own Telegram chat via a bot (used by run_daily.sh to push
the morning summary to your phone).

    py scripts/notify_telegram.py --text "hello"
    cat summary.txt | py scripts/notify_telegram.py
    py scripts/notify_telegram.py --whoami        # find your chat id, first-time setup

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, taken from the real environment
first (systemd EnvironmentFile on the VPS) and falling back to secrets/.env.

Standard library only — no new dependencies on top of Telethon.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / "secrets" / ".env"

API = "https://api.telegram.org/bot{token}/{method}"

# Telegram hard-caps a message at 4096 characters; leave headroom for the header.
CHUNK = 3800


def load_env(path):
    """Environment wins over secrets/.env, so systemd can inject without editing files."""
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(key):
            env[key] = os.environ[key].strip()
    return env


def call(token, method, params):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        sys.exit(f"[!!] Telegram API {method} failed: HTTP {e.code} — {body}")
    except urllib.error.URLError as e:
        sys.exit(f"[!!] Telegram API {method} unreachable: {e.reason}")


def split_message(text, limit=CHUNK):
    """Split on line boundaries so a table or list never breaks mid-row."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        # A single line longer than the limit still has to be cut somewhere.
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    return chunks


def whoami(token):
    """Print every chat that has messaged this bot — how you find your own chat id."""
    res = call(token, "getUpdates", {"timeout": 0})
    seen = {}
    for upd in res.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            name = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
            seen[chat["id"]] = name
    if not seen:
        print("No messages yet. Open Telegram, send your bot any message, then re-run this.")
        return
    print("Chats that have messaged this bot:")
    for cid, name in seen.items():
        print(f"  TELEGRAM_CHAT_ID={cid}   ({name})")


def main():
    ap = argparse.ArgumentParser(description="Send a Telegram message via your bot.")
    ap.add_argument("--text", help="Message text. If omitted, read from stdin.")
    ap.add_argument("--title", default="", help="Optional first line, sent as-is.")
    ap.add_argument("--whoami", action="store_true", help="List chat ids that messaged the bot.")
    ap.add_argument("--silent", action="store_true", help="Deliver without a notification sound.")
    args = ap.parse_args()

    env = load_env(ENV)
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token or "paste_your" in token:
        sys.exit("TELEGRAM_BOT_TOKEN not set — get one from @BotFather, see deploy/VPS-SETUP.md")

    if args.whoami:
        whoami(token)
        return

    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not chat_id or "paste_your" in chat_id:
        sys.exit("TELEGRAM_CHAT_ID not set — run:  py scripts/notify_telegram.py --whoami")

    text = args.text if args.text is not None else sys.stdin.read()
    text = text.strip()
    if not text:
        sys.exit("Nothing to send (empty text).")
    if args.title:
        text = f"{args.title}\n\n{text}"

    # Sent as plain text on purpose: the summary is arbitrary model output and
    # Telegram's MarkdownV2 rejects unescaped '.', '-', '(' etc. with an HTTP 400.
    parts = split_message(text)
    for i, part in enumerate(parts, 1):
        suffix = f"\n\n({i}/{len(parts)})" if len(parts) > 1 else ""
        call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": part + suffix,
            "disable_web_page_preview": "true",
            "disable_notification": "true" if args.silent else "false",
        })
    print(f"[ok] Sent {len(parts)} message(s) to chat {chat_id}.")


if __name__ == "__main__":
    main()
