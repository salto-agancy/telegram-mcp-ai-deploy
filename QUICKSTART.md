# Quickstart

Recommended: give your coding agent the prompt in [INSTALL_WITH_AI.md](INSTALL_WITH_AI.md).

For an experienced operator on a fresh Ubuntu VPS:

```bash
git clone https://github.com/salto-agancy/telegram-mcp-ai-deploy
cd telegram-mcp-ai-deploy
./scripts/bootstrap.sh
./scripts/init-secrets.sh
```

Edit `.env` (hostname/account ID) and `.runtime.env` (Telegram credentials) using a
local editor. Then:

```bash
./scripts/telegram-login.sh
./scripts/cloudflare-provision.sh
./scripts/deploy.sh
TELEGRAM_SMOKE=true ./scripts/healthcheck.sh
```

`cloudflare-provision.sh` securely prompts for a temporary scoped token. After success,
revoke it. Add the requested client using a template under `templates/`; never put the
bearer in the repository.
