#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
cd "$REPO_ROOT"
"$REPO_ROOT/scripts/preflight.sh" deploy

for service in telegram-mcp oauth-proxy cloudflared; do
  id="$(docker compose ps -q "$service")"
  [[ -n "$id" ]] || fail "$service container is absent"
  [[ "$(docker inspect -f '{{.State.Running}}' "$id")" == true ]] || fail "$service container is not running"
done

python3 "$REPO_ROOT/scripts/mcp_smoke.py"
