# Future architecture

Telegram remains the product boundary for the current `v0.x` series. No framework refactor
is planned until another server proves that shared extraction reduces real duplication.

Already-generic components are:

- Ubuntu/Docker bootstrap and Compose lifecycle;
- Cloudflare Tunnel and DNS provisioning;
- OAuth facade and protected secret-file handling;
- backup, update, rollback and health-report conventions;
- AI installer contract and MCP client templates.

A possible later extraction is:

```text
mcp-deploy/
├── servers/
│   ├── telegram/
│   ├── gmail/
│   ├── trello/
│   └── ...
├── infra/
├── skills/
└── templates/
```

Extraction should happen only after at least two production servers share stable interfaces.
Until then, keep Telegram code direct and readable, isolate generic shell/Python helpers, and
avoid abstractions that make security review or rollback harder.
