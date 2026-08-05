#!/usr/bin/env python3
"""
Telegram bot listener — lets you re-run the screener from your phone.

    py scripts/bot_listener.py

Long-polls getUpdates and responds to:
    /run      trigger a screener run now (replies immediately; the run itself
              messages you when the board is live)
    /status   last run result + whether one is in progress
    /help     list commands

SECURITY: only TELEGRAM_CHAT_ID is obeyed. A bot's username is discoverable, so
without this whitelist anyone who found the bot could execute runs on your box.
Every message from any other chat is logged and dropped.

Runs under systemd as idx-bot.service. Standard library only.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / "secrets" / ".env"
RUNNER = ROOT / "scripts" / "run_daily.sh"
LOCK = Path("/tmp/idx-screener.lock")
STATE = ROOT / "build" / "last_run.json"

API = "https://api.telegram.org/bot{token}/{method}"
WIB = timezone(timedelta(hours=7))
POLL_TIMEOUT = 30

HELP = (
    "IDX Telegram Screener\n\n"
    "/run — run the screener now\n"
    "/status — last run + whether one is in progress\n"
    "/help — this message\n\n"
    "The scheduled run fires automatically at 07:00 WIB on weekdays."
)


def log(msg):
    print(f"{datetime.now(WIB):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_env(path):
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


def call(token, method, params, timeout=60):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def send(token, chat_id, text):
    try:
        call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        })
    except Exception as e:                                    # noqa: BLE001
        log(f"[!!] send failed: {e}")


def run_in_progress():
    """A run holds an exclusive flock on LOCK for its whole duration."""
    if not LOCK.exists():
        return False
    try:
        import fcntl
        with open(LOCK, "r") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
                return False        # we could take it, so nobody is running
            except BlockingIOError:
                return True         # someone else holds it
    except Exception:                                          # noqa: BLE001
        return False


def status_text():
    if run_in_progress():
        return "A run is in progress right now."
    if not STATE.exists():
        return "No run recorded yet on this machine."
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "Last run state file is unreadable."
    # "ok" is the verified verdict: claude can exit 0 having produced nothing, so
    # exit_code alone would report a silent no-op as success. Older state files
    # predate the field, hence the fallback.
    if "ok" in st:
        good = bool(st["ok"])
    else:
        good = st.get("exit_code") == 0
    verdict = "ok" if good else f"FAILED (exit {st.get('exit_code')})"

    problems = st.get("problems") or []
    warnings = st.get("warnings") or []
    lines = [
        f"Last run: {st.get('finished_at', '?')}",
        f"Result: {verdict}",
        f"Trigger: {st.get('trigger', '?')}",
        f"Duration: {st.get('duration_s', '?')}s",
    ]
    if problems:
        lines.append("\nProblems:\n" + "\n".join(f"- {p}" for p in problems))
    if warnings:
        lines.append("\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings))
    return "\n".join(lines)


def trigger_run(token, chat_id):
    if run_in_progress():
        send(token, chat_id, "A run is already in progress — ignoring this one.")
        return
    if not RUNNER.exists():
        send(token, chat_id, f"Runner not found at {RUNNER}")
        return
    # Detached: a run takes minutes and the poll loop must stay responsive.
    # run_daily.sh sends the summary itself when it finishes.
    subprocess.Popen(
        ["/bin/bash", str(RUNNER), "--trigger", "telegram"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    send(token, chat_id, "Started. I'll message you when the board is live (~2-3 min).")
    log("run triggered from Telegram")


def handle(token, allowed_chat, msg):
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = (msg.get("text") or "").strip()

    if chat_id != allowed_chat:
        log(f"[drop] message from unauthorised chat {chat_id}: {text[:40]!r}")
        return
    if not text.startswith("/"):
        return

    cmd = text.split()[0].lstrip("/").split("@")[0].lower()
    if cmd == "run":
        trigger_run(token, chat_id)
    elif cmd == "status":
        send(token, chat_id, status_text())
    elif cmd in ("help", "start"):
        send(token, chat_id, HELP)
    else:
        send(token, chat_id, f"Unknown command /{cmd}\n\n{HELP}")


def main():
    env = load_env(ENV)
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    allowed_chat = env.get("TELEGRAM_CHAT_ID", "")
    if not token or "paste_your" in token:
        sys.exit("TELEGRAM_BOT_TOKEN not set — see deploy/VPS-SETUP.md")
    if not allowed_chat or "paste_your" in allowed_chat:
        sys.exit("TELEGRAM_CHAT_ID not set — run:  py scripts/notify_telegram.py --whoami")

    log(f"listener up; obeying chat {allowed_chat} only")

    # Drain anything queued while we were down, so a restart doesn't replay old commands.
    offset = None
    try:
        res = call(token, "getUpdates", {"timeout": 0})
        ups = res.get("result", [])
        if ups:
            offset = ups[-1]["update_id"] + 1
            log(f"skipped {len(ups)} queued update(s) from before startup")
    except Exception as e:                                     # noqa: BLE001
        log(f"[!!] initial drain failed: {e}")

    while True:
        try:
            params = {"timeout": POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset
            res = call(token, "getUpdates", params, timeout=POLL_TIMEOUT + 15)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    handle(token, allowed_chat, msg)
        except urllib.error.HTTPError as e:
            log(f"[!!] HTTP {e.code} — backing off 15s")
            time.sleep(15)
        except urllib.error.URLError as e:
            log(f"[!!] network: {e.reason} — backing off 15s")
            time.sleep(15)
        except Exception as e:                                 # noqa: BLE001
            log(f"[!!] unexpected: {e} — backing off 15s")
            time.sleep(15)


if __name__ == "__main__":
    main()
