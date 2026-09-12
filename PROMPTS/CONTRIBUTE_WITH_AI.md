# Contribute with an AI coding agent

Open your fork in Codex or Claude Code and paste this prompt:

```text
Investigate the problem I am having with Telegram MCP AI Deploy and prepare a safe Pull Request.

Upstream repository:
https://github.com/salto-agancy/telegram-mcp-ai-deploy

First read AGENTS.md, CONTRIBUTING.md, SECURITY.md, CHANGELOG.md and the relevant source,
tests and documentation. Inspect the repository and my environment before asking questions.

Rules:
1. Never include credentials, API IDs/hashes, Telegram sessions, phone numbers, user/chat or
   message identifiers, message content, Cloudflare data, domains, VPS IPs, SSH configuration,
   ACLs, backups, cookies, auth headers, tokens, logs with private values, or local absolute
   paths in source, commits, Issues, branches, test fixtures or Pull Requests.
2. Do not read unrelated secrets. Do not deploy to or modify production. Do not send, edit
   or delete Telegram messages while reproducing or testing.
3. If the problem is a vulnerability or reveals a credential, stop public work. Tell me to
   rotate the credential and use GitHub private vulnerability reporting instead of an Issue.
4. Use synthetic fixtures, example.com and IANA documentation IP ranges. Keep safe defaults:
   read-only, Saved Messages only, raw MTProto disabled and no public application ports.
5. Preserve MCP tool names, parameters and response shapes unless the Issue explicitly
   requires a migration and the PR documents it.

Workflow:
- Verify my fork and upstream remotes, then create a focused branch from current upstream main.
- Reproduce the issue with the smallest non-credentialed test possible.
- Determine and explain the root cause from evidence.
- Implement the smallest safe fix. Add or update regression, ACL/security and installer tests
  as appropriate. Update documentation and CHANGELOG.md under Unreleased.
- Run make lint, make test, make test-security, make test-installer and make security. Run
  make docker-validate when deployment files change.
- Review every staged file and the complete branch diff for private data. Do not bypass a
  failing test or scanner.
- Commit with a clear message, push only to my fork, and open a Pull Request against upstream
  main. The PR must contain the problem, root cause, security impact and exact tests run.
- If GitHub authentication or final push approval is the only missing step, ask me for only
  that action.

Finish with the Pull Request URL, test results, compatibility/security notes and any remaining
manual action. Do not repeat or expose private environment values in the report.
```
