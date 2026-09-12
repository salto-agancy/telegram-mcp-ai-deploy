# Maintainer workflow

Production and community users consume the same public source. Production-specific state
stays in ignored `.env`, `.runtime.env`, `secrets/`, named Docker volumes and encrypted
backups on the operator's VPS.

## Fix and release loop

1. Start from a clean local checkout and fetch `origin`.
2. Create `fix/...` or `feat/...` from `main` and ask Codex to read `AGENTS.md`.
3. Reproduce the problem with synthetic data; never copy production messages or identifiers.
4. Implement the smallest compatible fix and update tests, docs and `CHANGELOG.md`.
5. Run `make check` and, for deployment changes, `make docker-validate`.
6. Inspect the staged diff and full branch history, then commit and push the branch.
7. Open a Pull Request. Merge only with green CI and secret scanning.
8. Choose the next semantic `v0.x.y`, update `pyproject.toml` and move changelog entries from
   `Unreleased` into the dated release section.
9. Merge the release change, tag the exact green `main` commit and push the tag. The release
   workflow validates the tag before creating the GitHub Release.

Example release commands:

```bash
git switch main
git pull --ff-only
make check
git tag -a v0.34.0 -m "v0.34.0"
git push origin v0.34.0
```

## Production dogfooding

The production checkout uses `origin` from this repository but keeps runtime state outside
Git. After a release:

```bash
cd /path/to/telegram-mcp-ai-deploy
UPDATE_CHANNEL=stable ./scripts/update.sh
TELEGRAM_SMOKE=true ./scripts/healthcheck.sh
```

`update.sh` records the previous commit, creates an encrypted backup, fetches tags, deploys
the newest stable release and rolls back automatically if health verification fails. Use
`UPDATE_CHANNEL=main` only for deliberate pre-release dogfooding.

Do not update production merely to validate a Pull Request. CI and a disposable environment
come first; production rollout is a separate maintainer decision.
