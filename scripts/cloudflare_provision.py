#!/usr/bin/env python3
"""Idempotently provision a remotely managed Cloudflare Tunnel and DNS record."""

from __future__ import annotations

import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
ROOT = Path(__file__).resolve().parents[1]


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required setting: {name}")
    return value


def request(method: str, path: str, token: str, payload: dict | None = None) -> object:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "telegram-mcp-installer/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            document = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Cloudflare API request failed with HTTP {exc.code}; no response body logged"
        ) from exc
    if not document.get("success"):
        error_codes = [item.get("code") for item in document.get("errors", [])]
        raise SystemExit(f"Cloudflare API rejected request; error codes: {error_codes}")
    return document.get("result")


def choose_zone(token: str, account_id: str, hostname: str) -> dict:
    query = urllib.parse.urlencode({"account.id": account_id, "status": "active", "per_page": 50})
    zones = request("GET", f"/zones?{query}", token)
    matches = [zone for zone in zones if hostname == zone["name"] or hostname.endswith(f".{zone['name']}")]
    if not matches:
        raise SystemExit("no active Cloudflare zone matches MCP_HOSTNAME")
    return max(matches, key=lambda zone: len(zone["name"]))


def ensure_tunnel(token: str, account_id: str, name: str) -> dict:
    query = urllib.parse.urlencode({"name": name, "is_deleted": "false"})
    tunnels = request("GET", f"/accounts/{account_id}/cfd_tunnel?{query}", token)
    if tunnels:
        tunnel = tunnels[0]
        if tunnel.get("config_src") not in {None, "cloudflare"}:
            raise SystemExit("existing tunnel is locally managed; choose another CLOUDFLARE_TUNNEL_NAME")
        print("Cloudflare tunnel already exists; reconciling configuration")
        return tunnel
    tunnel = request(
        "POST",
        f"/accounts/{account_id}/cfd_tunnel",
        token,
        {"name": name, "config_src": "cloudflare"},
    )
    print("Cloudflare tunnel created")
    return tunnel


def ensure_dns(token: str, zone_id: str, hostname: str, target: str) -> None:
    query = urllib.parse.urlencode({"type": "CNAME", "name": hostname})
    records = request("GET", f"/zones/{zone_id}/dns_records?{query}", token)
    payload = {"type": "CNAME", "name": hostname, "content": target, "proxied": True, "ttl": 1}
    if records:
        request("PUT", f"/zones/{zone_id}/dns_records/{records[0]['id']}", token, payload)
        print("Cloudflare DNS record reconciled")
    else:
        request("POST", f"/zones/{zone_id}/dns_records", token, payload)
        print("Cloudflare DNS record created")


def main() -> None:
    token = required("CLOUDFLARE_API_TOKEN")
    account_id = required("CLOUDFLARE_ACCOUNT_ID")
    hostname = required("MCP_HOSTNAME").lower().rstrip(".")
    tunnel_name = os.environ.get("CLOUDFLARE_TUNNEL_NAME", "telegram-mcp").strip()
    zone = choose_zone(token, account_id, hostname)
    tunnel = ensure_tunnel(token, account_id, tunnel_name)
    tunnel_id = tunnel["id"]

    ingress = [
        {
            "hostname": hostname,
            "path": r"^/v1/(mcp|attachments)(/.*)?$",
            "service": "http://telegram-mcp:8000",
        },
        {"hostname": hostname, "service": "http://oauth-proxy:8001"},
        {"service": "http_status:404"},
    ]
    request(
        "PUT",
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        token,
        {"config": {"ingress": ingress}},
    )
    print("Cloudflare ingress configuration reconciled")
    ensure_dns(token, zone["id"], hostname, f"{tunnel_id}.cfargotunnel.com")

    tunnel_token = request("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token", token)
    secrets = ROOT / "secrets"
    secrets.mkdir(mode=0o700, exist_ok=True)
    token_path = secrets / "cloudflared_token"
    token_path.write_text(f"{tunnel_token}\n", encoding="utf-8")
    token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    metadata = {"tunnel_id": tunnel_id, "zone_id": zone["id"], "hostname": hostname}
    (secrets / "tunnel.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (secrets / "tunnel.json").chmod(stat.S_IRUSR | stat.S_IWUSR)
    print("CLOUDFLARE PROVISIONING PASSED")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        raise SystemExit(130) from None
