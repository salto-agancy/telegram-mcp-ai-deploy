"""Optional archive backend for content retrieval.

Disabled by default. Without ``ARCHIVE_DSN`` the gateway behaves exactly as
before: every read goes to live Telegram. With it configured, content questions
are answered from the local projection while dialog state still comes from
Telegram itself.
"""

from __future__ import annotations

import logging

from src.archive.backend import ArchiveBackend
from src.archive.models import (
    ArchiveChat,
    ArchiveMedia,
    ArchiveMessage,
    ArchiveSearchHit,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ArchiveBackend",
    "ArchiveChat",
    "ArchiveMedia",
    "ArchiveMessage",
    "ArchiveSearchHit",
    "get_archive_backend",
    "reset_archive_backend",
]

_backend: ArchiveBackend | None = None
_resolved = False


def get_archive_backend() -> ArchiveBackend | None:
    """Return the configured backend, or ``None`` when archive reads are off.

    Never raises: a misconfigured archive must degrade to live retrieval, not
    take the gateway down.
    """
    global _backend, _resolved
    if _resolved:
        return _backend

    _resolved = True
    from src.config.server_config import cfg

    config = cfg()
    dsn = (config.archive_dsn or "").strip()
    if not dsn:
        logger.debug("archive backend disabled (no ARCHIVE_DSN)")
        _backend = None
        return None

    try:
        from src.archive.postgres import PostgresArchiveBackend

        _backend = PostgresArchiveBackend(
            dsn,
            max_size=config.archive_pool_size,
            statement_timeout_ms=config.archive_statement_timeout_ms,
        )
        logger.info("archive backend enabled")
    except Exception:
        logger.exception("archive backend unavailable — falling back to live retrieval")
        _backend = None
    return _backend


def reset_archive_backend() -> None:
    """Drop the cached backend so the next call re-reads configuration (tests)."""
    global _backend, _resolved
    _backend = None
    _resolved = False
