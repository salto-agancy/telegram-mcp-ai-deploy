# Claude and Claude Code

## Claude Code

Copy `templates/claude-code/.mcp.json.example` to the project as `.mcp.json`, replace only
the hostname, and export `TELEGRAM_MCP_BEARER` in a protected shell/keychain environment.
Merge with an existing file—never overwrite it. Remote HTTP is preferred; SSE is legacy.

OAuth is also supported by Claude Code with preconfigured client credentials, but bearer is
the simplest non-browser automation path for this fixed-client deployment.

## Claude web/desktop custom integration

Add `https://HOST/mcp`, open advanced authentication, and enter the values from
`secrets/oauth_client_id` and `secrets/oauth_client_secret` without copying them to docs.
Complete browser authorization. The standard Claude callback is already allowlisted.

Official references:

- https://code.claude.com/docs/en/mcp
- https://support.anthropic.com/en/articles/11503834-building-custom-integrations-via-remote-mcp-servers
