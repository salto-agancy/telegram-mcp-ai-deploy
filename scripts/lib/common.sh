#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

info() { printf 'INFO: %s\n' "$*"; }
fail() { printf 'FAILED AT: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }

load_nonsecret_config() {
  if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a
    # .env is operator-controlled and must contain only KEY=VALUE assignments.
    source "$REPO_ROOT/.env"
    set +a
  fi
}

secret_file_ok() {
  [[ -f "$1" && -s "$1" ]] || fail "required secret file is missing or empty: $1"
  local mode
  case "$(uname -s)" in
    Darwin) mode="$(stat -f '%Lp' "$1")" ;;
    Linux) mode="$(stat -c '%a' "$1")" ;;
    *) fail "unsupported OS for secret permission check: $(uname -s)" ;;
  esac
  [[ "$mode" == "600" || "$mode" == "400" ]] || fail "secret file must be mode 600 or 400: $1"
}

configure_container_identity() {
  python3 "$REPO_ROOT/scripts/configure_container_identity.py"
  load_nonsecret_config
}

secure_runtime_files() {
  load_nonsecret_config
  [[ "${APP_UID:-}" =~ ^[0-9]+$ && "${APP_GID:-}" =~ ^[0-9]+$ ]] \
    || fail "APP_UID and APP_GID must be resolved numeric values"
  chmod 700 "$REPO_ROOT/secrets" "$REPO_ROOT/backups"
  local item
  for item in "$REPO_ROOT"/secrets/*; do
    [[ -e "$item" ]] || continue
    chmod 600 "$item"
  done
  if [[ "$(id -u)" == "0" ]]; then
    chown -R "${APP_UID}:${APP_GID}" "$REPO_ROOT/secrets"
  elif [[ "${APP_UID}" != "$(id -u)" || "${APP_GID}" != "$(id -g)" ]]; then
    fail "runtime files require root ownership migration or APP_UID/APP_GID matching the deploy user"
  fi
}

# `docker compose up -d` returns as soon as the cloudflared container starts, but the
# connector still has to register its edge connections before the public hostname resolves
# to this tunnel. The image is distroless and exposes no healthcheck, so gate on the
# connector's own readiness line instead of probing the public endpoint too early.
wait_for_tunnel_registration() {
  local deadline=$((SECONDS + ${TUNNEL_READY_TIMEOUT:-120}))
  while (( SECONDS < deadline )); do
    if docker compose logs --no-color --tail=200 cloudflared 2>/dev/null \
      | grep -q 'Registered tunnel connection'; then
      info "Cloudflare Tunnel registered an edge connection"
      return 0
    fi
    sleep 3
  done
  fail "cloudflared did not register a tunnel connection before the public healthcheck"
}
