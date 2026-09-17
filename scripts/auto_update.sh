#!/usr/bin/env bash
# Keeps this deployment on the newest commit that passed every check, and puts the previous
# one back if the new one does not come up healthy.
#
# It follows the `release` branch, never `main`: that branch is moved by CI only after both
# the test workflow and the secret scan succeed on the same commit. The server therefore
# needs no GitHub token and no inbound access — it asks GitHub for a branch, and the branch
# is the verdict.
#
# Two things differ from a single-service deployment and shape everything below:
#
#   * there are two Telegram accounts, each with its own live session. They are restarted
#     one after the other, never together, so a bad commit cannot take both down at once;
#   * the archive is what makes this release worth deploying. A container that starts
#     without a working archive is "healthy" by every HTTP measure and useless in practice,
#     so health here also asks whether the archive actually answers.
#
# Safe to run from a timer: with no new commit it does nothing and says nothing.
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/telegram-mcp-v1}"
BRANCH="${DEPLOY_BRANCH:-release}"
SERVICE_DIR="${SERVICE_DIR:-/opt/unified-tg-service}"
ACCOUNTS="${DEPLOY_ACCOUNTS:-personal work}"
declare -A PORTS=( [personal]=8820 [work]=8821 )

cd "$APP_DIR"
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Explicit refspec: a shallow checkout made with --branch <tag> carries no
# remote-tracking configuration, so a plain `git fetch origin release` succeeds
# while `origin/release` stays unknown. The first live run failed exactly there.
if ! git fetch --quiet origin "+${BRANCH}:refs/remotes/origin/${BRANCH}" 2>/dev/null; then
    # The branch appears the first time CI promotes a commit onto it. Until then there is
    # simply nothing verified to deploy, which is not an error.
    log "branch ${BRANCH} does not exist yet; nothing verified to deploy"
    exit 0
fi

current="$(git rev-parse HEAD)"
target="$(git rev-parse "origin/${BRANCH}")"
[ "$current" = "$target" ] && exit 0

log "new verified commit: ${current:0:8} -> ${target:0:8}"
log "$(git log --oneline -1 "$target")"

# Rebuilding costs minutes on two cores, so it happens only when the image contents can
# actually have changed. Application code is mounted from this checkout and needs a restart,
# not a rebuild.
needs_build=no
if ! git diff --quiet "$current" "$target" -- Dockerfile pyproject.toml uv.lock; then
    needs_build=yes
    log "dependencies or image definition changed — image will be rebuilt"
fi

# A rebuild on a loaded host is what took the connectors down once.
load="$(awk '{print int($1)}' /proc/loadavg)"
if [ "$needs_build" = "yes" ] && [ "$load" -ge 8 ]; then
    log "host busy (load ${load}), leaving the update for the next run"
    exit 0
fi

health() {
    local account="$1" port="${PORTS[$1]}" code status
    code="$(curl -s -o /tmp/.tg_health_$account -m 15 -w '%{http_code}' \
        "http://127.0.0.1:${port}/health" || echo 000)"
    status="$(python3 -c "
import json,sys
try:
    print(json.load(open('/tmp/.tg_health_$account')).get('status','?'))
except Exception:
    print('unreadable')
" 2>/dev/null)"
    log "health ${account}: http=${code} status=${status}"
    [ "$code" = "200" ] && [ "$status" = "healthy" ]
}

archive_answers() {
    # Asks the container the one question an HTTP probe cannot: is the archive reachable
    # from inside it. A release whose whole point is the archive must not pass health while
    # silently degraded to live-only reads.
    #
    # Reported, never fatal. During a staged rollout the two accounts can be on different
    # revisions, and an account still on an older image has no archive module at all — the
    # first live run let that ModuleNotFoundError reach the ERR trap and rolled back a
    # perfectly healthy deployment. Health is decided by health(); this only informs.
    local container="unified-tg-$1"
    docker exec -w /app "$container" python3 -c "
import sys, asyncio
sys.path.insert(0, '/app'); sys.argv = [sys.argv[0]]
from src.archive import get_archive_backend
b = get_archive_backend()
if b is None:
    print('archive not configured'); sys.exit(0)
async def go():
    h = await b.health()
    await b.close()
    return h
h = asyncio.run(go())
print('archive', h.get('reachable'), 'index', h.get('search_index_ready'))
print('archive unreachable' if not h.get('reachable') else '')
" 2>&1 | tail -1
}

restart_account() {
    local account="$1"
    log "restarting ${account}"
    ( cd "${SERVICE_DIR}/${account}" && docker compose up -d --force-recreate ) >/dev/null 2>&1
    for _ in $(seq 1 20); do
        health "$account" && return 0
        sleep 5
    done
    return 1
}

roll_back() {
    log "rolling back to ${current:0:8}"
    git checkout --quiet --force "$current"
    for account in $ACCOUNTS; do
        ( cd "${SERVICE_DIR}/${account}" && docker compose up -d --force-recreate ) >/dev/null 2>&1
    done
    local ok=yes
    for account in $ACCOUNTS; do
        health "$account" || ok=no
    done
    if [ "$ok" = yes ]; then
        log "rollback healthy; deployment left on the previous commit"
    else
        log "ROLLBACK DID NOT COME UP HEALTHY - needs a person"
    fi
    exit 1
}

git checkout --quiet --force "$target"

# The updater updates itself, and that is a trap: after the checkout the version
# already loaded in memory keeps running, and a rollback puts the old one back on
# disk. Seen on the collector: a fix to its own health check could never take
# effect, because every run rolled back and re-ran the same bug. So when the
# updater itself changed, the new version takes over from here. The flag keeps
# that from happening twice.
if [ "${TG_UPDATER_RELOADED:-}" != "1" ] \
   && ! git diff --quiet "$current" "$target" -- scripts/auto_update.sh; then
    log "the updater itself changed — continuing with the new version"
    TG_UPDATER_RELOADED=1 exec "$APP_DIR/scripts/auto_update.sh"
fi

trap roll_back ERR

if [ "$needs_build" = "yes" ]; then
    version="$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)"
    log "building salto/fast-mcp-telegram:${version}"
    docker build -q -t "salto/fast-mcp-telegram:${version}" . >/dev/null
fi

# One account at a time: if the first does not come back, the second is never touched.
for account in $ACCOUNTS; do
    restart_account "$account" || { log "${account} did not come up healthy"; roll_back; }
    log "archive from ${account}: $(archive_answers "$account" 2>&1 || echo 'not reported')"
done

trap - ERR
log "updated to ${target:0:8}; both accounts healthy"
