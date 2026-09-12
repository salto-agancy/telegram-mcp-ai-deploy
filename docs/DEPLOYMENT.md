# Deployment runbook

Supported automated target: current Ubuntu LTS/Debian with SSH, systemd, outbound HTTPS,
and enough storage for Telegram sessions/backups. Compose uses `restart: unless-stopped`,
so Docker restores the stack after reboot.

Order:

1. `scripts/preflight.sh base`
2. `scripts/bootstrap.sh` (`MANAGE_UFW=true` only after confirming SSH uses the allowed rule)
3. `scripts/init-secrets.sh`
4. Fill mode-0600 `.env` and `.runtime.env`.
5. `scripts/telegram-login.sh`
6. `scripts/cloudflare-provision.sh`
7. `scripts/deploy.sh`
8. `TELEGRAM_SMOKE=true scripts/healthcheck.sh`

The login is intentionally interactive: the operator enters Telegram OTP/2FA directly.
The generated bearer is written to a protected file and not printed. For multiple Telegram
accounts, deploy separate Compose projects/hostnames so each has independent secrets/ACL.

Logs are bounded by Docker rotation. Inspect safely with `docker compose logs --tail=200
SERVICE`; do not enable debug logs while handling credentials.
