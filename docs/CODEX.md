# Codex

Merge `templates/codex/config.toml.example` into project `.codex/config.toml` or user
`~/.codex/config.toml`, replace the hostname, and export `TELEGRAM_MCP_BEARER`. Codex CLI,
IDE, and desktop share the same configuration. Verify with `/mcp` or `codex mcp list`.

OAuth is supported by `codex mcp add ... --oauth-client-id ...` and `codex mcp login`, but
the callback URI shown by Codex must be included in `MCP_OAUTH_REDIRECT_URIS`. Bearer auth is
recommended for this reproducible fixed-client setup.

Official reference: https://learn.chatgpt.com/docs/extend/mcp
