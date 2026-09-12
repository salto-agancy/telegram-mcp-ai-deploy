# Architecture

`telegram-mcp` serves Streamable HTTP at `/v1/mcp`. Every production tool call resolves
the bearer to a Telethon session and a fail-closed ACL principal. The OAuth facade at
`/mcp` converts OAuth access tokens into the backend bearer without forwarding client
tokens. Its token state and Telegram sessions live in separate named volumes.

Cloudflare holds DNS/TLS at the edge. A remotely managed tunnel routes only the MCP and
attachment paths to the backend and all OAuth discovery/authorization paths to the facade.
The Compose network containing application services is internal; `cloudflared` also joins
an egress network. No reverse-proxy or firewall port for the application is needed.

Default registered tools are the ten high-level tools. The eleventh, `invoke_mtproto`, and
the raw HTTP bridge exist for backward compatibility but are registered only with
`ENABLE_RAW_MTPROTO=true`; the ACL still requires `allow_mtproto: true`.
