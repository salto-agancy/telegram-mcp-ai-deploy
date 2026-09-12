#!/usr/bin/env bash
set -Eeuo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$repo_root/scripts/check_secrets.py" "$@"
python3 "$repo_root/scripts/check_public_safety.py" "$@"
