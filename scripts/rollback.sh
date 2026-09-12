#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
cd "$REPO_ROOT"
ref="${1:-}"
if [[ -z "$ref" && -f .release-state ]]; then ref="$(<.release-state)"; fi
[[ -n "$ref" ]] || fail "provide a Git ref or run after update.sh"
git rev-parse --verify "${ref}^{commit}" >/dev/null || fail "unknown Git ref: $ref"
"$REPO_ROOT/scripts/backup.sh"
git switch --detach "$ref"
"$REPO_ROOT/scripts/deploy.sh"
printf 'ROLLBACK PASSED: %s\n' "$ref"
