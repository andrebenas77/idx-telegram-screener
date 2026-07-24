#!/usr/bin/env python3
"""
ONE-TIME Telegram login.  Run this yourself in a terminal (PowerShell):

    py scripts/tg_login.py

Telegram will send a login code to your Telegram app; type it in when asked
(and your 2FA/cloud password if you have one). This creates the session file
secrets/screener.session so that every screener run afterwards is automatic.
You only need to do this once (again only if you log out or move machines).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / "secrets" / ".env"
SESSION = ROOT / "secrets" / "screener"   # Telethon appends ".session"


def load_env(path):
    if not path.exists():
        sys.exit(
            f"Missing {path}\n"
            "-> Copy secrets/.env.example to secrets/.env and fill in your keys first."
        )
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        sys.exit("Telethon is not installed. Run:  py -m pip install telethon")

    env = load_env(ENV)
    api_id = env.get("TELEGRAM_API_ID")
    api_hash = env.get("TELEGRAM_API_HASH")
    phone = env.get("TELEGRAM_PHONE")
    if not (api_id and api_hash and phone) or "XXXX" in phone or "paste_your" in api_hash:
        sys.exit("Please fill TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_PHONE in secrets/.env")

    print("Connecting to Telegram... a login code will be sent to your Telegram app.")
    client = TelegramClient(str(SESSION), int(api_id), api_hash)
    client.start(phone=phone)   # connects, sends the code, prompts you for it (and 2FA if set)
    me = client.get_me()
    uname = f"@{me.username}" if me.username else "(no username)"
    print(f"\n[OK] Logged in as {me.first_name} {uname}.")
    print(f"[OK] Session saved -> {SESSION}.session")
    print("You're all set. You won't need to run this again.")
    client.disconnect()


if __name__ == "__main__":
    main()
