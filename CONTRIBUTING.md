# Contributing

Issues, documentation fixes, client compatibility improvements and focused Telegram tool
changes are welcome.

## Fast path

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/telegram-mcp-ai-deploy
cd telegram-mcp-ai-deploy
git switch -c fix/short-description
make setup
make check
```

Fork the upstream repository first, push the branch to your fork, then open a Pull Request
against `salto-agancy/telegram-mcp-ai-deploy:main`.

You can let Codex or Claude Code perform the workflow. Give it the prompt in
[PROMPTS/CONTRIBUTE_WITH_AI.md](PROMPTS/CONTRIBUTE_WITH_AI.md), then review its diff and
sanitized PR description before allowing the push.

## Contribution rules

- Keep each PR focused on one problem.
- Reproduce bugs and add a regression test when practical.
- Preserve MCP tool compatibility unless the PR clearly documents a migration.
- New write-capable tools need destructive annotations and ACL coverage.
- Update relevant documentation and `CHANGELOG.md` under `Unreleased`.
- Run `make check`; Docker-related changes also require `make docker-validate`.
- Use English for code, comments, Issues and Pull Requests.

## Privacy gate

Never commit or attach:

- `.env`, `.runtime.env`, Telegram `.session` files or chat/message exports;
- API IDs/hashes, bot/OAuth/bearer/Cloudflare tokens or cookies;
- real phone numbers, Telegram users/chats, message text, domains, VPS IPs or SSH details;
- production ACLs, backups, logs or local absolute paths.

Use `example.com`, IANA documentation IP ranges, fake identifiers and synthetic messages.
Sanitize logs before posting. `make security` scans the worktree and all reachable commits,
but automated scanning does not replace review.

If you discover a vulnerability or credential, do not create a public Issue. Follow
[SECURITY.md](SECURITY.md).

## Review checklist

- The root cause and scope are clear.
- The change is minimal and backward-compatible where possible.
- Unit, ACL/security and installer tests pass.
- Lint, Docker validation where relevant, and secret/PII scanning pass.
- Documentation and changelog are current.
- The PR contains no private operational data.

By contributing, you agree that your contribution is licensed under the repository's MIT
license and follows the [Code of Conduct](CODE_OF_CONDUCT.md).
