#!/usr/bin/env bash
# The 07:30 WIB morning report. Renders build_daily_report.py and pushes it to Telegram.
#
# Deliberately NOT folded into run_daily.sh. That job owns the panel refresh, three boards, a
# claude step and a git push; it takes ~150s and any failure inside it is a failure of the
# board. This is a read-only consumer of what it produced, it costs nothing, and it must be able
# to fail without touching anything upstream.
#
# Its own lock, so a slow 07:00 job delays this rather than racing it. run_daily.sh holds
# /tmp/idx-screener.lock; we wait on ours and separately refuse to report on a stale board.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${ROOT}/build/daily-report.log"
mkdir -p "${ROOT}/build"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }

notify() {
    python3 "${ROOT}/scripts/notify_telegram.py" "$@" >>"$LOG" 2>&1 || \
        log "[!!] notify failed (see log)"
}

exec 9>/tmp/idx-daily-report.lock
if ! flock -w 600 9; then
    log "[!!] could not take the lock within 600s -- another run is still going. Exiting."
    exit 0
fi

log "=== daily report start (trigger=${1:-manual}) ==="

# The report is a consumer of the 07:00 job. If that job did not finish, say so rather than
# reporting yesterday's board under today's date -- an absence of a RUN reading as a market
# with nothing in it is the failure mode this whole repo keeps re-learning.
LAST="${ROOT}/build/last_run.json"
if [[ -f "$LAST" ]]; then
    OK=$(python3 -c "import json;print(json.load(open('$LAST')).get('ok'))" 2>/dev/null || echo "?")
    if [[ "$OK" != "True" ]]; then
        log "[!!] the 07:00 run did not finish ok (ok=$OK)"
        notify --title "IDX DAILY report SKIPPED" \
               --text "The 07:00 screener run did not finish ok, so the board on disk cannot be trusted. No report today. Check ${LOG}."
        exit 1
    fi
fi

TEXT="$(cd "$ROOT" && timeout 900 python3 scripts/build_daily_report.py --summary 2>>"$LOG")"
CODE=$?

if [[ $CODE -ne 0 || -z "$TEXT" ]]; then
    log "[!!] build_daily_report.py exited $CODE, ${#TEXT} chars"
    notify --title "IDX DAILY report FAILED" \
           --text "build_daily_report.py exited ${CODE}. Last 800 chars of log:
$(tail -c 800 "$LOG")"
    exit 1
fi

log "rendered ${#TEXT} chars"
notify --text "$TEXT"
log "=== daily report ok ==="
