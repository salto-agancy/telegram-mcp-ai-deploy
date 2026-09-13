#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
cd "$REPO_ROOT"
"$REPO_ROOT/scripts/preflight.sh" config
configure_container_identity
secure_runtime_files
"$REPO_ROOT/scripts/preflight.sh" deploy
"$REPO_ROOT/scripts/secret-scan.sh" --worktree

docker compose build --pull telegram-mcp oauth-proxy
docker compose pull cloudflared
docker compose up -d --remove-orphans telegram-mcp oauth-proxy cloudflared

deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  backend_id="$(docker compose ps -q telegram-mcp)"
  proxy_id="$(docker compose ps -q oauth-proxy)"
  if [[ -n "$backend_id" && -n "$proxy_id" ]] \
    && [[ "$(docker inspect -f '{{.State.Health.Status}}' "$backend_id")" == healthy ]] \
    && [[ "$(docker inspect -f '{{.State.Health.Status}}' "$proxy_id")" == healthy ]]; then
    break
  fi
  sleep 3
done

"$REPO_ROOT/scripts/healthcheck.sh"
