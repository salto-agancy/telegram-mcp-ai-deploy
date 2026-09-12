#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

phase="${1:-base}"
cd "$REPO_ROOT"
need git
need python3

[[ -f docker-compose.yml && -f Dockerfile && -f uv.lock ]] || fail "run from a complete repository clone"
[[ "$(uname -m)" =~ ^(x86_64|amd64|arm64|aarch64)$ ]] || fail "unsupported CPU architecture: $(uname -m)"

if [[ "$phase" != "base" ]]; then
  need docker
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is unavailable"
  docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable to this user"
  [[ -f .env ]] || fail "missing .env; copy .env.example and set MCP_HOSTNAME/account ID"
  [[ -f .runtime.env ]] || fail "missing .runtime.env; copy .runtime.env.example and add Telegram credentials"
  load_nonsecret_config
  [[ -n "${MCP_HOSTNAME:-}" && "$MCP_HOSTNAME" != *example.com ]] || fail "MCP_HOSTNAME is not configured"
  [[ -n "${CLOUDFLARE_ACCOUNT_ID:-}" && "$CLOUDFLARE_ACCOUNT_ID" != 00000000000000000000000000000000 ]] || fail "CLOUDFLARE_ACCOUNT_ID is not configured"
  docker compose config --quiet || fail "docker-compose.yml or environment is invalid"
fi

if [[ "$phase" == "deploy" ]]; then
  for item in backend_bearer oauth_client_id oauth_client_secret cloudflared_token acl.yaml; do
    secret_file_ok "$REPO_ROOT/secrets/$item"
  done
fi

printf 'PREFLIGHT PASSED (%s)\n' "$phase"
