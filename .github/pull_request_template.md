## Problem and root cause

Describe the problem and the evidence for the root cause.

## Change

Describe the smallest safe fix and any compatibility impact.

## Verification

- [ ] `make lint`
- [ ] `make test`
- [ ] `make test-security`
- [ ] `make test-installer`
- [ ] `make security`
- [ ] `make docker-validate` if deployment files changed

## Privacy and security

- [ ] No credentials, sessions, real Telegram content/identifiers, phone numbers, domains,
      VPS IPs, ACLs, backups, logs or local absolute paths are included.
- [ ] Write-capable tools have destructive annotations and ACL tests.
- [ ] User-visible changes are documented under `CHANGELOG.md` → `Unreleased`.
