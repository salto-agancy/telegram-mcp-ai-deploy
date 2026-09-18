"""get_messages entry point and mode dispatch."""

from typing import Any

from src.tools.messages import read_messages_by_ids
from src.utils.error_handling import log_and_build_error
from src.utils.message_format import response_attachment_warning

from .replies import _handle_reply_mode
from .search_mode import _DEFAULT_MAX_CONCURRENT, _handle_query_mode
from .types import MessageRetrievalMode, ThreadScope, resolve_mode


def _build_search_params(
    *,
    query: str | None,
    chat_id: str | None,
    message_ids: list[int] | None,
    reply_to_id: int | None,
    thread_scope: ThreadScope,
    limit: int,
    min_date: str | None,
    max_date: str | None,
    chat_type: str | None,
    public: bool | None,
    auto_expand_batches: int,
    include_total_count: bool,
    max_concurrent: int | None = None,
) -> dict[str, Any]:
    return {
        "query": query,
        "chat_id": chat_id,
        "message_ids": message_ids,
        "reply_to_id": reply_to_id,
        "thread_scope": thread_scope,
        "limit": limit,
        "min_date": min_date,
        "max_date": max_date,
        "chat_type": chat_type,
        "public": public,
        "auto_expand_batches": auto_expand_batches,
        "include_total_count": include_total_count,
        "max_concurrent": max_concurrent,
        "is_global_search": chat_id is None,
        "has_query": bool(query and query.strip()),
        "has_date_filter": bool(min_date or max_date),
        "message_count": len(message_ids) if message_ids else 0,
    }


def _unsupported_date_filter_error(
    params: dict[str, Any], mode_label: str
) -> dict[str, Any] | None:
    if not params.get("has_date_filter"):
        return None
    return log_and_build_error(
        operation="get_messages",
        error_message=f"min_date and max_date are not supported for {mode_label} mode",
        params=params,
        exception=ValueError(f"Date filters not supported for {mode_label} mode"),
    )


async def _dispatch_search_mode(
    mode: MessageRetrievalMode,
    params: dict[str, Any],
    *,
    query: str | None,
    chat_id: str | None,
    message_ids: list[int] | None,
    reply_to_id: int | None,
    limit: int,
    min_date: str | None,
    max_date: str | None,
    chat_type: str | None,
    public: bool | None,
    auto_expand_batches: int,
    include_total_count: bool,
    thread_scope: ThreadScope,
) -> dict[str, Any]:
    if mode is MessageRetrievalMode.MESSAGE_IDS:
        if chat_id is None or message_ids is None:
            return log_and_build_error(
                operation="get_messages",
                error_message="chat_id and message_ids required for message_ids mode",
                params=params,
                exception=ValueError("Missing required params"),
            )
        if err := _unsupported_date_filter_error(params, "message_ids"):
            return err
        return await _handle_ids_mode(chat_id, message_ids, params)

    if mode is MessageRetrievalMode.REPLIES:
        if chat_id is None or reply_to_id is None:
            return log_and_build_error(
                operation="get_messages",
                error_message="chat_id and reply_to_id required for replies mode",
                params=params,
                exception=ValueError("Missing required params"),
            )
        return await _handle_reply_mode(
            chat_id, reply_to_id, limit, query, params, thread_scope,
            min_date=min_date, max_date=max_date,
        )

    return await _handle_query_mode(
            query=query,
            chat_id=chat_id,
            limit=limit,
            min_date=min_date,
            max_date=max_date,
            chat_type=chat_type,
            public=public,
            auto_expand_batches=auto_expand_batches,
        include_total_count=include_total_count,
        params=params,
    )


async def _handle_ids_mode(
    chat_id: str,
    message_ids: list[int],
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handle reading specific messages by IDs with unified output format."""
    messages_list = await read_messages_by_ids(chat_id, message_ids)

    if len(messages_list) == 1 and "error" in messages_list[0]:
        return messages_list[0]

    result: dict[str, Any] = {
        "messages": messages_list,
        "has_more": False,
    }

    if warning := response_attachment_warning(messages_list):
        result["_warning"] = warning

    return result


async def search_messages_impl(
    query: str | None = None,
    chat_id: str | None = None,
    message_ids: list[int] | None = None,
    reply_to_id: int | None = None,
    limit: int = 20,
    min_date: str | None = None,
    max_date: str | None = None,
    chat_type: str | None = None,
    public: bool | None = None,
    auto_expand_batches: int = 1,
    include_total_count: bool = False,
    thread_scope: ThreadScope = "auto",
    max_concurrent: int | None = _DEFAULT_MAX_CONCURRENT,
    source: str = "auto",
    match_in: str | None = None,
    brief: bool = False,
) -> dict[str, Any]:
    """
    Unified message retrieval: search, browse, read by IDs, or list replies.

    Modes: per-chat search/browse (optional query); message_ids; reply_to_id;
    global search (non-empty query required). message_ids cannot combine with
    query or reply_to_id.

    Args:
        max_concurrent: Max parallel SearchGlobal requests (default: 2).
        source: Where a global search looks. "auto" (default) adds the archive
            when one is configured, so speech and screenshots are searchable;
            with no archive it behaves exactly as before. "live" forces Telegram
            only, "archive" forces the local projection only.
        match_in: Comma-separated channels allowed to produce an archive hit:
            text, voice_transcription, file_name, media_text. Without it a
            question about what was said out loud competes with every typed
            message and loses on volume alone.
        brief: Return identifiers, dates, attachment metadata and short excerpts
            instead of whole messages. For counting, listing or filtering, where
            full text is paid for and then discarded: one such answer came back
            at 76 000 tokens when the caller needed a single filename field.
    """
    params = _build_search_params(
        query=query,
        chat_id=chat_id,
            message_ids=message_ids,
            reply_to_id=reply_to_id,
        thread_scope=thread_scope,
        limit=limit,
        min_date=min_date,
        max_date=max_date,
        chat_type=chat_type,
        public=public,
        auto_expand_batches=auto_expand_batches,
        include_total_count=include_total_count,
        max_concurrent=max_concurrent,
    )

    if thread_scope in ("full", "direct") and reply_to_id is None:
        return log_and_build_error(
            operation="get_messages",
            error_message="thread_scope requires reply_to_id",
            params=params,
            exception=ValueError("thread_scope requires reply_to_id"),
        )

    try:
        mode = resolve_mode(
            chat_id=chat_id,
            query=query,
            message_ids=message_ids,
            reply_to_id=reply_to_id,
        )
    except ValueError as e:
        return log_and_build_error(
            operation="get_messages",
            error_message=str(e),
            params=params,
            exception=e,
        )

    # source="archive" says "look only in the local projection". Calling Telegram
    # anyway was not merely wasteful: a live search that finds nothing returns an
    # error, and an error carries no `messages`, so the archive was never asked.
    # A screenshot whose recognised text answers the question came back as "no
    # messages found" — the one case the archive exists for.
    # match_in names a channel only the archive has — a voice transcript, text
    # recognised on an image, an attachment name. Mixing live text results into
    # that is not extra generosity, it is noise: asking for attachments named
    # "contract" returned four such files buried under forty-six ordinary
    # messages, and the four were read as one. A question this specific gets a
    # specific answer.
    archive_only = source == "archive" or bool(match_in)
    if archive_only and mode == "global_search" and query and query.strip():
        result: dict[str, Any] = {"messages": [], "has_more": False}
    else:
        result = await _dispatch_search_mode(
            mode,
            params,
            query=query,
            chat_id=chat_id,
            message_ids=message_ids,
            reply_to_id=reply_to_id,
            limit=limit,
            min_date=min_date,
            max_date=max_date,
            chat_type=chat_type,
            public=public,
            auto_expand_batches=auto_expand_batches,
            include_total_count=include_total_count,
            thread_scope=thread_scope,
        )

    return await _augment_with_archive(
        result,
        mode=mode,
        query=query,
        chat_id=chat_id,
        limit=limit,
        min_date=min_date,
        max_date=max_date,
        source="archive" if archive_only else source,
        match_in=match_in,
        brief=brief,
    )


async def _augment_with_archive(
    result: dict[str, Any],
    *,
    mode: MessageRetrievalMode,
    query: str | None,
    chat_id: str | None,
    limit: int,
    min_date: str | None,
    max_date: str | None,
    source: str,
    match_in: str | None = None,
    brief: bool = False,
) -> dict[str, Any]:
    """Add archive hits to a global text search, when an archive is configured.

    Applies to global search with a query only. Reading a known chat, fetching by
    id and listing replies already have an exact answer from Telegram; widening
    them would change well-defined behaviour for no gain.
    """
    if source == "live" or not isinstance(result, dict):
        return result
    if chat_id is not None or not (query and query.strip()):
        return result

    # A live search that found nothing returns an error, not an empty list. Taking
    # that as final meant the archive was never consulted, so anything only it can
    # answer — a spoken phrase, text recognised on a screenshot, an attachment
    # name — was reported as "no messages found". The error is kept only if the
    # archive has nothing either.
    live_error = None
    if "messages" not in result:
        if not result.get("ok", True) or "error" in result:
            live_error = result
            result = {"messages": [], "has_more": False}
        else:
            return result

    from src.tools.search.archive_search import (
        merge_search_results,
        search_archive_messages,
    )

    channels = [c.strip() for c in (match_in or "").split(",") if c.strip()] or None
    archived, error, archive_total = await search_archive_messages(
        query=query,
        limit=limit,
        min_date=min_date,
        max_date=max_date,
        match_in=channels,
        brief=brief,
    )
    if error:
        return live_error or {**result, "archive_status": error}
    if not archived:
        # Nothing on either side: the live error, if there was one, is the honest
        # answer — it says what was searched for.
        if live_error is not None:
            return live_error
        if source != "archive":
            return result

    live = [] if source == "archive" else list(result.get("messages") or [])
    merged, stats = merge_search_results(live, archived, limit=limit)
    out = {**result, "messages": merged, "search_sources": stats}
    if archive_total is not None and archive_total > len(archived):
        # Say that the page is a page. Without this a caller counts what it was
        # handed and reports that as the answer.
        out["archive_total_matches"] = archive_total
    return out
