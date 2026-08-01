#!/usr/bin/env bash
#
# Unattended daily screener run (VPS). Invoked by idx-screener.timer at 07:00 WIB
# on weekdays, or on demand by bot_listener.py when you send /run from your phone.
#
#   ./run_daily.sh [--trigger timer|telegram|manual]
#
# Runs the SAME skill you run interactively, captures Claude's step-8 summary, and
# pushes it to your phone. Secrets come from the systemd EnvironmentFile, never
# from this file.

set -euo pipefail

TRIGGER="manual"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --trigger) TRIGGER="${2:-manual}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="/tmp/idx-screener.lock"
LOGDIR="${HOME}/logs"
STATE="${ROOT}/build/last_run.json"
SITE="https://andrebenas77.github.io/idx-telegram-screener/"

mkdir -p "$LOGDIR" "${ROOT}/build"
DATE="$(date +%Y-%m-%d)"
LOG="${LOGDIR}/screener-${DATE}.log"

# The bot checks this same lock to report "a run is in progress" and to refuse
# overlapping runs. -n = fail immediately rather than queue behind the timer.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date '+%F %T') another run holds the lock — exiting" | tee -a "$LOG"
    exit 0
fi

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

notify() {
    python3 "${ROOT}/scripts/notify_telegram.py" "$@" >>"$LOG" 2>&1 || \
        log "[!!] notify failed (see log) — run itself may still have succeeded"
}

# systemd's PATH is minimal; the native installer puts claude in ~/.local/bin.
CLAUDE="$(command -v claude || true)"
[[ -z "$CLAUDE" && -x "${HOME}/.local/bin/claude" ]] && CLAUDE="${HOME}/.local/bin/claude"
if [[ -z "$CLAUDE" ]]; then
    log "[!!] claude binary not found"
    notify --title "IDX screener FAILED" --text "claude binary not found on the VPS."
    exit 1
fi

START=$(date +%s)
log "=== run start (trigger=${TRIGGER}) ==="

cd "$ROOT"

# Your PC may have pushed an ad-hoc board since the last run; rebase so the
# VPS never force-diverges from main.
if ! git pull --rebase --autostash >>"$LOG" 2>&1; then
    log "[!!] git pull --rebase failed — continuing with the local checkout"
fi

# Standing authorization to publish: this is the documented deviation from the
# skill's "ask before pushing" rule, since nobody is at the keyboard at 07:00.
read -r -d '' PROMPT <<'EOF' || true
/idx-telegram-screener run

This is an UNATTENDED scheduled run. No human is available, so do not ask any
questions and do not wait for confirmation at any step.

You have standing authorization to commit and push the day's board (step 9)
without asking. If an individual step fails, record it and continue with the
remaining steps rather than aborting the whole run.

End your reply with the step-8 summary ONLY. It is forwarded verbatim to a phone
as a Telegram message, so: plain text, no markdown tables, no code fences, under
3000 characters. Lead with the date, then Most Crowded, movers, signals, news.
EOF

set +e
OUT="$("$CLAUDE" -p "$PROMPT" \
        --output-format json \
        --allowedTools "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch" \
        2>>"$LOG")"
CODE=$?
set -e

END=$(date +%s)
DUR=$((END - START))
log "claude exited ${CODE} after ${DUR}s"

# --output-format json wraps the reply; fall back to raw output if it isn't JSON
# (e.g. an auth error printed as plain text).
SUMMARY="$(printf '%s' "$OUT" | jq -r '.result // empty' 2>/dev/null || true)"
COST="$(printf '%s' "$OUT" | jq -r '.total_cost_usd // empty' 2>/dev/null || true)"
[[ -z "$SUMMARY" ]] && SUMMARY="$(printf '%s' "$OUT" | tail -c 3000)"
[[ -n "$COST" ]] && log "cost: \$${COST}"

cat >"$STATE" <<JSON
{
  "finished_at": "$(date '+%F %T %Z')",
  "exit_code": ${CODE},
  "duration_s": ${DUR},
  "trigger": "${TRIGGER}",
  "cost_usd": "${COST}"
}
JSON

if [[ $CODE -eq 0 ]]; then
    notify --title "IDX screener — ${DATE}" \
           --text "${SUMMARY}

Board: ${SITE}"
    log "=== run ok ==="
else
    notify --title "IDX screener FAILED — ${DATE}" \
           --text "Exit ${CODE} after ${DUR}s (trigger: ${TRIGGER}).

Last output:
${SUMMARY}

Log: ${LOG}"
    log "=== run FAILED ==="
    exit "$CODE"
fi
