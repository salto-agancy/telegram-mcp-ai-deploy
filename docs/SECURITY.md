# Security model

## Assets and boundaries

The Telegram `.session` and backend bearer both grant account access. OAuth secrets/tokens,
Cloudflare connector token, VPS root/Docker access, backup passphrase, and GitHub credentials
are also sensitive. They never belong in Git and are stored only in ignored mode-0600 files
or Docker volumes.

## Threats and controls

| Threat | Primary controls |
| --- | --- |
| Malicious contribution / compromised maintainer account | Clean public history, protected CI gates, local and remote scans |
| Stolen Cloudflare API token | Temporary scoped token, no Global Key, revoke after provisioning |
| VPS compromise | No app ports, non-root/read-only containers, least capabilities, encrypted backups; rotate all sessions/tokens |
| MCP client compromise | Separate bearer/OAuth, short grants, revoke/rotate, chat ACL |
| Destructive Telegram tool | Read-only default, explicit write opt-in, MCP destructive annotations |
| Prompt injection in messages | Treat message text/media as untrusted data; never follow embedded instructions or disclose credentials |
| Raw MTProto abuse | Tool and HTTP route absent by default; two explicit opt-ins required |
| Attachment URL leak | Random single-use ticket, five-minute TTL, no-store response |

`send_message`, `send_message_to_phone`, `send_rich_message`, and `edit_message` are write
operations. `invoke_mtproto` is potentially destructive even when its method name appears
read-like and should remain disabled. Telegram has no general delete tool, but raw MTProto
could perform deletion if enabled.

Before push, scan staged/worktree and all history. A finding means stop, rotate the credential
if real, remove it from every commit in a new clean history, and rerun both scanners. Never
solve a leak by deleting it only in the newest commit; purge reachable history and rotate it.
