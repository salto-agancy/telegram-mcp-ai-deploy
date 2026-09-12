# ChatGPT

ChatGPT does not read local Codex configuration. In a workspace/account that supports custom
MCP apps/connectors, enable developer mode, create an app for `https://HOST/mcp`, choose
OAuth, and provide the fixed OAuth client ID/secret. If ChatGPT displays a callback URL not
already configured, add that exact URL to `MCP_OAUTH_REDIRECT_URIS`, redeploy `oauth-proxy`,
then retry authorization.

Creating/enabling the app is a final manual UI action because it is bound to the receiving
person's ChatGPT account/workspace and consent. If custom MCP apps are unavailable on that
plan or workspace, use Codex or Claude Code; do not expose an unauthenticated endpoint.

Official reference: https://learn.chatgpt.com/docs/extend/mcp
