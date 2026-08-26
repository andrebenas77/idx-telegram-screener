#!/usr/bin/env python3
"""Durable state for the Telegram bot: dedup, pending tickets, outbox.

Everything here lives under `position_book.BOOK` (`data/book/`), which is gitignored on
a public repo and honours `IDX_BOOK_DIR`, so tests get isolation for free and nothing
here can be published by accident.

Why any of this is needed. The listener is `Restart=always`, so it dies and comes back
routinely — a network blip, a deploy, an OOM. Today `bot_listener.py` keeps its update
offset in a local variable and DISCARDS whatever queued while it was down, which is a
reasonable anti-replay choice for `/run` and a bad one for a ledger write: a `/yes` sent
during a ten-second restart window vanishes with no error anywhere. Persisting the offset
makes replay possible; the dedup below makes replay SAFE. Neither works without the other.

The three separate guards, and why one is not enough:

- `update_id` — Telegram's own monotone id. Catches a replayed poll.
- `message_id` — catches an EDIT. Telegram gives an edited message a NEW `update_id`, so
  the first guard cannot see it, but the edit carries the ORIGINAL `message_id`. Without
  this an edited `/open ISAT 500 2540` re-dispatches and writes twice.
- committed-event hashes — catches the same command retyped after a lost confirmation.

Writes are atomic (`os.replace`). A kill mid-write would otherwise leave truncated JSON,
and the next read would find no pending ticket for a trade that is half-recorded.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from position_book import BOOK  # noqa: E402

WIB = timezone(timedelta(hours=7))

SEEN = BOOK / "bot_seen.json"
PENDING = BOOK / "bot_pending.json"
OUTBOX = BOOK / "bot_outbox.jsonl"
CONFIRM = BOOK / "bot_confirm.json"

KEEP_UPDATES = 500      # a few days of normal use; bounded so the file cannot grow forever
KEEP_MESSAGES = 500
KEEP_COMMITS = 50
TICKET_TTL_S = 600      # 10 minutes — see is_expired()
REPLAY_MAX_AGE_S = 900  # 15 minutes — see too_old()


def now_wib() -> str:
    return datetime.now(WIB).isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt state file must never stop the bot answering. Losing dedup history
        # degrades to "might re-ask for confirmation", which is safe; refusing to start
        # is not.
        return default


# ------------------------------------------------------------------------------ seen

def load_seen() -> dict:
    d = _read(SEEN, {})
    d.setdefault("version", 1)
    d.setdefault("offset", None)
    d.setdefault("seen_updates", [])
    d.setdefault("seen_messages", [])
    d.setdefault("committed", [])
    return d


def save_seen(seen: dict) -> None:
    seen["seen_updates"] = seen["seen_updates"][-KEEP_UPDATES:]
    seen["seen_messages"] = seen["seen_messages"][-KEEP_MESSAGES:]
    seen["committed"] = seen["committed"][-KEEP_COMMITS:]
    _atomic_write(SEEN, seen)


def mark_update(seen: dict, update_id: int) -> bool:
    """True if this update is new. False means already dispatched — drop it."""
    if update_id in seen["seen_updates"]:
        return False
    seen["seen_updates"].append(update_id)
    return True


def mark_message(seen: dict, message_id: int) -> bool:
    """True if this message_id is new. False means we have already acted on it."""
    if message_id in seen["seen_messages"]:
        return False
    seen["seen_messages"].append(message_id)
    return True


def record_commit(seen: dict, ev_hash: str, summary: str) -> None:
    seen["committed"].append({"hash": ev_hash, "ts": now_wib(), "summary": summary})


def recent_commit(seen: dict, ev_hash: str, within_s: int = 900) -> dict | None:
    """The same event committed a moment ago — a retype after a lost confirmation."""
    cut = datetime.now(WIB) - timedelta(seconds=within_s)
    for c in reversed(seen.get("committed", [])):
        if c.get("hash") != ev_hash:
            continue
        try:
            if datetime.fromisoformat(c["ts"]) >= cut:
                return c
        except ValueError:
            continue
    return None


def too_old(msg: dict, max_age_s: int = REPLAY_MAX_AGE_S) -> bool:
    """A queued command replayed long after it was typed.

    Same reasoning as `idx-trade-preclose.timer`'s `Persistent=false`: a catch-up run
    firing at 22:00 would tell you to sell into a market that closed six hours ago. A
    `/open` replayed an hour later records a fill at a price that has moved.
    """
    ts = msg.get("date")
    if not ts:
        return False
    return (datetime.now(timezone.utc).timestamp() - float(ts)) > max_age_s


# --------------------------------------------------------------------------- pending

def load_pending() -> dict | None:
    return _read(PENDING, None)


def save_pending(p: dict) -> None:
    _atomic_write(PENDING, p)


def clear_pending() -> None:
    PENDING.unlink(missing_ok=True)


def is_expired(p: dict) -> bool:
    """A ticket you have stopped looking at is one whose price you have forgotten.

    Ten minutes: a fill you are recording is a fact about the last few minutes. Beyond
    that, retyping is cheaper than confirming something you no longer remember.
    """
    try:
        return datetime.fromisoformat(p["expires_ts"]) < datetime.now(WIB)
    except (KeyError, ValueError):
        return True


def new_ticket(*, command: str, raw_text: str, event: dict, ticket_text: str,
               chat_id: str, update_id: int, message_id: int,
               book_fingerprint: str, warnings: list | None = None,
               extra: dict | None = None) -> dict:
    now = datetime.now(WIB)
    return {"version": 1,
            "created_ts": now.isoformat(timespec="seconds"),
            "expires_ts": (now + timedelta(seconds=TICKET_TTL_S)).isoformat(timespec="seconds"),
            "chat_id": str(chat_id), "update_id": update_id, "message_id": message_id,
            "command": command, "raw_text": raw_text,
            # `ts` is deliberately absent from `event` — stamped at commit, because
            # verify enforces monotone timestamps across the whole ledger.
            "event": event, "ticket_text": ticket_text,
            "book_fingerprint": book_fingerprint,
            "warnings": warnings or [], **(extra or {})}


# ---------------------------------------------------------------------------- outbox

def outbox_append(text: str) -> None:
    """A confirmation that could not be delivered. Never merely lost."""
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now_wib(), "text": text}, ensure_ascii=False) + "\n")


def outbox_drain() -> list[str]:
    """Pop everything queued. Called after any successful send."""
    if not OUTBOX.exists():
        return []
    out = []
    for line in OUTBOX.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line)["text"])
            except (json.JSONDecodeError, KeyError):
                pass
    OUTBOX.unlink(missing_ok=True)
    return out


# --------------------------------------------------------------------------- confirm

def mark_confirmed(note: str = "") -> None:
    _atomic_write(CONFIRM, {"ts": now_wib(), "note": note})


def last_confirmed() -> dict | None:
    return _read(CONFIRM, None)


# -------------------------------------------------------------------------- selftest

def _selftest() -> int:
    import shutil
    import tempfile as _tf
    tmp = Path(_tf.mkdtemp(prefix="botstate-"))
    fails = []

    def chk(name, got, want):
        ok = got == want
        print(f"  [{'ok' if ok else '!!'}] {name}: got {got!r} want {want!r}")
        if not ok:
            fails.append(name)

    g = globals()
    for k in ("SEEN", "PENDING", "OUTBOX", "CONFIRM"):
        g[k] = tmp / Path(g[k]).name
    try:
        print("dedup")
        seen = load_seen()
        chk("first update is new", mark_update(seen, 100), True)
        chk("same update is not", mark_update(seen, 100), False)
        chk("first message is new", mark_message(seen, 55), True)
        chk("an EDIT of it is not", mark_message(seen, 55), False)
        save_seen(seen)
        chk("survives a reload", mark_update(load_seen(), 100), False)

        print("bounded growth")
        s2 = load_seen()
        for i in range(KEEP_UPDATES + 250):
            mark_update(s2, 10_000 + i)
        save_seen(s2)
        chk("update list is capped", len(load_seen()["seen_updates"]), KEEP_UPDATES)

        print("commit idempotency")
        s3 = load_seen()
        record_commit(s3, "sha256:abc", "ISAT open")
        save_seen(s3)
        chk("recent commit found", bool(recent_commit(load_seen(), "sha256:abc")), True)
        chk("unknown hash absent", recent_commit(load_seen(), "sha256:zzz"), None)

        print("pending ticket")
        t = new_ticket(command="open", raw_text="/open ISAT 500 2600",
                       event={"type": "open", "symbol": "ISAT"},
                       ticket_text="...", chat_id="1", update_id=1, message_id=2,
                       book_fingerprint="sha256:deadbeef")
        chk("ts omitted from the event", "ts" in t["event"], False)
        save_pending(t)
        chk("round-trips", load_pending()["command"], "open")
        chk("fresh ticket is live", is_expired(load_pending()), False)
        stale = dict(t, expires_ts="2020-01-01T00:00:00+07:00")
        chk("past expiry is expired", is_expired(stale), True)
        chk("malformed is expired", is_expired({}), True)
        clear_pending()
        chk("cleared", load_pending(), None)

        print("outbox")
        outbox_append("undelivered one")
        outbox_append("undelivered two")
        chk("drains in order", outbox_drain(), ["undelivered one", "undelivered two"])
        chk("empty after drain", outbox_drain(), [])

        print("corrupt state does not stop the bot")
        SEEN.write_text("{not json", encoding="utf-8")
        chk("degrades to empty", load_seen()["offset"], None)

        print("replay age")
        import time as _t
        chk("just-typed is fresh", too_old({"date": _t.time()}), False)
        chk("an hour old is stale", too_old({"date": _t.time() - 3600}), True)
        chk("no date is not stale", too_old({}), False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"[!!] {len(fails)} failed: {', '.join(fails)}")
        return 1
    print("[ok] all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
