#!/usr/bin/env python3
"""Build the workbook and deliver it. Spawned detached by /export.

Separate from trade_bot so the poll loop never blocks on a panel load, and separate from
export_book so delivery can fail without losing the file — the workbook is written to
disk first and only then uploaded, so a Telegram outage costs you a message, not the
export.
"""
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notify_telegram as nt  # noqa: E402
from position_book import BOOK  # noqa: E402

WIB = timezone(timedelta(hours=7))


def _msg(token, chat, text):
    """notify_telegram exposes call(), not a send() helper — main() does the sending."""
    try:
        nt.call(token, "sendMessage", {"chat_id": chat, "text": text,
                                       "disable_web_page_preview": "true"})
    except Exception as e:                                     # noqa: BLE001
        print(f"[!!] could not send: {e!r}", file=sys.stderr)


def main() -> int:
    env = nt.load_env(nt.ENV)
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[!!] telegram credentials absent", file=sys.stderr)
        return 1

    day = datetime.now(WIB).strftime("%Y-%m-%d")
    out = BOOK / "exports" / f"book-{day}.xlsx"
    r = subprocess.run([sys.executable,
                        str(Path(__file__).resolve().parent / "export_book.py"),
                        "--out", str(out)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        _msg(token, chat, "The export failed:\n" + (r.stderr or r.stdout)[-600:])
        return 1

    tail = [l for l in r.stdout.splitlines() if "rows x" in l or l.startswith("ledger:")]
    if nt.send_document(token, chat, out, caption=f"Book export {day}\n" + "\n".join(tail)):
        print(f"sent {out}")
        return 0
    _msg(token, chat, f"The workbook was built ({out.stat().st_size // 1024} KB) but the "
                      f"upload failed. It is on the VPS at {out}.")
    print("upload failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
