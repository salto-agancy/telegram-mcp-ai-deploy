# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

### Fixed

### Security

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

[Unreleased]: https://github.com/salto-agancy/telegram-mcp-ai-deploy/compare/v0.34.0...HEAD
[0.34.0]: https://github.com/salto-agancy/telegram-mcp-ai-deploy/releases/tag/v0.34.0
