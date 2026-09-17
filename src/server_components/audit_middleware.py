"""One row per MCP tool call into the shared journal ``salto_crm.mcp_tool_audit``.

The gateway could say what Telegram did but not what was asked of it: the shared
journal held rows from salto-core and pg-mcp only, so "which tool does this
account actually use, and what fails" had no answer for Telegram at all.

The journal is a different database from ``ARCHIVE_DSN`` — the archive is this
product's own data, the journal is shared across every MCP service — so this
module owns a second, tiny pool and touches nothing else.

What never reaches a row: bearer tokens, DSNs, chat ids, message text, argument
values. Only a sha256 of the arguments, a fingerprint of the session token, the
latency and whether the call raised. Writes are detached and swallow their own
errors: a Telegram call must never fail because the journal is down.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Sequence
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

logger = logging.getLogger(__name__)

__all__ = ["AuditMiddleware"]

_INSERT = """INSERT INTO mcp_tool_audit
  (service, oauth_subject, scopes, tool, request_id, latency_ms, ok, row_count,
   input_sha256, note)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"""

_JSON_SCALARS = (str, int, float, bool, type(None))


def _scrub(value: Any) -> Any:
    """Reduce a value to plain JSON so the hash is stable across restarts and no
    object repr (clients, memory addresses) can reach the row."""
    if isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, dict):
        return {str(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_scrub(v) for v in value]
    return f"<{type(value).__name__}>"


def input_hash(args: dict[str, Any] | None) -> str:
    """Fingerprint of the arguments. Identical calls collapse to one hash, which
    is what makes repeated failures visible; the values themselves stay out."""
    try:
        return hashlib.sha256(
            json.dumps(_scrub(args or {}), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
    # A missing hash beats a failed call.
    except Exception:
        return ""


def _session_fingerprint() -> str | None:
    """Stable, non-reversible id of the calling session.

    The bearer token identifies the client but *is* the credential, so only its
    digest is stored — enough to tell two connectors apart, useless if leaked.
    """
    token: str | None = None
    try:
        from src.client.connection import get_request_token

        token = get_request_token()
    except Exception:
        token = None
    if not token:
        try:
            from fastmcp.server.dependencies import get_access_token

            access = get_access_token()
            token = getattr(access, "token", None) if access is not None else None
        except Exception:
            token = None
    if not token:
        return None
    return "tok:" + hashlib.sha256(token.encode()).hexdigest()[:12]


class AuditMiddleware(Middleware):
    """Record every ``tools/call`` in the shared MCP journal."""

    def __init__(self, dsn: str, service: str) -> None:
        self._dsn = dsn
        self._service = service
        self._pool: Any = None
        self._pool_lock = asyncio.Lock()
        self._pending: set[asyncio.Task] = set()

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        started = time.monotonic()
        ok, note = True, ""
        try:
            return await call_next(context)
        except Exception as exc:
            # Only the class name: driver and Telegram error messages quote chat
            # ids, phone numbers and request bodies.
            ok, note = False, type(exc).__name__
            raise
        finally:
            self._fire(
                tool=context.message.name,
                arguments=context.message.arguments,
                ok=ok,
                latency_ms=int((time.monotonic() - started) * 1000),
                note=note,
                request_id=self._request_id(context),
            )

    @staticmethod
    def _request_id(context: MiddlewareContext[Any]) -> str | None:
        try:
            return str(context.fastmcp_context.request_id)
        # No fastmcp context outside an HTTP request.
        except Exception:
            return None

    def _fire(self, *, tool: str, arguments: dict[str, Any] | None, ok: bool,
              latency_ms: int, note: str, request_id: str | None) -> None:
        """Queue one row. Never awaits, never raises — safe in a finally block."""
        row = (
            self._service,
            _session_fingerprint(),
            None,  # scopes: this gateway authorises per session token, not per scope
            tool,
            request_id,
            latency_ms,
            ok,
            None,  # row_count: results are free-form, a generic count would lie
            input_hash(arguments),
            (note or None),
        )
        try:
            task = asyncio.get_running_loop().create_task(self._write(row))
        except RuntimeError:
            return  # no running loop — nothing to audit into
        # Hold a reference, otherwise the task may be collected mid-flight.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _pool_or_connect(self) -> Any:
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:
                    import asyncpg

                    self._pool = await asyncpg.create_pool(
                        self._dsn, min_size=0, max_size=2, command_timeout=5
                    )
        return self._pool

    async def _write(self, row: Sequence[Any]) -> None:
        try:
            pool = await self._pool_or_connect()
            async with pool.acquire() as conn:
                await conn.execute(_INSERT, *row, timeout=5)
        except Exception as exc:
            # Container filesystems are ephemeral, so the fallback is the log the
            # operator already collects and rotates, not a file nobody replays.
            logger.warning(
                "audit: journal write failed (%s) for tool=%s ok=%s latency_ms=%s",
                type(exc).__name__, row[3], row[6], row[5],
            )
