#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
cd "$REPO_ROOT"
need openssl
umask 077
mkdir -p secrets backups
chmod 700 secrets backups

[[ -f .env ]] || install -m 600 .env.example .env
[[ -f .runtime.env ]] || install -m 600 .runtime.env.example .runtime.env
configure_container_identity

for name in backend_bearer oauth_client_id oauth_client_secret; do
  if [[ ! -s "secrets/$name" ]]; then
    openssl rand -hex 32 >"secrets/$name"
    chmod 600 "secrets/$name"
  fi
done

load_nonsecret_config
export TELEGRAM_ACCESS_MODE="${TELEGRAM_ACCESS_MODE:-read-only}"
export TELEGRAM_ALLOWED_CHATS="${TELEGRAM_ALLOWED_CHATS:-me}"
python3 "$REPO_ROOT/scripts/render_acl.py"
secure_runtime_files

printf 'SECRETS INITIALIZED\n'
printf 'Edit .env and .runtime.env without pasting their values into chat or Git.\n'
