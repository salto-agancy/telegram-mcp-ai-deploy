# Architecture decision

Status: accepted, 2026-09-11.

## Current state

The starting system was `fast-mcp-telegram` 0.33: FastMCP 3, Telethon user MTProto,
Streamable HTTP, an origin OAuth proxy and Cloudflare Tunnel. The maintained fork adds
three extension tools to the upstream eight. The application worked, but reproducible
deployment and safe public distribution required a separate product layer.

## Problems

- Historical development artifacts contained personal metadata and could not become the
  public release history.
- ACL was opt-in and unlisted sessions inherited full access.
- `send_rich_message` missed one chat-scope gate; raw MTProto was exposed by default.
- Attachment tickets were reusable for an hour; telemetry defaulted on.
- Rebuilding a fresh VPS required hidden local files and operator knowledge.

## Available upstream solutions

Current public implementations were evaluated by maintenance, license, transport,
authentication, Telegram account model, and deployment maturity. None preserved this
installation's user-account behavior and all 11 tool APIs without a migration. FastMCP
and MCP now recommend Streamable HTTP for remote servers; SSE-only deployment is legacy.

Decision: **KEEP + HARDEN + selectively rebase upstream**. Replacing with a bot-only or
different-schema project would break existing clients and lose extension tools. Track upstream
changes separately and accept them only after compatibility tests.

## Target architecture

```text
Claude/ChatGPT --OAuth--> Cloudflare Tunnel --private--> OAuth facade
Codex/Claude Code --Bearer--> Cloudflare Tunnel --private--> FastMCP
                                                        |
                                                        +--> Telethon session volume
```

Only `cloudflared` has egress. No container publishes a host port. OAuth uses one fixed,
secret-bearing client with DCR disabled; bearer clients use a separate path. ACL is
fail-closed and raw MTProto is absent unless explicitly enabled.

## Migration plan

1. Publish a clean root history containing only sanitized source and generic examples.
2. Preserve tool names, arguments, and response shapes; hide raw MTProto by default.
3. Replace host bind mounts with a lockfile-built image and named volumes.
4. Provision Tunnel/DNS through scoped Cloudflare API calls.
5. Add reproducible scripts, encrypted backup, health checks, CI secret scans, templates,
   and AI deployment contract.
6. Test from a fresh clone with placeholder credentials, then perform a credentialed VPS
   smoke test before migrating a production hostname.
