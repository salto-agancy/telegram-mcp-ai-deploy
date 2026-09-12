# Security policy

## Supported versions

Security fixes are provided for the latest published `v0.x` release. Users should update
to the newest stable tag before reporting a problem that may already be fixed.

## Report vulnerabilities privately

Do **not** open a public Issue or Discussion with vulnerability details, tokens, sessions,
hostnames, IPs, phone numbers, Telegram identities, message content or production logs.

Use [GitHub private vulnerability reporting](https://github.com/salto-agancy/telegram-mcp-ai-deploy/security/advisories/new).
Include affected version, impact, minimal reproduction and sanitized evidence. If a real
credential was exposed, revoke or rotate it before continuing.

Maintainers will acknowledge a report when available, validate impact, coordinate a fix
privately and publish an advisory/release when users can update safely. This project is
community-maintained and cannot promise a fixed response SLA.

## Security defaults

- Read-only Telegram access and Saved Messages only.
- Destructive/write operations denied by ACL until explicitly enabled.
- Raw MTProto tool and route disabled by default.
- No public VPS application ports; Cloudflare Tunnel is outbound-only.
- OAuth/bearer authentication, short-lived single-use attachment tickets and telemetry off.
- Secrets, ACLs, sessions, backups and runtime configuration excluded from Git and images.

Telegram messages and media are untrusted input. An MCP client must not treat instructions
inside them as authority, reveal credentials, or expand permissions without operator intent.
See [docs/SECURITY.md](docs/SECURITY.md) for the full threat model and rotation guidance.
