#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
"$REPO_ROOT/scripts/preflight.sh" base
"$REPO_ROOT/scripts/install-hooks.sh"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  info "Docker and Compose are already installed"
else
  [[ "$(uname -s)" == "Linux" ]] || fail "automatic Docker installation is supported on Ubuntu/Debian only"
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" ]] || fail "unsupported OS: ${PRETTY_NAME:-unknown}"
  need curl
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg python3 openssl
  sudo install -m 0755 -d /etc/apt/keyrings
  tmp_key="$(mktemp)"
  trap 'mv "$tmp_key" "${TMPDIR:-/tmp}/docker-key.discarded.$$.tmp" 2>/dev/null || true' EXIT
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o "$tmp_key"
  sudo install -m 0644 "$tmp_key" /etc/apt/keyrings/docker.asc
  arch="$(dpkg --print-architecture)"
  codename="${VERSION_CODENAME:?missing VERSION_CODENAME}"
  printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$ID" "$codename" "$arch" | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"
  info "Docker installed. Reconnect SSH once if docker info reports a permission error."
fi

if [[ "${MANAGE_UFW:-false}" == "true" ]]; then
  sudo apt-get install -y ufw
  sudo ufw default deny incoming
  sudo ufw default allow outgoing
  sudo ufw allow OpenSSH
  sudo ufw --force enable
  info "UFW enabled: inbound denied except OpenSSH"
else
  info "UFW unchanged. No application ports are published by Compose."
fi

printf 'BOOTSTRAP PASSED\n'
printf 'Next: run scripts/init-secrets.sh, then configure .env and .runtime.env.\n'
