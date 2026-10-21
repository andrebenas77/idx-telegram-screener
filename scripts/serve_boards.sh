#!/usr/bin/env bash
# Serve the boards from this box, behind the same basic auth as the position book.
#
# The repo went private on 2026-10-21. It had been public with Pages on since July, which
# published the momentum rule, the trade layer and the broker research to anyone. A private
# repo on the Free plan has no Pages at all, and Pro would publish PUBLICLY again (Pages
# access control needs Enterprise Cloud), so Pages is gone for good. git stays as the
# ARCHIVE of what each board said; this copy is the delivery.
#
# Caddy's site for the book (/etc/caddy/Caddyfile, 103-58-101-93.sslip.io) is a plain
# file_server over /var/www/idxbook with basic_auth on every path, so a boards/ folder
# inside it is served and protected with no Caddy change.
#
#   scripts/serve_boards.sh            copy docs/ pages into $WEBROOT/boards
#
# Warn-not-fail at the call site: a failed copy leaves yesterday's pages on the server, and
# run_daily.sh says so in the morning message.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBROOT="${WEBROOT:-/var/www/idxbook}"
DEST="${WEBROOT}/boards"

log() { echo "$(date '+%F %T') [serve] $*"; }

if [[ ! -d "$WEBROOT" ]]; then
    log "[warn] ${WEBROOT} missing - nothing is serving the boards (expected on the laptop)"
    exit 0
fi

# 2750 + group caddy: the setgid bit keeps every file created later in the caddy group.
# Without it a new file lands in the invoking user's group and caddy answers 404 to a
# correctly authenticated request - the bug publish_portfolio.sh already hit once.
if [[ ! -d "$DEST" ]]; then
    install -d -m 2750 -g caddy "$DEST" || { log "[!!] could not create ${DEST}"; exit 1; }
    install -d -m 2750 -g caddy "${DEST}/archive" || { log "[!!] could not create ${DEST}/archive"; exit 1; }
fi

FAILED=0
shopt -s nullglob
for f in "${ROOT}"/docs/*.html "${ROOT}"/docs/history.csv; do
    install -m 640 -g caddy "$f" "${DEST}/$(basename "$f")" || { log "[!!] copy failed: $f"; FAILED=1; }
done
for f in "${ROOT}"/docs/archive/*.html; do
    install -m 640 -g caddy "$f" "${DEST}/archive/$(basename "$f")" || { log "[!!] copy failed: $f"; FAILED=1; }
done

# Prove caddy can read what was written, by stat, not `sudo -u caddy test -r` (a timer
# cannot sudo, so that check fails on good runs too; see publish_portfolio.sh).
GRP="$(stat -c '%G' "${DEST}/index.html" 2>/dev/null || echo '?')"
MODE="$(stat -c '%a' "${DEST}/index.html" 2>/dev/null || echo '000')"
if [[ "$GRP" != "caddy" || "${MODE:1:1}" -lt 4 ]]; then
    log "[!!] ${DEST}/index.html is ${GRP} mode ${MODE} - caddy cannot read it, expect 404"
    exit 1
fi

[[ $FAILED -eq 0 ]] || exit 1
log "served $(ls "$DEST"/*.html 2>/dev/null | wc -l) pages + $(ls "$DEST"/archive/*.html 2>/dev/null | wc -l) archive pages -> ${DEST}"
