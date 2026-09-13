# Deployment runbook

Supported automated target: current Ubuntu LTS/Debian with SSH, systemd, outbound HTTPS,
and enough storage for Telegram sessions/backups. Compose uses `restart: unless-stopped`,
so Docker restores the stack after reboot.

Order:

1. `scripts/preflight.sh base`
2. `scripts/bootstrap.sh` (`MANAGE_UFW=true` only after confirming SSH uses the allowed rule)
3. `scripts/init-secrets.sh`
4. Fill mode-0600 `.env` and `.runtime.env`.
5. `scripts/telegram-login-web.sh start`, SSH local-forward opened by the coding agent,
   browser QR/2FA, then `scripts/telegram-login-web.sh finish`
6. `scripts/cloudflare-provision.sh`
7. `scripts/deploy.sh`
8. `TELEGRAM_SMOKE=true scripts/healthcheck.sh`

The preferred user-account login does not require the operator to open a terminal. The coding
agent starts a temporary loopback-only setup portal on the VPS, opens an SSH local-forward and
launches the local browser. The operator only scans the regenerating Telegram QR and enters 2FA
in the browser form. The agent then closes the forward and recreates the service without the
temporary loopback port. `scripts/telegram-login.sh` remains a bot/headless fallback.

The backend, setup container and cloudflared run under a resolved non-root UID/GID. When the
installer runs as root it chooses IDs not assigned to a host account and aligns only runtime
secret ownership; when run as an ordinary deploy user it uses that user's IDs. Telegram and
setup receive outbound networking because MTProto requires it, but no host application port is
published during normal operation.

Logs are bounded by Docker rotation. Inspect safely with `docker compose logs --tail=200
SERVICE`; do not enable debug logs while handling credentials.
