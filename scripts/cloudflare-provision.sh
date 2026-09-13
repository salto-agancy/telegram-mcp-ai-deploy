#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
cd "$REPO_ROOT"
"$REPO_ROOT/scripts/preflight.sh" config
load_nonsecret_config

if [[ -z "${CLOUDFLARE_API_TOKEN:-}" ]]; then
  CLOUDFLARE_API_TOKEN=""
  read -r -s -p 'Temporary scoped Cloudflare API token: ' CLOUDFLARE_API_TOKEN || true
  printf '\n'
  [[ -n "$CLOUDFLARE_API_TOKEN" ]] || fail "Cloudflare API token is empty"
  export CLOUDFLARE_API_TOKEN
fi

python3 "$REPO_ROOT/scripts/cloudflare_provision.py"
secure_runtime_files
unset CLOUDFLARE_API_TOKEN
printf 'The temporary Cloudflare API token may now be revoked.\n'
