#!/usr/bin/env bash
set -Eeuo pipefail

: "${CLOUD_DEPLOY_CONFIG:?set the path to the pilot update config}"
# shellcheck source=/dev/null
source "$CLOUD_DEPLOY_CONFIG"
: "${CLOUD_APP_DIR:?set the dedicated cloud checkout}"
: "${CLOUD_ENV_FILE:?set the dedicated cloud Compose env file}"
: "${CLOUD_STATE_DIR:?set the pilot update state directory}"
umask 077
mkdir -p "$CLOUD_STATE_DIR"
exec 9>"$CLOUD_STATE_DIR/update.lock"
flock -n 9 || exit 0
cd "$CLOUD_APP_DIR"
release=refs/remotes/origin/release-cloud
state="$CLOUD_STATE_DIR/deployed"
git fetch --quiet origin "refs/heads/release-cloud:$release"
target="$(git rev-parse "$release")"
previous=""
if [[ -f "$state" ]]; then
    read -r previous < "$state"
    git merge-base --is-ancestor "$previous" "$target"
fi
if [[ "$previous" == "$target" ]]; then exit 0; fi

compose=(docker compose -p telegram-mcp-cloud --env-file "$CLOUD_ENV_FILE" -f docker-compose.cloud.yml)
apply() {
    local service
    export CLOUD_IMAGE_TAG="$1"
    for service in cloud-first cloud-second cloud-oauth; do
        "${compose[@]}" up -d --no-deps --no-build --wait --wait-timeout 100 "$service" || return
    done
}
rollback() {
    trap - ERR INT TERM
    if [[ -n "$previous" ]] && git checkout --quiet --detach "$previous" && apply "$previous"; then
        echo "cloud_update=rollback_ok"
    elif [[ -z "$previous" ]]; then
        CLOUD_IMAGE_TAG="$target" "${compose[@]}" stop
        echo "cloud_update=initial_failed"
    else
        echo "cloud_update=rollback_failed"
    fi
    exit 1
}

git diff --quiet
git diff --cached --quiet
git checkout --quiet --detach "$target"
export CLOUD_IMAGE_TAG="$target"
trap rollback ERR INT TERM
"${compose[@]}" build cloud-oauth
apply "$target"
printf '%s\n' "$target" > "$state.tmp"
mv -f "$state.tmp" "$state"
trap - ERR INT TERM
echo "cloud_update=healthy"
