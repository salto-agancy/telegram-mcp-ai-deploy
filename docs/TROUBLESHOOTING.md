# Troubleshooting

- `PREFLIGHT FAILED`: fix the named missing command/file/value; rerun the same stage.
- Backend unhealthy: `docker compose logs --tail=200 telegram-mcp`; verify API credentials,
  ACL syntax, and session volume.
- OAuth unhealthy: inspect `oauth-proxy`; verify secret file metadata and HTTPS base URL.
- Tunnel disconnected: inspect `cloudflared`, outbound 7844/443, and rotate connector token.
- DNS/TLS failure: rerun Cloudflare provisioning, then allow propagation before healthcheck.
- MCP 401/authorization error: reconnect OAuth or update the local bearer environment; never
  put a bearer in a URL.
- Telegram `FloodWait`: wait exactly the returned interval and narrow/paginate the query.

Every deployment failure should be reported as `FAILED AT`, `CAUSE`, and
`NEXT AUTOMATIC ACTION`; “looks healthy” is not an acceptance result.
