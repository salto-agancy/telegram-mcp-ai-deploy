#!/usr/bin/env bash
set -Eeuo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
git -C "$repo_root" config core.hooksPath .githooks
chmod +x "$repo_root/.githooks/pre-push"
printf 'Git pre-push secret scan installed\n'
