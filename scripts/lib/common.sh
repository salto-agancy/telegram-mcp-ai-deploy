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
  mode="$(stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1")"
  [[ "$mode" == "600" || "$mode" == "400" ]] || fail "secret file must be mode 600 or 400: $1"
}
