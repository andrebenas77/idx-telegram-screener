#!/usr/bin/env python3
"""Build the workbook and deliver it. Spawned detached by /export.

Separate from trade_bot so the poll loop never blocks on a panel load, and separate from
export_book so the delivery half can fail without losing the file — the workbook is
written to disk first and only then uploaded.
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notify_telegram as nt  # noqa: E402
from position_book import BOOK  # noqa: E402

WIB = __import__("datetime").timezone(__import__("datetime").timedelta(hours=7))


def main() -> int:
    env = nt.load_env(nt.ENV if hasattr(nt, "ENV") else Path("secrets/.env"))
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    day = datetime.now(WIB).strftime("%Y-%m-%d")
    out = BOOK / "exports" / f"book-{day}.xlsx"
    r = subprocess.run([sys.executable,
                        str(Path(__file__).resolve().parent / "export_book.py"),
                        "--out", str(out)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        nt.send(token, chat, "The export failed:\n" + (r.stderr or r.stdout)[-500:])
        return 1
    tail = [l for l in r.stdout.splitlines() if "rows x" in l or l.startswith("ledger:")]
    ok = nt.send_document(token, chat, out,
                          caption=f"Book export {day}\n" + "\n".join(tail))
    if not ok:
        nt.send(token, chat, f"Workbook built at {out} but the upload failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
