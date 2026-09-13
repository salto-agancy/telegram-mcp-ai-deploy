#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
cd "$REPO_ROOT"

action="${1:-start}"
setup_compose=(docker compose -f docker-compose.yml -f infra/docker/compose.setup.yml)

session_ready() {
  docker compose exec -T telegram-mcp python -c \
    'from pathlib import Path; token=Path("/run/secrets/backend_bearer").read_text().strip(); raise SystemExit(0 if (Path("/data/sessions") / f"{token}.session").is_file() else 1)'
}

case "$action" in
  start)
    "$REPO_ROOT/scripts/init-secrets.sh"
    "$REPO_ROOT/scripts/preflight.sh" config
    configure_container_identity
    secure_runtime_files
    "${setup_compose[@]}" build telegram-mcp
    "${setup_compose[@]}" up -d --force-recreate --no-deps telegram-mcp
    deadline=$((SECONDS + 120))
    while (( SECONDS < deadline )); do
      container_id="$(docker compose ps -q telegram-mcp)"
      if [[ -n "$container_id" ]] \
        && [[ "$(docker inspect -f '{{.State.Health.Status}}' "$container_id")" == healthy ]]; then
        break
      fi
      sleep 2
    done
    container_id="$(docker compose ps -q telegram-mcp)"
    [[ -n "$container_id" ]] || fail "Telegram setup container did not start"
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$container_id")" == healthy ]] \
      || fail "Telegram setup container did not become healthy"
    printf 'TELEGRAM SETUP PORTAL READY\n'
    printf 'Local-only VPS endpoint: http://127.0.0.1:%s/setup?branch=new-session\n' "${TELEGRAM_SETUP_PORT:-8765}"
    printf 'The coding agent must open this through an SSH local-forward; never expose the port publicly.\n'
    ;;
  status)
    if session_ready; then
      printf 'TELEGRAM SESSION READY\n'
    else
      printf 'TELEGRAM SESSION PENDING\n'
      exit 1
    fi
    ;;
  finish)
    session_ready || fail "QR login has not produced the protected Telegram session"
    docker compose up -d --force-recreate --no-deps telegram-mcp
    printf 'TELEGRAM WEB LOGIN PASSED\n'
    ;;
  *) fail "usage: scripts/telegram-login-web.sh [start|status|finish]" ;;
esac
