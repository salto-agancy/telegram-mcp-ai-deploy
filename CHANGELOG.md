# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `recent_activity`: one read-only batch snapshot of recent activity across chats,
  replacing the discover-then-open-each-chat pattern. Returns Telegram's own unread
  state (`unread_count`, `read_inbox_max_id`, `read_outbox_max_id`) alongside the
  messages in the window, direction, reply links, attachment metadata and ready
  voice transcripts. Whether a message needs a reply is left to the caller.
- Optional archive backend (`ARCHIVE_DSN`, extra `archive`): answers content
  questions from a locally maintained projection so ordinary retrieval neither
  re-reads Telegram nor re-runs speech-to-text. Disabled by default; unread state
  is always read live. See `docs/ARCHIVE.md`.
- `search_messages_globally` gained `source` (`auto` / `live` / `archive`). With an
  archive configured, `auto` also searches voice transcripts, recognised image text
  and attachment names, which Telegram's own index does not cover. Without one,
  behaviour is unchanged.

### Changed

### Fixed

- Make `make docker-validate` work in a fresh clone using only public example files.
- Allow external contributors to use their chosen Git commit email while still rejecting
  private maintainer email metadata and personal data in repository contents.
- Wait for the Cloudflare Tunnel connector to register an edge connection before running the
  public healthcheck, so a correct deployment is no longer reported as failed.
- Retry the first public probe a bounded number of times to absorb tunnel route and proxied
  DNS propagation, while still failing closed when the endpoint is genuinely unreachable.

### Security

## [0.34.1] - 2026-09-13

### Added

- Add a loopback-only, SSH-forwarded Telegram QR/2FA installer flow with automatic QR refresh.

### Changed

- Resolve non-root container UID/GID safely instead of assuming host UID 1000 is available.
- Give only the Telegram and tunnel services their required outbound connectivity.

### Fixed

- Restore required outbound Telegram connectivity while keeping normal host ports closed.
- Align protected runtime file ownership for the backend, setup flow and cloudflared.
- Make Linux secret-mode checks, piped credentials and Cloudflare-facing smoke requests portable.

### Security

- Keep the temporary setup portal bound to VPS loopback and require the coding agent to close
  its SSH forward after login.

## [0.34.0] - 2026-09-12

### Added

- AI-driven Ubuntu deployment with Docker Compose and Cloudflare Tunnel provisioning.
- OAuth facade, bearer endpoint, health checks, encrypted backup and rollback scripts.
- Claude, Claude Code, ChatGPT and Codex templates plus focused Telegram agent skills.
- Public contribution workflow, issue forms, release automation and maintainer runbook.

### Changed

- Deployment defaults to the latest stable semantic tag.
- Public examples and integration tests use synthetic identifiers and configurable fixtures.

### Fixed

- `send_rich_message` is covered by chat-scoped ACL enforcement.
- Attachment download tickets expire quickly and are consumed after one successful read.

### Security

- ACL is fail-closed, read-only and limited to Saved Messages by default.
- Raw MTProto and telemetry are disabled by default.
- Application containers publish no VPS ports; CI and pre-push hooks scan full Git history.
- Locked runtime dependencies include patched releases for all GitHub security alerts known at release time.

[Unreleased]: https://github.com/salto-agancy/telegram-mcp-ai-deploy/compare/v0.34.1...HEAD
[0.34.1]: https://github.com/salto-agancy/telegram-mcp-ai-deploy/compare/v0.34.0...v0.34.1
[0.34.0]: https://github.com/salto-agancy/telegram-mcp-ai-deploy/releases/tag/v0.34.0
