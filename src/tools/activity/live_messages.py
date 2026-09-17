"""Bounded live top-up for chats the archive cannot answer for.

Used only when a chat is missing from the archive or its copy is older than the
window being asked about. The read deliberately skips speech-to-text: running it
inside a retrieval request is what makes a voice-heavy chat cost thirty seconds,
and the background pipeline produces the same transcript for free. A voice
message read this way comes back marked ``pending``, never as silence.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telethon import errors as tg_errors

from src.archive.models import ArchiveMedia, ArchiveMessage

logger = logging.getLogger(__name__)

# Concurrency is bounded on purpose: Telegram answers a burst with FloodWait,
# and a benchmark bought with rate limiting is not a real improvement.
DEFAULT_CONCURRENCY = 4

# Telethon sleeps off a FloodWait shorter than its flood_sleep_threshold (60s by
# default) and retries without raising, so `except FloodWaitError` never fires and
# the wait shows up only as elapsed time. Measured on a real account: after ~60
# rapid reads Telegram answered every request with FloodWait 29s, the telemetry
# reported zero flood events, and the run simply looked slow. A read that takes
# longer than this is therefore recorded as a stall, which is a statement about
# what was observed rather than a guess at the cause.
STALL_SECONDS = 5.0

# A single slow chat must not decide how long the whole request takes. Measured
# on the work account with channels included: one channel read took 30.1s and
# the answer took 31.3s, while every other chat was already done. The per-chat
# timeout bounds that one read; the phase budget bounds the sum of them. What
# does not fit is reported as unfinished, never returned as an empty chat that
# looks like "nothing happened there".
PER_READ_TIMEOUT_SECONDS = 8.0
TOPUP_BUDGET_SECONDS = 20.0

_VOICE_ATTRS = ("voice", "round", "audio")


def _classify_media(message: Any) -> ArchiveMedia | None:
    media = getattr(message, "media", None)
    if media is None:
        return None

    doc = getattr(media, "document", None)
    photo = getattr(media, "photo", None)

    kind: str | None = None
    duration: int | None = None
    file_name: str | None = None
    mime: str | None = None
    size: int | None = None

    if photo is not None:
        kind = "photo"
    elif doc is not None:
        mime = getattr(doc, "mime_type", None)
        size = getattr(doc, "size", None)
        for attr in getattr(doc, "attributes", []) or []:
            name = getattr(attr, "file_name", None)
            if name:
                file_name = name
            if getattr(attr, "voice", False):
                kind = "voice"
                duration = getattr(attr, "duration", None)
            elif getattr(attr, "round_message", False):
                kind = "round"
                duration = getattr(attr, "duration", None)
        if kind is None:
            kind = "document"
    else:
        kind = type(media).__name__.replace("MessageMedia", "").lower() or "media"

    is_voice = kind in _VOICE_ATTRS
    is_image = kind == "photo"
    return ArchiveMedia(
        kind=kind,
        file_name=file_name,
        mime_type=mime,
        size_bytes=size,
        duration_seconds=duration,
        # The transcript exists or will exist in the background pipeline; this
        # path never starts one, so the honest status is "pending".
        transcription_status="pending" if is_voice else "absent",
        media_text_status="pending" if is_image else "absent",
    )


def _to_archive_message(message: Any, *, account: str, chat_id: int) -> ArchiveMessage:
    date = getattr(message, "date", None)
    text = (
        getattr(message, "text", None)
        or getattr(message, "message", None)
        or None
    )
    reply_to = getattr(message, "reply_to_msg_id", None) or getattr(
        getattr(message, "reply_to", None), "reply_to_msg_id", None
    )
    edit_date = getattr(message, "edit_date", None)
    return ArchiveMessage(
        id=int(message.id),
        date=date.isoformat() if date is not None else "",
        account=account,
        chat_id=chat_id,
        sender_id=(
            int(message.sender_id) if getattr(message, "sender_id", None) is not None else None
        ),
        is_outgoing=bool(getattr(message, "out", False)),
        is_service=type(message).__name__ == "MessageService"
        or getattr(message, "action", None) is not None,
        reply_to_msg_id=int(reply_to) if reply_to else None,
        edit_date=edit_date.isoformat() if edit_date is not None else None,
        text=text,
        media=_classify_media(message),
    )


async def fetch_chat_window(
    client: Any,
    *,
    chat_id: int,
    account: str,
    since_iso: str,
    until_iso: str | None,
    limit: int,
    run: Any = None,
) -> tuple[list[ArchiveMessage], bool, str | None]:
    """Read one chat's window. Returns (messages, has_more, error)."""
    collected: list[ArchiveMessage] = []
    has_more = False
    started = time.monotonic()
    try:
        async for message in client.iter_messages(chat_id, limit=limit + 1):
            date = getattr(message, "date", None)
            if date is None:
                continue
            iso = date.isoformat()
            if iso < since_iso:
                break
            if until_iso and iso > until_iso:
                continue
            if len(collected) >= limit:
                has_more = True
                break
            collected.append(
                _to_archive_message(message, account=account, chat_id=chat_id)
            )
        if run is not None:
            run.telegram_rpc_calls += 1
            run.note_read_duration(time.monotonic() - started)
    except tg_errors.FloodWaitError as exc:
        if run is not None:
            run.flood_wait_events += 1
            run.flood_wait_seconds += int(getattr(exc, "seconds", 0) or 0)
        logger.warning("flood wait while topping up a chat: %ss", getattr(exc, "seconds", "?"))
        return collected, has_more, "flood_wait"
    except (tg_errors.ChannelPrivateError, tg_errors.ChatAdminRequiredError):
        if run is not None:
            run.errors += 1
        return [], False, "access_denied"
    except Exception:
        if run is not None:
            run.errors += 1
        logger.exception("live top-up failed for a chat")
        return [], False, "unavailable"
    return collected, has_more, None


async def fetch_many(
    client: Any,
    *,
    chat_ids: list[int],
    account: str,
    since_iso: str,
    until_iso: str | None,
    limit: int,
    concurrency: int = DEFAULT_CONCURRENCY,
    run: Any = None,
    per_read_timeout: float = PER_READ_TIMEOUT_SECONDS,
    budget_seconds: float = TOPUP_BUDGET_SECONDS,
) -> dict[int, tuple[list[ArchiveMessage], bool, str | None]]:
    """Read several chats with bounded concurrency, time and patience."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(cid: int):
        async with semaphore:
            return cid, await asyncio.wait_for(
                fetch_chat_window(
                    client,
                    chat_id=cid,
                    account=account,
                    since_iso=since_iso,
                    until_iso=until_iso,
                    limit=limit,
                    run=run,
                ),
                timeout=per_read_timeout,
            )

    tasks = {asyncio.ensure_future(one(c)): c for c in chat_ids}
    done, pending = await asyncio.wait(tasks, timeout=budget_seconds)

    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        unfinished = [tasks[t] for t in pending]
        if run is not None:
            run.live_topup_unfinished.extend(unfinished)
        logger.warning(
            "live top-up budget of %.1fs spent, %d chat(s) left unread",
            budget_seconds,
            len(unfinished),
        )

    out: dict[int, tuple[list[ArchiveMessage], bool, str | None]] = {}
    for task in done:
        error = task.exception()
        if error is not None:
            cid = tasks[task]
            if isinstance(error, asyncio.TimeoutError):
                # One chat ran out of its own time. The others still answer.
                if run is not None:
                    run.live_topup_unfinished.append(cid)
                logger.warning("live read of chat %s exceeded %.1fs", cid, per_read_timeout)
            else:
                if run is not None:
                    run.errors += 1
                logger.exception("live top-up task failed", exc_info=error)
            continue
        cid, payload = task.result()
        out[cid] = payload
    return out
