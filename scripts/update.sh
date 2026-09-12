#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
cd "$REPO_ROOT"

channel="${UPDATE_CHANNEL:-stable}"
[[ "$channel" == stable || "$channel" == main ]] \
  || fail "UPDATE_CHANNEL must be stable or main"

"$REPO_ROOT/scripts/preflight.sh" deploy
"$REPO_ROOT/scripts/secret-scan.sh" --worktree --history
[[ -z "$(git status --porcelain)" ]] || fail "working tree must be clean before update"

previous="$(git rev-parse HEAD)"
printf '%s\n' "$previous" >.release-state
chmod 600 .release-state
"$REPO_ROOT/scripts/backup.sh"
git fetch --tags --prune origin

if [[ "$channel" == stable ]]; then
  target_name="$(git for-each-ref --sort=-version:refname --format='%(refname:short)' 'refs/tags/v[0-9]*' | head -n 1)"
  [[ "$target_name" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "no stable semantic version tag found"
  target="$(git rev-list -n 1 "$target_name")"
  git switch --detach "$target"
else
  git fetch origin main
  git switch main
  git merge --ff-only origin/main
  target_name="main"
  target="$(git rev-parse HEAD)"
fi

printf 'Updating %s -> %s (%s)\n' "$previous" "$target" "$target_name"
git diff "$previous" "$target" -- CHANGELOG.md || true

if TELEGRAM_SMOKE="${TELEGRAM_SMOKE:-true}" "$REPO_ROOT/scripts/deploy.sh"; then
  printf 'UPDATE PASSED: %s -> %s (%s)\n' "$previous" "$target" "$target_name"
else
  printf 'Update healthcheck failed; rolling back to %s\n' "$previous" >&2
  git switch --detach "$previous"
  TELEGRAM_SMOKE="${TELEGRAM_SMOKE:-true}" "$REPO_ROOT/scripts/deploy.sh"
  fail "new release failed healthcheck; previous release restored"
fi
