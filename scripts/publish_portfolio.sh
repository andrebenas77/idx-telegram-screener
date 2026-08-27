#!/usr/bin/env bash
# Regenerate the private portfolio page and push it.
#
# Three guards, checked before anything is written or pushed. The failure they exist to
# prevent is the only unrecoverable one in this repo: a book on a public, indexed Pages
# site cannot be unpublished. Any one guard would do; all three exist because being
# wrong once is permanent.
#
#   1. build_portfolio.py refuses to write inside the public repo's docs/ tree
#   2. the git toplevel must equal BOOK_REPO_DIR
#   3. the remote must match the configured private remote
#
# And never `git add -A`: the publish is path-scoped to one file, so a stray file in the
# working tree cannot ride along. That is the same idiom run_daily.sh uses for the public
# boards, for the same reason.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENVF="${ROOT}/secrets/portfolio.env"
[[ -f "$ENVF" ]] && set -a && . "$ENVF" && set +a

BOOK_REPO_DIR="${BOOK_REPO_DIR:-$HOME/idx-book}"
BOOK_REMOTE="${BOOK_REMOTE:-git@github-idxbook:andrebenas77/idx-book.git}"
OUT="${BOOK_REPO_DIR}/docs/index.html"
MARKET_ONLY="${1:---market-hours-only}"

log() { echo "$(date '+%F %T') $*"; }

# --- guard 2: are we where we think we are?
if [[ ! -d "${BOOK_REPO_DIR}/.git" ]]; then
    log "[!!] ${BOOK_REPO_DIR} is not a git repo — refusing"; exit 1
fi
TOP="$(git -C "$BOOK_REPO_DIR" rev-parse --show-toplevel 2>/dev/null || echo '')"
if [[ "$(cd "$BOOK_REPO_DIR" && pwd -P)" != "$(cd "$TOP" && pwd -P)" ]]; then
    log "[!!] ${BOOK_REPO_DIR} is not the toplevel of its repo (${TOP}) — refusing"; exit 1
fi

# --- guard 3: is it the repo we mean?
ACTUAL="$(git -C "$BOOK_REPO_DIR" remote get-url origin 2>/dev/null || echo '')"
if [[ "$ACTUAL" != "$BOOK_REMOTE" ]]; then
    log "[!!] remote is '${ACTUAL}', expected '${BOOK_REMOTE}' — refusing to push the book"
    exit 1
fi

# --- guard 1 lives inside the generator
if ! python3 "${ROOT}/scripts/build_portfolio.py" --out "$OUT" ${MARKET_ONLY:+$MARKET_ONLY}; then
    log "[!!] build_portfolio failed"; exit 1
fi
[[ -s "$OUT" ]] || { log "nothing generated (outside the session, or flat)"; exit 0; }

# --- serve it. This, not GitHub Pages, is the destination.
#
# Pages access control needs Enterprise Cloud; Pro publishes PUBLICLY and there is no way
# to restrict it after the fact - which is how the book was briefly exposed on 2026-08-27.
# Caddy on this box serves it over HTTPS behind basic auth instead. The git push below is
# now an ARCHIVE (a dated history of the book), not the delivery mechanism.
WEBROOT="${WEBROOT:-/var/www/idxbook}"
if [[ -d "$WEBROOT" ]]; then
    # -g caddy is load-bearing. `install` creates a NEW file owned by the INVOKING user
    # and their primary group, so without it the scheduled run produces a file the caddy
    # user cannot read and the site answers 404 to a correctly authenticated request.
    # A manual test under `sg caddy` passes, which is how this hid: the interactive check
    # and the timer had different effective groups.
    if install -m 640 -g caddy "$OUT" "${WEBROOT}/index.html" 2>/dev/null; then
        log "served -> ${WEBROOT}/index.html"
        # Prove it rather than assume it. A page nobody can read is worse than no page,
        # because the timer keeps reporting success.
        if ! sudo -n -u caddy test -r "${WEBROOT}/index.html" 2>/dev/null; then
            log "[!!] caddy cannot READ the page it is meant to serve - expect 404"
        fi
    else
        log "[!!] could not write ${WEBROOT} - the page is stale on the server"
    fi
else
    log "[warn] ${WEBROOT} missing - nothing is serving the page"
fi

cd "$BOOK_REPO_DIR"
if [[ -z "$(git status --porcelain docs/index.html)" ]]; then
    log "page unchanged — nothing to publish"; exit 0
fi
# Path-scoped. NEVER `git add -A` in a repo whose whole purpose is holding private numbers.
if git add docs/index.html \
   && git commit -q -m "Portfolio $(date '+%F %H:%M') WIB" \
   && git push -q origin main; then
    log "published $(git rev-parse --short HEAD)"
else
    log "[!!] built but NOT published"; exit 1
fi
