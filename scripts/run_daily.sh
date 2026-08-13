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

# ---------------------------------------------------------------------------
# Momentum board.
#
# Deliberately OUTSIDE the claude step and guarded separately: it is deterministic
# Python with no model in the loop, so a claude failure must not cost us the board,
# and a board failure must not suppress the crowded-board summary. Both feed one
# message at the end.
#
# The refresh REFUSES to write when a corporate action lands inside its window —
# back-adjustment anchors on the newest date, so rescaling only the refreshed slice
# would leave a silent discontinuity against the untouched history. That is a
# non-zero exit, and the right response is a full rebuild, not a retry.
# ---------------------------------------------------------------------------
MOM_SUMMARY=""
MOM_OK=false
ACC_SUMMARY=""
ACC_OK=false
set +e
python3 "${ROOT}/scripts/backfill_panel.py" --incremental --window 90 >>"$LOG" 2>&1
REFRESH_CODE=$?
if [[ $REFRESH_CODE -ne 0 ]]; then
    # Distinguish the causes — an earlier version blamed every non-zero exit on a
    # corporate action, which sent a manual-run env problem chasing the wrong fix.
    case $REFRESH_CODE in
        2) log "[!!] panel refresh: INVEZGO_API_KEY not set. systemd supplies it from"
           log "     /etc/idx-screener.env; a manual shell does NOT — export it first." ;;
        1) log "[!!] panel refresh: corporate action inside the window, so the merged"
           log "     slice would not match older history."
           log "     fix: python3 scripts/backfill_panel.py --refresh-actions" ;;
        *) log "[!!] panel refresh exited ${REFRESH_CODE} — see the log above" ;;
    esac
else
    # Page first, summary second: a summary that linked to a page which failed to
    # rebuild would point at yesterday's board while reading as today's.
    if python3 "${ROOT}/scripts/build_momentum_board.py" >>"$LOG" 2>&1; then
        MOM_SUMMARY="$(python3 "${ROOT}/scripts/build_momentum_board.py" --summary \
                        2>>"$LOG")"
        [[ -n "$MOM_SUMMARY" ]] && MOM_OK=true

        # Publish it. The claude step commits and pushes the crowded board BEFORE this
        # block runs, so without an explicit publish here docs/momentum.html is
        # regenerated after that push and never reaches Pages — the site would serve a
        # stale board while the Telegram summary described today's. Same standing
        # authorization to push as the crowded board: nobody is at the keyboard at 07:00.
        if [[ -n "$(git status --porcelain docs/momentum.html)" ]]; then
            if git add docs/momentum.html \
               && git commit -q -m "Momentum board ${DATE}" >>"$LOG" 2>&1 \
               && git push -q origin main >>"$LOG" 2>&1; then
                log "momentum board published"
            else
                log "[!!] momentum board built but NOT published"
                MOM_OK=false
            fi
        else
            log "momentum board unchanged — nothing to publish"
        fi
    fi

    # ---- accumulation board (the pre-momentum stage) --------------------------------
    # Deliberately a WARN, never a FATAL: it is observation-mode and unvalidated, so a
    # failure here must not take down a run whose crowded and momentum boards are fine.
    # It also needs data/panel/gross-*.csv.gz; without that it renders empty and says so
    # rather than erroring, which is the same degrade-don't-block posture as every other
    # input in this pipeline.
    if python3 "${ROOT}/scripts/build_universe.py" --no-discover >>"$LOG" 2>&1 \
       && python3 "${ROOT}/scripts/build_accum_board.py" >>"$LOG" 2>&1; then
        ACC_SUMMARY="$(python3 "${ROOT}/scripts/build_accum_board.py" --summary \
                        2>>"$LOG")"
        [[ -n "$ACC_SUMMARY" ]] && ACC_OK=true
        if [[ -n "$(git status --porcelain docs/accumulation.html)" ]]; then
            if git add docs/accumulation.html \
               && git commit -q -m "Accumulation board ${DATE}" >>"$LOG" 2>&1 \
               && git push -q origin main >>"$LOG" 2>&1; then
                log "accumulation board published"
            else
                log "[!!] accumulation board built but NOT published"
                ACC_OK=false
            fi
        else
            log "accumulation board unchanged — nothing to publish"
        fi
    fi
fi
set -e
$MOM_OK && log "momentum board built" || log "[!!] momentum board NOT built"
$ACC_OK && log "accumulation board built" || log "[!] accumulation board NOT built"

# --output-format json wraps the reply; fall back to raw output if it isn't JSON
# (e.g. an auth error printed as plain text).
SUMMARY="$(printf '%s' "$OUT" | jq -r '.result // empty' 2>/dev/null || true)"
COST="$(printf '%s' "$OUT" | jq -r '.total_cost_usd // empty' 2>/dev/null || true)"
[[ -z "$SUMMARY" ]] && SUMMARY="$(printf '%s' "$OUT" | tail -c 3000)"
[[ -n "$COST" ]] && log "cost: \$${COST}"

# ---------------------------------------------------------------------------
# Post-run verification.
#
# A zero exit from claude is NOT evidence that the run did anything. If the
# skill fails to resolve — e.g. ~/.claude/skills/idx-telegram-screener is
# missing, which is exactly what happened on this VPS's first run — claude exits
# 0 within seconds having produced nothing, and an unguarded script reports
# "run ok". At 07:00 with nobody watching, that silent no-op looks identical to
# a good day. These checks are what tell the two apart.
#
# FATAL = there is no published board today. WARN = it published, but thinner
# than it should be (a data source was down); still worth reading.
# ---------------------------------------------------------------------------
FATAL=()
WARN=()

# 1. Duration. A real run reads the channels, then prices, brokers and news; the
#    measured baseline on this box is ~230s. Under a minute, it did not run.
MIN_DUR=60
if [[ $DUR -lt $MIN_DUR ]]; then
    FATAL+=("finished in ${DUR}s but a real run takes ~230s — the skill likely never executed")
fi

# 2. fetch_mentions.py writes this on every run; no file means Telegram was
#    never read, which is the whole input to the screen.
if [[ ! -s "${ROOT}/build/mentions-${DATE}.json" ]]; then
    FATAL+=("build/mentions-${DATE}.json missing or empty — Telegram was never read")
fi

# 3. The board that gets served must carry today's date.
if ! grep -q "$DATE" "${ROOT}/docs/index.html" 2>/dev/null; then
    FATAL+=("docs/index.html does not mention ${DATE} — the board was not rebuilt")
fi

# 4. Committed-but-unpushed is invisible from the server yet leaves the site on
#    yesterday's board. This is a real failure that used to pass silently.
if git fetch -q origin main >>"$LOG" 2>&1; then
    UNPUSHED="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)"
    if [[ "${UNPUSHED:-0}" -gt 0 ]]; then
        FATAL+=("${UNPUSHED} commit(s) never pushed — the published site was not updated")
    fi
else
    WARN+=("could not reach GitHub to confirm the board was published")
fi

# 5. Advisory only: a data-source outage still yields a usable board.
[[ -s "${ROOT}/build/prices-${DATE}.json" ]]  || WARN+=("no price data for ${DATE}")
[[ -s "${ROOT}/build/brokers-${DATE}.json" ]] || WARN+=("no broker-flow data for ${DATE}")

# 6. The momentum board is a separate deliverable and gets the same "did it actually
#    rebuild" treatment as the crowded board — an unchanged file at 07:00 looks
#    identical to a good run otherwise. WARN not FATAL: the crowded board is still
#    worth delivering on its own.
if ! $MOM_OK; then
    WARN+=("momentum board not rebuilt — see the log")
elif ! grep -q "session " "${ROOT}/docs/momentum.html" 2>/dev/null; then
    WARN+=("docs/momentum.html has no session line — it may be stale")
fi

# 6b. The accumulation board is observation-mode and unvalidated, so it warns and never
# fails the run. An empty board is a real state (no gross partition yet), not an error.
if ! $ACC_OK; then
    WARN+=("accumulation board not rebuilt — see the log")
elif ! grep -q "${DATE}" "${ROOT}/docs/accumulation.html" 2>/dev/null      && ! grep -q "IDX Accumulation" "${ROOT}/docs/accumulation.html" 2>/dev/null; then
    WARN+=("docs/accumulation.html looks stale")
fi

for w in ${WARN[@]+"${WARN[@]}"};  do log "[warn] $w"; done
for f in ${FATAL[@]+"${FATAL[@]}"}; do log "[!!] $f"; done

OK=true
[[ $CODE -ne 0 || ${#FATAL[@]} -gt 0 ]] && OK=false

# jq -R/-s turns the arrays into JSON safely; the empty case needs handling
# separately because printf on an empty array still emits one blank line.
if [[ ${#FATAL[@]} -eq 0 ]]; then FATAL_JSON='[]'
else FATAL_JSON="$(printf '%s\n' "${FATAL[@]}" | jq -R . | jq -s -c .)"; fi
if [[ ${#WARN[@]} -eq 0 ]]; then WARN_JSON='[]'
else WARN_JSON="$(printf '%s\n' "${WARN[@]}" | jq -R . | jq -s -c .)"; fi

cat >"$STATE" <<JSON
{
  "finished_at": "$(date '+%F %T %Z')",
  "ok": ${OK},
  "exit_code": ${CODE},
  "duration_s": ${DUR},
  "trigger": "${TRIGGER}",
  "cost_usd": "${COST}",
  "problems": ${FATAL_JSON},
  "warnings": ${WARN_JSON}
}
JSON

WARNTEXT=""
if [[ ${#WARN[@]} -gt 0 ]]; then
    WARNTEXT="

Warnings:
$(printf -- '- %s\n' "${WARN[@]}")"
fi

MOMTEXT=""
if [[ -n "$MOM_SUMMARY" ]]; then
    MOMTEXT="

------------------------------
${MOM_SUMMARY}"
fi

# The accumulation board rides in the same message as the other two. notify_telegram.py
# chunks at 3800 chars on line boundaries, so three boards in one text is safe; three
# separate notifications at 07:00 would not be.
ACCTEXT=""
if [[ -n "$ACC_SUMMARY" ]]; then
    ACCTEXT="

------------------------------
${ACC_SUMMARY}"
fi

if $OK; then
    # One message carrying both boards, not two notifications.
    notify --title "IDX screener — ${DATE}" \
           --text "${SUMMARY}

Board: ${SITE}${MOMTEXT}${ACCTEXT}${WARNTEXT}"
    log "=== run ok ==="
else
    REASON=""
    [[ $CODE -ne 0 ]] && REASON="- claude exited ${CODE}"$'\n'
    [[ ${#FATAL[@]} -gt 0 ]] && REASON="${REASON}$(printf -- '- %s\n' "${FATAL[@]}")"
    notify --title "IDX screener FAILED — ${DATE}" \
           --text "The ${TRIGGER} run did not produce a published board.

${REASON}
Ran ${DUR}s.

Last output:
$(printf '%s' "$SUMMARY" | tail -c 1200)

Log: ${LOG}"
    log "=== run FAILED ==="
    exit 1
fi
