# ChatGPT custom app values

- Remote MCP URL: `https://telegram-mcp.example.com/mcp`
- Authentication: OAuth
- Client credentials: protected files under `secrets/`

Replace the hostname. Add the exact ChatGPT callback URL to
`MCP_OAUTH_REDIRECT_URIS` before retrying if the UI supplies one.
