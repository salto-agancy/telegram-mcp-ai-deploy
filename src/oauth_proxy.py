"""OAuth 2.1 facade for clients that cannot send the backend bearer directly.

This module uses FastMCP's OAuth provider behind a fixed-client facade.
Dynamic client registration is intentionally disabled: one client is pre-registered
through deployment secrets and allowed redirect URIs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from fastmcp.server.auth.providers import in_memory as _in_memory
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull


def _secret(name: str, *, required: bool = True) -> str:
    """Load a secret from NAME_FILE first, then NAME, without logging it."""
    file_value = os.environ.get(f"{name}_FILE", "").strip()
    if file_value:
        value = Path(file_value).read_text(encoding="utf-8").strip()
    else:
        value = os.environ.get(name, "").strip()
    if required and not value:
        raise RuntimeError(f"Required secret {name} is not configured")
    return value


_ACCESS_TTL = int(os.environ.get("ACCESS_TOKEN_TTL_SECONDS", str(24 * 60 * 60)))
_in_memory.DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS = _ACCESS_TTL

_DEFAULT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)


class FixedClientOAuthProvider(InMemoryOAuthProvider):
    """Single fixed OAuth client with SQLite-persisted grants and tokens."""

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        db_path: str,
        redirect_uris: list[str],
    ) -> None:
        super().__init__(
            base_url=base_url,
            resource_base_url=base_url,
            client_registration_options=ClientRegistrationOptions(enabled=False),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=None,
        )
        self._client_id = client_id
        self._fixed_client = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=redirect_uris,
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="mcp",
        )
        self.clients[client_id] = self._fixed_client
        self._lock = threading.Lock()
        db = Path(db_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self._db.commit()
        self._load()

    def _save(self) -> None:
        with self._lock:
            state = {
                "auth_codes": {
                    key: value.model_dump(mode="json")
                    for key, value in self.auth_codes.items()
                },
                "access_tokens": {
                    key: value.model_dump(mode="json")
                    for key, value in self.access_tokens.items()
                },
                "refresh_tokens": {
                    key: value.model_dump(mode="json")
                    for key, value in self.refresh_tokens.items()
                },
                "a2r": self._access_to_refresh_map,
                "r2a": self._refresh_to_access_map,
            }
            self._db.execute(
                "INSERT OR REPLACE INTO kv (k, v) VALUES ('state', ?)",
                (json.dumps(state),),
            )
            self._db.commit()

    def _load(self) -> None:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k = 'state'").fetchone()
        if not row:
            return
        state = json.loads(row[0])
        self.auth_codes = {
            key: AuthorizationCode.model_validate(value)
            for key, value in state.get("auth_codes", {}).items()
        }
        self.access_tokens = {
            key: AccessToken.model_validate(value)
            for key, value in state.get("access_tokens", {}).items()
        }
        self.refresh_tokens = {
            key: RefreshToken.model_validate(value)
            for key, value in state.get("refresh_tokens", {}).items()
        }
        self._access_to_refresh_map = state.get("a2r", {})
        self._refresh_to_access_map = state.get("r2a", {})

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._fixed_client if client_id == self._client_id else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise ValueError("Dynamic client registration is disabled")

    async def authorize(self, client, params):
        result = await super().authorize(client, params)
        self._save()
        return result

    async def exchange_authorization_code(self, client, authorization_code):
        result = await super().exchange_authorization_code(client, authorization_code)
        self._save()
        return result

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        result = await super().exchange_refresh_token(client, refresh_token, scopes)
        self._save()
        return result

    async def revoke_token(self, token) -> None:
        await super().revoke_token(token)
        self._save()


def build_proxy():
    client_id = _secret("MCP_OAUTH_CLIENT_ID")
    client_secret = _secret("MCP_OAUTH_CLIENT_SECRET")
    backend_bearer = _secret("BACKEND_BEARER")
    base_url = os.environ["PUBLIC_BASE_URL"].strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("PUBLIC_BASE_URL must use https://")
    backend_url = os.environ.get("BACKEND_URL", "http://telegram-mcp:8000/v1/mcp")
    redirect_uris = [
        uri.strip()
        for uri in os.environ.get("MCP_OAUTH_REDIRECT_URIS", "").split(",")
        if uri.strip()
    ] or list(_DEFAULT_REDIRECT_URIS)
    transport = StreamableHttpTransport(
        backend_url,
        headers={"Authorization": f"Bearer {backend_bearer}"},
    )
    provider = FixedClientOAuthProvider(
        base_url=base_url,
        client_id=client_id,
        client_secret=client_secret,
        db_path=os.environ.get("OAUTH_DB_PATH", "/data/oauth.db"),
        redirect_uris=redirect_uris,
    )
    return create_proxy(Client(transport), name="telegram-mcp-oauth", auth=provider)


def main() -> None:
    proxy = build_proxy()
    proxy.run(
        transport="http",
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("BIND_PORT", "8001")),
        path=os.environ.get("MCP_PATH", "/mcp"),
        show_banner=False,
    )


if __name__ == "__main__":
    main()
