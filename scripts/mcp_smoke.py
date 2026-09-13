#!/usr/bin/env python3
"""Protocol-level health checks for the public Streamable HTTP endpoint."""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "telegram-mcp-healthcheck/1"
# A freshly registered tunnel route and a freshly created proxied DNS record become
# globally answerable a little after the connector reports readiness, so the very first
# public probe can still fail while the deployment itself is correct. Retry it a bounded
# number of times instead of failing the whole deployment on one transient error.
PUBLIC_PROBE_ATTEMPTS = 5
PUBLIC_PROBE_BACKOFF_SECONDS = 3.0


def open_with_retries(
    request: urllib.request.Request,
    timeout: int,
    attempts: int = PUBLIC_PROBE_ATTEMPTS,
    backoff: float = PUBLIC_PROBE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Open a public probe, retrying transient edge failures. Returns the HTTP status."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            sleep(backoff * attempt)
    raise RuntimeError(
        f"public endpoint did not answer after {attempts} attempts: {last_error}"
    )


def env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def decode_response(raw: bytes, content_type: str) -> dict:
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in content_type:
        data_lines = [
            line[6:] for line in text.splitlines() if line.startswith("data: ")
        ]
        if not data_lines:
            raise RuntimeError("MCP returned an empty event stream")
        return json.loads(data_lines[-1])
    return json.loads(text)


def rpc(
    url: str, token: str | None, method: str, params: dict, request_id: int
) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(
        request, timeout=45, context=ssl.create_default_context()
    ) as response:
        return decode_response(
            response.read(), response.headers.get("Content-Type", "")
        )


def main() -> None:
    config = env_file(ROOT / ".env")
    hostname = config["MCP_HOSTNAME"]
    socket.getaddrinfo(hostname, 443)
    token = (ROOT / "secrets/backend_bearer").read_text(encoding="utf-8").strip()
    base = f"https://{hostname}"

    metadata_request = urllib.request.Request(
        f"{base}/.well-known/oauth-authorization-server",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    metadata_status = open_with_retries(metadata_request, timeout=20)
    if metadata_status != 200:
        raise RuntimeError(f"OAuth metadata status is {metadata_status}")

    endpoint = f"{base}/v1/mcp"
    initialized = rpc(
        endpoint,
        token,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "installer-healthcheck", "version": "1"},
        },
        1,
    )
    if "result" not in initialized:
        raise RuntimeError(f"MCP initialize failed: {initialized}")
    tools = rpc(endpoint, token, "tools/list", {}, 2)
    tool_rows = tools.get("result", {}).get("tools", [])
    names = {row.get("name") for row in tool_rows}
    required = {"get_messages", "search_messages_globally", "find_chats"}
    if not required.issubset(names):
        raise RuntimeError(f"required tools missing: {sorted(required - names)}")
    print(f"MCP tools/list passed ({len(names)} tools)")

    try:
        rpc(
            endpoint,
            None,
            "tools/call",
            {"name": "get_messages", "arguments": {"chat_id": "me", "limit": 1}},
            3,
        )
    except urllib.error.HTTPError as exc:
        if exc.code not in {401, 403}:
            raise RuntimeError(
                f"unauthenticated request returned unexpected HTTP {exc.code}"
            ) from None
    else:
        raise RuntimeError("unauthenticated Telegram tool call was not rejected")
    print("Authentication negative test passed")

    if os.environ.get("TELEGRAM_SMOKE", "true").lower() == "true":
        result = rpc(
            endpoint,
            token,
            "tools/call",
            {"name": "get_messages", "arguments": {"chat_id": "me", "limit": 1}},
            4,
        )
        if result.get("error") or result.get("result", {}).get("isError"):
            raise RuntimeError("read-only Telegram smoke returned an MCP error")
        print("Read-only Telegram smoke passed")

    print("DEPLOYMENT PASSED")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, RuntimeError, urllib.error.URLError) as exc:
        print(
            f"FAILED AT: public MCP healthcheck\nCAUSE: {exc}\nNEXT AUTOMATIC ACTION: inspect docker compose logs",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
