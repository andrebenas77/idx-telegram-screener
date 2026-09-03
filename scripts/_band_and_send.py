#!/usr/bin/env python3
"""Build the /band write-up for one symbol and deliver it. Spawned detached by bot_listener.

Separate from the listener so the poll loop never blocks on Yahoo (a cold fetch can take
seconds, a stalled one up to ~90s), and separate from name_band so delivery can fail without
taking the probe down with it: text first, then the chart, and every failure becomes a
message rather than silence.

Telegram calls here raise HTTPError instead of exiting: notify_telegram.call() sys.exit()s on
a 4xx, which `except Exception` does not catch — an oversize text would kill the child
silently. See bot_listener.call for the same choice.

    python3 scripts/_band_and_send.py DSSA
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import name_band  # noqa: E402
import notify_telegram as nt  # noqa: E402

WIB = timezone(timedelta(hours=7))
LOCK = Path("/tmp/idx-band.lock")
LOCK_WAIT_S = 45


def _call(token, method, params):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(nt.API.format(token=token, method=method), data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _msg(token, chat, text) -> bool:
    ok = True
    for chunk in nt.split_message(text):
        try:
            _call(token, "sendMessage", {"chat_id": chat, "text": chunk,
                                         "disable_web_page_preview": "true"})
        except Exception as e:                                   # noqa: BLE001
            print(f"[!!] could not send: {e!r}", file=sys.stderr)
            ok = False
    return ok


def _take_lock():
    """Serialise /band children so two never race on the same cache file. No-op off Linux."""
    try:
        import fcntl
    except ImportError:
        return None
    fh = open(LOCK, "w")
    deadline = time.time() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() > deadline:
                fh.close()
                raise TimeoutError("another /band is still running")
            time.sleep(0.5)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _band_and_send.py SYMBOL", file=sys.stderr)
        return 2
    sym = sys.argv[1].upper().removesuffix(".JK")
    env = nt.load_env(nt.ENV)
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[!!] telegram credentials absent", file=sys.stderr)
        return 1

    try:
        lock = _take_lock()
    except TimeoutError as e:
        _msg(token, chat, f"{sym}: {e} - try again in a moment.")
        return 1

    now = datetime.now(WIB)
    cold = not (name_band.R.CACHE / now.date().isoformat() / f"{sym}.json").exists()
    if cold:
        _msg(token, chat, f"Fetching two years of bars for {sym} ...")

    try:
        before = name_band.R.fingerprints()
        ctx = name_band.build(sym, now=now, log=lambda *x: print(*x, file=sys.stderr))
        text = name_band.render_text(ctx)
        if not _msg(token, chat, text):
            print("[!!] text delivery failed", file=sys.stderr)
        if ctx.get("error"):
            name_band.R.assert_unmoved(before)
            return 2
        png = name_band.OUT_DIR / f"{sym}-{ctx['session']}.png"
        name_band.render_png(ctx, png)
        cap = (f"{sym} · session {ctx['session']} · RVOL5 "
               + (f"{ctx['score']['rvol5']:.2f}" if ctx["score"]["rvol5"] is not None else "-")
               + f" · {ctx['score']['cell']}")
        if not nt.send_photo(token, chat, png, caption=cap):
            if not nt.send_document(token, chat, png, caption=cap):
                _msg(token, chat, f"The chart could not be uploaded; it is on the VPS at {png}.")
        name_band.R.assert_unmoved(before)
        return 0
    except SystemExit as e:
        _msg(token, chat, f"{sym}: the write-up stopped: {e}")
        return 1
    except Exception as e:                                       # noqa: BLE001
        _msg(token, chat, f"{sym}: something went wrong building the write-up: {e!r}")
        print(f"[!!] {e!r}", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            try:
                lock.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
