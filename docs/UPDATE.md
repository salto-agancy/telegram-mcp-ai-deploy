# Update and rollback

The default update channel is the newest stable semantic tag:

```bash
./scripts/update.sh
```

The script requires a clean checkout, scans the worktree and all reachable history, stores
the previous commit, creates an encrypted backup, fetches tags, shows the changelog diff,
deploys the newest `vX.Y.Z` tag and runs protocol plus read-only Telegram health checks. If
deployment fails it restores and redeploys the previous commit.

Maintainers may deliberately dogfood unreleased `main`:

```bash
UPDATE_CHANNEL=main ./scripts/update.sh
```

Do not recommend `main` to normal users. Review `CHANGELOG.md` for migrations and security
notes before every production update. Run `scripts/rollback.sh <known-good-tag-or-commit>`
for an explicit rollback. A detached HEAD on a stable release or after rollback is expected;
runtime configuration and named volumes survive source rollback.

An AI agent asked to “Update my Telegram MCP to the latest stable version” should read this
file, inspect the current version and runtime state, run the script, verify
`DEPLOYMENT PASSED`, and report any rollback without exposing configuration.
