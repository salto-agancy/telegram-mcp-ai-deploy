# Cloudflare Tunnel

The installer uses the current remotely managed Tunnel API. Create a temporary API token
scoped to the target account/zone with:

- Account — Cloudflare Tunnel — Edit
- Zone — DNS — Edit
- Zone — Zone — Read

Do not use a Global API Key. Run `scripts/cloudflare-provision.sh`; it discovers the best
matching zone, creates or reuses the named tunnel, reconciles ingress, creates/updates a
proxied CNAME, and writes only the connector token to `secrets/cloudflared_token`.
The generic desired-state shape is documented in [`cloudflare.example.yaml`](../cloudflare.example.yaml);
the executable source of truth remains `scripts/cloudflare_provision.py`.

Ingress is intentionally limited: `/v1/mcp` and short-lived attachments reach the bearer
backend; the hostname default reaches the OAuth facade; unmatched hostnames return 404.
The VPS needs outbound TCP/UDP 7844 and HTTPS but no public application port.

After provisioning, revoke the temporary API token. Rotating the tunnel token requires a
new API token and another provisioning run, followed by `docker compose up -d cloudflared`.

Official references:

- https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel-api/
- https://developers.cloudflare.com/fundamentals/api/reference/permissions/
