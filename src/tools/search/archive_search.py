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
from src.archive.account_scope import pick_archive_account
from src.config.server_config import cfg
from src.tools.activity.window import to_archive_string
from src.utils.datetime_parse import parse_iso_datetime_utc

logger = logging.getLogger(__name__)

# Bound on archive candidates before merging, so one broad query cannot dominate.
ARCHIVE_OVERSHOOT = 3


def _normalise_date(raw: str | None) -> str | None:
    parsed = parse_iso_datetime_utc(raw)
    return to_archive_string(parsed) if parsed else None


# Fields worth keeping when the caller asked for a list rather than the reading.
# Enough to decide and to fetch the full message afterwards; nothing that grows
# with the length of the conversation.
_BRIEF_KEYS = ("id", "date", "chat_id", "account", "is_outgoing", "matched_in", "source")
_BRIEF_MEDIA_KEYS = ("kind", "file_name", "mime_type", "size_bytes",
                     "duration_seconds", "transcription_status", "media_text_status")
# Long fields are cut rather than dropped: a hit whose only evidence is inside a
# transcript has to show enough of it to be recognisable.
_BRIEF_EXCERPT = 160


def _excerpt(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _BRIEF_EXCERPT:
        return value[:_BRIEF_EXCERPT] + "…"
    return value


def _hit_to_message(hit: Any, *, brief: bool = False) -> dict[str, Any]:
    payload = hit.to_dict()
    payload["source"] = "archive"
    if not brief:
        return payload

    out = {k: payload[k] for k in _BRIEF_KEYS if k in payload}
    for key in ("text", "transcription", "media_text"):
        if payload.get(key):
            out[key] = _excerpt(payload[key])
    media = payload.get("media")
    if isinstance(media, dict):
        slim = {k: media[k] for k in _BRIEF_MEDIA_KEYS if k in media}
        for key in ("transcription", "media_text"):
            if media.get(key):
                slim[key] = _excerpt(media[key])
        if slim:
            out["media"] = slim
    return out


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
    match_in: list[str] | None = None,
    session_label: str | None = None,
    brief: bool = False,
    timeout_seconds: float = 10.0,
) -> tuple[list[dict[str, Any]], str | None, int | None]:
    """Search the archive. Returns (messages, error, total) and never raises.

    ``total`` is how many messages match in all, not how many are returned. A page
    that does not say so gets read as a total: an agent asked how many voice
    messages mention payment, got its fifty, and answered "fifty" — there were 242.
    """
    backend = get_archive_backend()
    if backend is None or not query or not query.strip():
        return [], None, None

    try:
        accounts = await backend.accounts()
    except Exception:
        logger.exception("archive account list failed — live results only")
        return [], "archive_unavailable", None

    # Whose rows this session may read. A search that omits this does not fail —
    # it answers with someone else's messages: both connectors were returning the
    # same attachments, so work served personal correspondence and back again.
    account = pick_archive_account(
        accounts, [], session_label or "", configured=cfg().archive_account
    )
    if account is None:
        # Refusing is the safe end of the trade: a live-only answer is
        # incomplete, an answer from the wrong account is a leak.
        logger.warning(
            "archive holds %d accounts and none is selected for this session — "
            "skipping archive results", len(accounts),
        )
        return [], "archive_account_undetermined", None

    try:
        hits = await asyncio.wait_for(
            backend.search(
                accounts=[account],
                query=query,
                chat_ids=chat_ids,
                sender_ids=sender_ids,
                since=_normalise_date(min_date),
                until=_normalise_date(max_date),
                media_kinds=media_kinds,
                match_in=match_in,
                limit=min(limit * ARCHIVE_OVERSHOOT, 300),
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning("archive search timed out — live results only")
        return [], "archive_timeout", None
    except Exception:
        logger.exception("archive search failed — live results only")
        return [], "archive_unavailable", None

    total: int | None = None
    if len(hits) >= limit:
        # Only worth a second query when the page is full: a short page is its own
        # total, and counting is not free on a large archive.
        try:
            total = await asyncio.wait_for(
                backend.count_matches(
                    accounts=[account],
                    query=query,
                    chat_ids=chat_ids,
                    sender_ids=sender_ids,
                    since=_normalise_date(min_date),
                    until=_normalise_date(max_date),
                    media_kinds=media_kinds,
                    match_in=match_in,
                ),
                timeout=timeout_seconds,
            )
        except Exception:
            logger.warning("archive match count failed — page returned without a total")

    return [_hit_to_message(h, brief=brief) for h in hits], None, total


# Smallest share of the answer reserved for hits only the archive can produce.
# Without a reservation they are appended after every live result and cut off by
# the limit: measured on a real query, live filled all ten slots and the one
# message that actually contained the spoken phrase was dropped entirely.
ARCHIVE_ONLY_MIN_SHARE = 3


def merge_search_results(
    live: list[dict[str, Any]],
    archived: list[dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Combine both channels without duplicating a message, and without letting
    either crowd the other out.

    Where the same message exists on both sides, the archived copy wins: it is the
    one carrying the transcript and the recognised image text.

    Archive-only hits get a reserved share of the limit rather than a place at the
    end of the queue. They are not merely extra results — they are the only answer
    to questions live search cannot answer at all, because Telegram indexes message
    text and nothing else. A phrase spoken in a voice message, or written on a
    screenshot, exists in exactly one of these two channels; appending it after ten
    live matches means the caller never sees it.

    Live results keep priority within their share, since Telegram's own relevance
    is good for text, and the archive does not cover every chat.
    """
    merged: dict[tuple[Any, Any], dict[str, Any]] = {}
    live_order: list[tuple[Any, Any]] = []
    archive_order: list[tuple[Any, Any]] = []

    for message in live:
        key = _message_key(message)
        if key not in merged:
            live_order.append(key)
        merged[key] = {**message, "source": "live"}

    for message in archived:
        key = _message_key(message)
        if key in merged:
            merged[key] = {**merged[key], **message, "source": "both"}
        else:
            if key not in merged:
                archive_order.append(key)
            merged[key] = message

    archive_only = len(archive_order)
    reserved = min(archive_only, max(1, limit // ARCHIVE_ONLY_MIN_SHARE))
    live_slots = max(0, limit - reserved)

    # Interleave so neither channel is silently truncated: live keeps the head of
    # the answer, archive-only hits keep their reserved places.
    kept_live = live_order[:live_slots]
    kept_archive = archive_order[:reserved]
    combined_keys = kept_live + kept_archive

    # Any room left over (one channel had fewer hits than its share) goes to
    # whatever is still waiting, so a reservation never shrinks the answer.
    if len(combined_keys) < limit:
        spare = limit - len(combined_keys)
        leftovers = live_order[live_slots:] + archive_order[reserved:]
        combined_keys += leftovers[:spare]

    combined = [merged[k] for k in combined_keys]
    stats = {
        "live": len(live),
        "archive": len(archived),
        "archive_only": archive_only,
        "archive_only_returned": sum(
            1 for k in combined_keys if k in set(kept_archive) | set(archive_order)
            and merged[k].get("source") == "archive"
        ),
        "returned": len(combined),
    }
    return combined, stats
