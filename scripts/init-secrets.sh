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

for name in oauth_client_id oauth_client_secret; do
  if [[ ! -s "secrets/$name" ]]; then
    openssl rand -hex 32 >"secrets/$name"
    chmod 600 "secrets/$name"
  fi
done

printf 'SECRETS INITIALIZED\n'
printf 'Edit .env and .runtime.env without pasting their values into chat or Git.\n'
