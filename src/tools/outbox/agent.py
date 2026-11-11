"""Which agent made the call: the OAuth proxy adds ``x-salto-agent`` per request.

Claude and ChatGPT reach this server through the same backend bearer, so the token
cannot tell them apart; the proxy can, because it knows the OAuth client.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

AGENT_HEADER = "x-salto-agent"
_SAFE = re.compile(r"[^a-z0-9_.-]")


def current_agent() -> str:
    try:
        from fastmcp.server.dependencies import get_http_headers

        raw = (get_http_headers() or {}).get(AGENT_HEADER, "")
    except Exception as e:  # no HTTP request in context (stdio, tests)
        logger.debug("agent header unavailable: %s", e)
        raw = ""
    label = _SAFE.sub("", raw.strip().lower())[:40]
    return label or "unknown"
