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

## Updating from Git

The server follows the `release` branch and updates itself to it. CI moves that
branch, and only once every check has passed on the same commit — so the branch
is not "the latest push" but "the latest thing that passed". GitHub has no access
to the server; the server reads Git.

```
commit → CI → secret scan → release branch → server pulls it
       → restart personal → health → restart work → health → roll back on failure
```

Where the deployment lives is configuration, not code. The updater reads
`/etc/salto-mcp/telegram-deploy.env` (override with `DEPLOY_CONFIG`) or plain
environment variables; `deploy.env.example` documents every name and ships with
placeholder values. Paths, container prefix and ports used to sit in the script
as defaults, which made a public repository a map of one private server — none
of it secret, none of it anyone else's business either.

What `scripts/auto_update.sh` does, and why it is shaped this way:

- **One account at a time.** There are two Telegram accounts with two live
  sessions. If the first does not come back healthy, the second is never touched
  and the previous commit is restored — a bad commit cannot take both down.
- **Rebuild only when the image can have changed.** Application code is mounted
  from the checkout, so most updates are a restart. A rebuild costs minutes on
  two cores and is skipped when the host is loaded.
- **Health asks the archive, not just the port.** A container can answer 200 while
  silently degraded to live-only reads — that exact failure shipped once, when the
  image was built without the database driver. The probe therefore asks, from
  inside the container, whether the archive actually answers.

Disable it:

```bash
systemctl disable --now telegram-mcp-autoupdate.timer
```

Units are in `infra/systemd/telegram-mcp-autoupdate.*`, checking every 10 minutes
with an offset so it does not collide with the hourly Telegram collection.

### What makes a commit deployable

`release` moves only when every required workflow has succeeded **on that exact
commit**. A check still running means wait, not pass; a green result on a
different commit does not count toward this one; and any conclusion other than
success — including cancelled — refuses promotion. The rule is exercised in
`tests/unit/release/test_release_promotion.py`, because relying on GitHub's
scheduling order would make it a race rather than a guarantee.
