#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
cd "$REPO_ROOT"
"$REPO_ROOT/scripts/preflight.sh" config
configure_container_identity
secure_runtime_files

docker compose build telegram-mcp setup
docker compose --profile setup run --rm setup
secret_file_ok "$REPO_ROOT/secrets/backend_bearer"

load_nonsecret_config
export TELEGRAM_ACCESS_MODE="${TELEGRAM_ACCESS_MODE:-read-only}"
export TELEGRAM_ALLOWED_CHATS="${TELEGRAM_ALLOWED_CHATS:-me}"
python3 "$REPO_ROOT/scripts/render_acl.py"
secure_runtime_files

printf 'TELEGRAM LOGIN PASSED\n'
