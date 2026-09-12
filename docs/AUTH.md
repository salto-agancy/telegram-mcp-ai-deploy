# Authentication

Two authenticated client paths share one backend principal:

- `/v1/mcp`: static bearer in the `Authorization` header. Best for Codex and Claude Code;
  keep the value in an environment variable or OS keychain.
- `/mcp`: OAuth 2.1 facade for hosted clients. Dynamic client registration is disabled.
  The client ID/secret are random protected files; authorization/access/refresh state is
  persisted in `oauth-data`.

Claude web callbacks are allowed by default. When another hosted client supplies an exact
callback URL, append it to `MCP_OAUTH_REDIRECT_URIS` in `.env` and redeploy the OAuth facade.
Do not use token-in-URL compatibility endpoints.

Rotate bearer: back up, rerun Telegram login, regenerate ACL, restart backend/proxy, and
update clients. Rotate OAuth client secret by replacing its protected file and recreating
the proxy; existing OAuth grants will need reconnection.
