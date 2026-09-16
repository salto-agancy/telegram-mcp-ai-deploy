"""Archive-backed branch of global search.

Live Telegram search only sees message text. What was said in a voice message, or
written on a screenshot, is invisible to it — the words exist, but not in any field
Telegram indexes. The archive holds transcripts and recognised image text, so the
same question can be answered there.

Both sides are used and merged rather than one replacing the other: the archive
knows more about the chats it covers, live search still reaches chats it does not.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.archive import get_archive_backend
from src.tools.activity.window import to_archive_string
from src.utils.datetime_parse import parse_iso_datetime_utc

logger = logging.getLogger(__name__)

# Bound on archive candidates before merging, so one broad query cannot dominate.
ARCHIVE_OVERSHOOT = 3


def _normalise_date(raw: str | None) -> str | None:
    parsed = parse_iso_datetime_utc(raw)
    return to_archive_string(parsed) if parsed else None


def _hit_to_message(hit: Any) -> dict[str, Any]:
    payload = hit.to_dict()
    payload["source"] = "archive"
    return payload


def _message_key(message: dict[str, Any]) -> tuple[Any, Any]:
    chat = message.get("chat") or {}
    chat_id = message.get("chat_id")
    if chat_id is None and isinstance(chat, dict):
        chat_id = chat.get("id")
    return (chat_id, message.get("id"))


async def search_archive_messages(
    *,
    query: str,
    limit: int,
    min_date: str | None = None,
    max_date: str | None = None,
    chat_ids: list[int] | None = None,
    sender_ids: list[int] | None = None,
    media_kinds: list[str] | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[list[dict[str, Any]], str | None]:
    """Search the archive. Returns (messages, error) and never raises."""
    backend = get_archive_backend()
    if backend is None or not query or not query.strip():
        return [], None

    try:
        hits = await asyncio.wait_for(
            backend.search(
                query=query,
                chat_ids=chat_ids,
                sender_ids=sender_ids,
                since=_normalise_date(min_date),
                until=_normalise_date(max_date),
                media_kinds=media_kinds,
                limit=min(limit * ARCHIVE_OVERSHOOT, 300),
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning("archive search timed out — live results only")
        return [], "archive_timeout"
    except Exception:
        logger.exception("archive search failed — live results only")
        return [], "archive_unavailable"

    return [_hit_to_message(h) for h in hits], None


def merge_search_results(
    live: list[dict[str, Any]],
    archived: list[dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Combine both channels without duplicating a message.

    Where the same message exists on both sides, the archived copy wins: it is the
    one carrying the transcript and the recognised image text. Live-only results
    keep their place, because the archive does not cover every chat.
    """
    merged: dict[tuple[Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any]] = []

    for message in live:
        key = _message_key(message)
        if key not in merged:
            order.append(key)
        merged[key] = {**message, "source": "live"}

    archive_only = 0
    for message in archived:
        key = _message_key(message)
        if key in merged:
            merged[key] = {**merged[key], **message, "source": "both"}
        else:
            order.append(key)
            merged[key] = message
            archive_only += 1

    combined = [merged[k] for k in order][:limit]
    stats = {
        "live": len(live),
        "archive": len(archived),
        "archive_only": archive_only,
        "returned": len(combined),
    }
    return combined, stats
