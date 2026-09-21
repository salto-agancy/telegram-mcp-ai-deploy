"""Voice notes: read their text, and put an agreed text under one of your own.

Two operations that must stay apart, because one is reading and the other changes what
other people see in the chat:

``transcribe_voice_message_impl`` — read only. Returns the text of one voice note or round
video, preferring what already exists: the transcription Telegram itself produced, kept in
the in-process cache from an earlier read. Nothing is re-recognised while a usable answer
is already known, so the common case costs one message fetch and no speech-to-text at all.

``set_voice_caption_impl`` — write. Puts a caption under an EXISTING voice message of the
operator's own, by editing that message. It never posts a second message, never re-uploads
audio and never touches the media itself. It also never writes the caption's text: what to
say is the operator's decision, made with them before this is called.

The split is deliberate. Transcribing is cheap, safe and repeatable; editing a message that
someone may already have read is neither, and the two must not happen in one step.
"""

from __future__ import annotations

import logging
from typing import Any

from src.client.connection import get_connected_client
from src.tools.messages.core import _normalize_parse_mode, detect_message_formatting
from src.tools.messages.editing import edit_message_impl
from src.tools.messages.reading import read_messages_by_ids
from src.utils.entity import get_entity_by_id
from src.utils.error_handling import log_and_build_error
from src.utils.logging_utils import log_operation_start, log_operation_success
from src.utils.message_format import (
    _TRANSCRIPTION_CACHE,
    PremiumRequiredError,
    _is_user_premium,
    _transcribe_single_voice_message,
    _transcription_cache_get,
    _transcription_cache_key,
)

logger = logging.getLogger(__name__)

SPOKEN_MEDIA_TYPES = ("voice", "round_video")


def _media(message: dict[str, Any]) -> dict[str, Any]:
    media = message.get("media")
    return media if isinstance(media, dict) else {}


async def _is_outgoing(message: dict[str, Any], client) -> bool:
    """Whether this account sent the message — the only kind Telegram lets us edit.

    Decided by comparing the sender id with this account's own id. The message dict
    carries no «mine» flag: it names who sent it and nothing more, so a check that looked
    for one would answer «not mine» about every message, including the operator's own.
    """
    sender = message.get("sender") or {}
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    if sender_id is None:
        return False
    try:
        me = await client.get_me()
    except Exception:
        return False
    return int(sender_id) == int(getattr(me, "id", 0) or 0)


def _duration(media: dict[str, Any]) -> int | None:
    """Voice length in seconds, under the name the media placeholder actually uses."""
    for key in ("duration_seconds", "duration"):
        value = media.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


async def _fetch_one(chat_id: str, message_id: int) -> dict[str, Any] | None:
    messages = await read_messages_by_ids(chat_id, [int(message_id)])
    if not messages:
        return None
    first = messages[0]
    return first if isinstance(first, dict) else None


async def transcribe_voice_message_impl(
    chat_id: str,
    message_id: int,
    force: bool = False,
) -> dict[str, Any]:
    """Text of one voice note or round video. Read-only; changes nothing in Telegram.

    Args:
        chat_id: chat holding the message (@username, numeric id, or 'me')
        message_id: id of the voice / round-video message itself
        force: ask Telegram again even when a transcription is already known. Telegram
            imposes a long per-message cooldown after a transcription, so this is for the
            rare case where the stored text is visibly wrong, not for routine use.
    """
    params = {"chat_id": chat_id, "message_id": message_id, "force": force}
    log_operation_start("Transcribing voice message", params)

    try:
        entity = await get_entity_by_id(chat_id)
        if not entity:
            return log_and_build_error(
                operation="transcribe_voice_message",
                error_message=f"Cannot find chat with ID '{chat_id}'",
                params=params,
                exception=ValueError(f"Cannot find any entity corresponding to '{chat_id}'"),
            )

        cache_key = _transcription_cache_key(entity, int(message_id))
        if force and cache_key is not None:
            _TRANSCRIPTION_CACHE.pop(cache_key, None)
        before = _transcription_cache_get(cache_key) if cache_key is not None else None
        was_cached = bool(before and before.state() == "done")

        message = await _fetch_one(chat_id, message_id)
        if message is None or message.get("error"):
            return {
                "status": "not_found",
                "chat_id": chat_id,
                "message_id": message_id,
                "error": (message or {}).get("error", "message not found or inaccessible"),
            }

        media = _media(message)
        media_type = media.get("type")
        if media_type not in SPOKEN_MEDIA_TYPES:
            return {
                "status": "not_voice",
                "chat_id": chat_id,
                "message_id": message_id,
                "media_type": media_type,
                "note": "this message carries no speech to transcribe; "
                        "voice notes and round videos are the ones that do",
            }

        text = message.get("transcription")
        source = "telegram" if text else None

        # A round video holds speech just as a voice note does, but the bulk path in
        # get_messages only transcribes voice. Asking for this one directly is the whole
        # reason this tool exists, so do it here rather than leave the caller with silence.
        if not text and media_type == "round_video":
            client = await get_connected_client()
            if await _is_user_premium(client):
                try:
                    text = await _transcribe_single_voice_message(
                        client, entity, int(message_id)
                    )
                    source = "telegram" if text else None
                except PremiumRequiredError:
                    text = None

        if not text:
            pending = _transcription_cache_get(cache_key) if cache_key is not None else None
            if pending and pending.state() == "pending":
                return {
                    "status": "pending",
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "media_type": media_type,
                    "duration_seconds": _duration(media),
                    "note": "Telegram is still recognising this one; ask again shortly",
                }
            if pending and pending.state() == "rate_limited":
                return {
                    "status": "rate_limited",
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "media_type": media_type,
                    "duration_seconds": _duration(media),
                    "note": "Telegram is refusing further transcriptions for now "
                            "(per-message cooldown); the earlier text, if any, still stands",
                }
            return {
                "status": "unavailable",
                "chat_id": chat_id,
                "message_id": message_id,
                "media_type": media_type,
                "duration_seconds": _duration(media),
                "note": "Telegram returned no text for this message. A Premium account is "
                        "required for transcription; speech-to-text outside Telegram is not "
                        "part of this server.",
            }

        log_operation_success("Voice message transcribed", chat_id)
        return {
            "status": "ready",
            "chat_id": chat_id,
            "message_id": message_id,
            "media_type": media_type,
            "duration_seconds": _duration(media),
            "transcription": text,
            "transcription_source": source or "telegram",
            "cached": was_cached and not force,
            "outgoing": await _is_outgoing(message, await get_connected_client()),
            "date": message.get("date"),
            "link": message.get("link"),
        }
    except Exception as e:
        return log_and_build_error(
            operation="transcribe_voice_message",
            error_message=f"Failed to transcribe voice message: {e!s}",
            params=params,
            exception=e,
        )


async def set_voice_caption_impl(
    chat_id: str,
    message_id: int,
    caption: str,
    parse_mode: str | None = "auto",
) -> dict[str, Any]:
    """Put an already-agreed text under an existing own voice message.

    Refuses anything else: a message someone else sent (Telegram would reject the edit),
    and a message that is not a voice note or round video (captioning those is ordinary
    editing, and going through a voice-specific tool would only hide what is happening).

    The caption is written by the operator, not here. This reads the message back after
    the edit and reports what actually stands in the chat.
    """
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption_length": len(caption or ""),
        "parse_mode": parse_mode,
    }
    log_operation_start("Setting caption on voice message", params)

    if not (caption or "").strip():
        return log_and_build_error(
            operation="set_voice_caption",
            error_message="caption is empty — nothing to put under the voice message",
            params=params,
            exception=ValueError("empty caption"),
        )

    message = await _fetch_one(chat_id, message_id)
    if message is None or message.get("error"):
        return {
            "status": "not_found",
            "chat_id": chat_id,
            "message_id": message_id,
            "error": (message or {}).get("error", "message not found or inaccessible"),
        }

    media_type = _media(message).get("type")
    if media_type not in SPOKEN_MEDIA_TYPES:
        return {
            "status": "not_voice",
            "chat_id": chat_id,
            "message_id": message_id,
            "media_type": media_type,
            "error": "this tool only captions voice notes and round videos; "
                     "use edit_message for other messages",
        }
    if not await _is_outgoing(message, await get_connected_client()):
        return {
            "status": "not_editable",
            "chat_id": chat_id,
            "message_id": message_id,
            "error": "this voice message was sent by someone else — Telegram allows editing "
                     "only your own messages, and nothing was attempted",
        }

    resolved = _normalize_parse_mode(parse_mode)
    if resolved == "auto":
        resolved = detect_message_formatting(caption)

    edited = await edit_message_impl(
        chat_id=chat_id,
        message_id=int(message_id),
        new_text=caption,
        parse_mode=resolved,
    )
    if isinstance(edited, dict) and edited.get("error"):
        return {
            "status": "failed",
            "chat_id": chat_id,
            "message_id": message_id,
            "error": edited.get("error"),
        }

    # Read it back: an edit that reports success and leaves the old text in the chat is the
    # failure this check exists for.
    after = await _fetch_one(chat_id, message_id)
    stored = (after or {}).get("text") or ""
    after_media = _media(after or {}).get("type")
    log_operation_success("Voice caption set", chat_id)
    return {
        "status": "ok" if stored.strip() == caption.strip() else "mismatch",
        "chat_id": chat_id,
        "message_id": message_id,
        "media_type": after_media,
        "media_unchanged": after_media == media_type,
        "parse_mode": resolved,
        "caption": stored,
        "caption_matches_requested": stored.strip() == caption.strip(),
        "duration_seconds": _duration(_media(after or {})),
        "link": (after or {}).get("link"),
        "note": None if stored.strip() == caption.strip()
        else "Telegram stored a different text than requested — compare before resending",
    }
