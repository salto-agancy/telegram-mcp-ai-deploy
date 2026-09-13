#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
cd "$REPO_ROOT"
need docker
need openssl

if [[ -z "${BACKUP_PASSPHRASE:-}" ]]; then
  BACKUP_PASSPHRASE=""
  read -r -s -p 'Backup encryption passphrase: ' BACKUP_PASSPHRASE || true
  printf '\n'
  [[ -n "$BACKUP_PASSPHRASE" ]] || fail "backup encryption passphrase is empty"
  export BACKUP_PASSPHRASE
fi
[[ ${#BACKUP_PASSPHRASE} -ge 16 ]] || fail "backup passphrase must be at least 16 characters"

mkdir -p backups
chmod 700 backups
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="backups/telegram-mcp-${timestamp}.tar.gz.enc"
project="${COMPOSE_PROJECT_NAME:-telegram-mcp}"

docker run --rm \
  -v "${project}_telegram-sessions:/archive/sessions:ro" \
  -v "${project}_oauth-data:/archive/oauth:ro" \
  alpine:3.22 tar -C /archive -czf - sessions oauth \
  | openssl enc -aes-256-cbc -salt -pbkdf2 -pass env:BACKUP_PASSPHRASE -out "$target"
chmod 600 "$target"
unset BACKUP_PASSPHRASE
printf 'BACKUP PASSED: %s\n' "$target"
