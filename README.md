# Telegram MCP AI Deploy

Self-hosted Telegram MCP that an AI coding agent can deploy for you.

[![CI](https://github.com/salto-agancy/telegram-mcp-ai-deploy/actions/workflows/test.yml/badge.svg)](https://github.com/salto-agancy/telegram-mcp-ai-deploy/actions/workflows/test.yml)
[![Secret scan](https://github.com/salto-agancy/telegram-mcp-ai-deploy/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/salto-agancy/telegram-mcp-ai-deploy/actions/workflows/secret-scan.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## What it does

The server gives MCP clients controlled access to a Telegram user account through
FastMCP and Telethon. It can search and read messages, inspect chats, fetch media and,
only after explicit opt-in, send or edit messages. It supports remote deployment through
Docker Compose, OAuth/bearer authentication and an outbound-only Cloudflare Tunnel.

> [!WARNING]
> This MCP can expose Telegram content to an AI client. Defaults are deliberately strict:
> **read-only**, **Saved Messages only**, destructive tools blocked by ACL, and raw MTProto
> disabled. Expand permissions only after reviewing the threat model.

## Architecture

```text
Claude / ChatGPT -- OAuth -----┐
                               ├─ Cloudflare Tunnel ─ private Docker network ─ FastMCP ─ Telegram
Codex / Claude Code -- Bearer -┘
```

No application port is published on the VPS. Runtime secrets and Telegram sessions stay
in ignored files or Docker volumes and never belong in the repository.

## Deploy with AI

```bash
git clone https://github.com/salto-agancy/telegram-mcp-ai-deploy
cd telegram-mcp-ai-deploy
```

Open the directory in Codex, Claude Code or another capable coding agent and paste the
prompt from [INSTALL_WITH_AI.md](INSTALL_WITH_AI.md). The agent performs preflight,
prepares the VPS, provisions Cloudflare, deploys the stack and verifies the MCP protocol.

Manual operator path: [QUICKSTART.md](QUICKSTART.md).

## Supported clients

| Client | Connection | Guide |
| --- | --- | --- |
| Claude | Remote MCP with OAuth | [docs/CLAUDE.md](docs/CLAUDE.md) |
| Claude Code | Streamable HTTP, OAuth or bearer | [templates/claude-code](templates/claude-code) |
| ChatGPT | Remote MCP with OAuth where supported | [docs/CHATGPT.md](docs/CHATGPT.md) |
| Codex | Streamable HTTP with bearer environment variable | [docs/CODEX.md](docs/CODEX.md) |

## Security

- Secrets, sessions, ACLs, backups and runtime state are Git-ignored.
- Cloudflare uses a temporary scoped API token, never a Global API Key.
- ACL is fail-closed; raw MTProto requires two explicit opt-ins.
- Every push and pull request runs tests and full-history secret scanning.
- Never paste unredacted logs or credentials into an Issue or Pull Request.

Read [SECURITY.md](SECURITY.md) before enabling Telegram write access. Report
vulnerabilities privately through [GitHub Security Advisories](https://github.com/salto-agancy/telegram-mcp-ai-deploy/security/advisories/new).

## Update

Ask your coding agent: `Update my Telegram MCP to the latest stable version.`

The reproducible path is `make update`: it scans the checkout, creates an encrypted
backup, fetches the latest stable tag, deploys it, runs health checks and restores the
previous commit if deployment fails. See [docs/UPDATE.md](docs/UPDATE.md).

## Something doesn't work?

1. [Open an Issue](https://github.com/salto-agancy/telegram-mcp-ai-deploy/issues/new/choose).
2. Attach only sanitized logs—remove tokens, hostnames, IPs, phone numbers, chat names,
   message content and session paths.
3. Ask Codex or Claude Code to investigate using
   [PROMPTS/CONTRIBUTE_WITH_AI.md](PROMPTS/CONTRIBUTE_WITH_AI.md).
4. If it finds a fix, let it create a tested Pull Request.

## Contribute

Contributions are welcome through forks and Pull Requests. Run `make check` before opening
a PR and follow [CONTRIBUTING.md](CONTRIBUTING.md). Architecture and deployment details
live under [docs/](docs/).

Licensed under MIT. The application began as a fork of
[`leshchenko1979/fast-mcp-telegram`](https://github.com/leshchenko1979/fast-mcp-telegram);
see [NOTICE.md](NOTICE.md) for attribution.
