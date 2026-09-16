"""``recent_activity`` — one prepared snapshot of Telegram activity.

The problem it solves: answering "what happened in the last 24 hours" used to
mean discovering chats, then opening each one, one call per chat, then waiting
while voice messages were transcribed inside the request. This assembles the
same answer from state that already exists.

Where each fact comes from, and why:

* dialog state — always live. Unread counts and read markers change the moment
  the owner opens a chat; a snapshot presented as current would be confidently
  wrong.
* message content, voice transcripts, attachment metadata, recognised image text
  — from the archive when it is fresh enough, because it is already computed.
* live top-up — only for chats the archive does not cover or has not caught up
  with, with bounded concurrency and without running speech-to-text.

What it deliberately does not do: decide who deserves a reply, which chat belongs
to which client, or what counts as an obligation. It returns Telegram facts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from src.archive import get_archive_backend
from src.archive.models import ArchiveMessage
from src.client.connection import get_connected_client
from src.config.server_config import cfg
from src.tools.activity.dialog_state import (
    DEFAULT_DIALOG_SCAN_LIMIT,
    DialogState,
    scan_dialogs,
)
from src.tools.activity.live_messages import fetch_many
from src.tools.activity.run_telemetry import RetrievalRun
from src.tools.activity.window import WindowError, resolve_window, to_archive_string
from src.utils.datetime_parse import parse_iso_datetime_utc
from src.utils.error_handling import log_and_build_error

logger = logging.getLogger(__name__)

MAX_CHATS = 200
MAX_MESSAGES_PER_CHAT = 100

# Chat kinds that are conversations rather than one-way feeds. A 24-hour audit
# drowns in channels otherwise: they out-post working chats by an order of
# magnitude while carrying nothing addressed to the owner.
CONVERSATION_TYPES = ("private", "group", "bot")


def _filter_types(requested: str | None) -> tuple[list[str] | None, str | None]:
    if not requested:
        return None, None
    wanted = [t.strip().lower() for t in requested.split(",") if t.strip()]
    valid = {"private", "group", "channel", "bot", "unknown"}
    unknown = [t for t in wanted if t not in valid]
    if unknown:
        return None, f"unsupported chat_type value(s): {', '.join(unknown)}"
    return wanted, None


def _direction_summary(messages: list[ArchiveMessage], state: DialogState | None) -> dict[str, Any]:
    """Last incoming and last outgoing, so the caller need not re-derive them."""
    last_in = None
    last_out = None
    for msg in messages:
        if msg.is_outgoing is True and last_out is None:
            last_out = msg
        elif msg.is_outgoing is False and last_in is None:
            last_in = msg
        if last_in is not None and last_out is not None:
            break

    summary: dict[str, Any] = {}
    if last_in is not None:
        summary["last_incoming"] = {"id": last_in.id, "date": last_in.date}
        if state is not None:
            summary["last_incoming"]["unread"] = state.is_message_unread(
                message_id=last_in.id, is_outgoing=False
            )
    if last_out is not None:
        summary["last_outgoing"] = {"id": last_out.id, "date": last_out.date}
    if last_in is not None and last_out is not None:
        summary["outgoing_after_incoming"] = last_out.id > last_in.id
    return summary


def _lag_seconds(last_synced_at: str | None, now: datetime) -> int | None:
    parsed = parse_iso_datetime_utc(last_synced_at)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


async def recent_activity_impl(
    since: str = "24h",
    until: str | None = None,
    accounts: list[str] | str | None = None,
    chat_type: str | None = None,
    limit_chats: int = 50,
    limit_messages_per_chat: int = 20,
    unread_only: bool = False,
    include_channels: bool = False,
    include_archived_dialogs: bool = False,
    include_telemetry: bool = False,
) -> dict[str, Any]:
    """Assemble the activity snapshot. See the module docstring for the contract."""
    params = {
        "since": since,
        "until": until,
        "accounts": accounts,
        "chat_type": chat_type,
        "limit_chats": limit_chats,
        "limit_messages_per_chat": limit_messages_per_chat,
        "unread_only": unread_only,
        "include_channels": include_channels,
    }

    if limit_chats <= 0 or limit_chats > MAX_CHATS:
        return log_and_build_error(
            operation="recent_activity",
            error_message=f"limit_chats must be between 1 and {MAX_CHATS}, got {limit_chats}",
            params=params,
            exception=ValueError("invalid limit_chats"),
        )
    if limit_messages_per_chat <= 0 or limit_messages_per_chat > MAX_MESSAGES_PER_CHAT:
        return log_and_build_error(
            operation="recent_activity",
            error_message=(
                f"limit_messages_per_chat must be between 1 and {MAX_MESSAGES_PER_CHAT}, "
                f"got {limit_messages_per_chat}"
            ),
            params=params,
            exception=ValueError("invalid limit_messages_per_chat"),
        )

    try:
        start, end = resolve_window(since, until)
    except WindowError as exc:
        return log_and_build_error(
            operation="recent_activity",
            error_message=str(exc),
            params=params,
            exception=exc,
        )

    type_filter, type_error = _filter_types(chat_type)
    if type_error:
        return log_and_build_error(
            operation="recent_activity",
            error_message=type_error,
            params=params,
            exception=ValueError(type_error),
        )

    now = datetime.now(UTC)
    since_iso = to_archive_string(start)
    until_iso = to_archive_string(end) if end else None
    run = RetrievalRun()
    config = cfg()

    client = await get_connected_client()
    me = await client.get_me()
    run.telegram_rpc_calls += 1
    account_label = (getattr(me, "username", None) or str(getattr(me, "id", ""))) or "self"

    requested_accounts = (
        [accounts] if isinstance(accounts, str) else list(accounts or [])
    )

    # ── 1. Dialog state: one live pass, not one call per chat ────────────────
    with run.phase("dialog_state"):
        scan = await scan_dialogs(
            client,
            since_iso=since_iso,
            limit=max(limit_chats, DEFAULT_DIALOG_SCAN_LIMIT),
            include_archived=include_archived_dialogs,
        )
    run.telegram_rpc_calls += scan.rpc_calls
    run.chats_discovered = len(scan.dialogs)

    allowed = set(type_filter) if type_filter else set(CONVERSATION_TYPES)
    if include_channels and not type_filter:
        allowed.add("channel")

    candidates: list[DialogState] = []
    for state in scan.dialogs:
        if state.chat_type not in allowed:
            continue
        if unread_only and not (state.unread_count or state.unread_mark):
            continue
        candidates.append(state)

    candidates.sort(
        key=lambda s: (s.last_activity_date or "", s.chat_id), reverse=True
    )
    truncated = len(candidates) > limit_chats
    candidates = candidates[:limit_chats]
    run.chats_inspected = len(candidates)
    by_id = {s.chat_id: s for s in candidates}

    # ── 2. Content from the archive ─────────────────────────────────────────
    backend = get_archive_backend()
    archive_chats: dict[int, Any] = {}
    archive_accounts: list[str] = []
    archive_error: str | None = None

    if backend is not None and candidates:
        with run.phase("archive"):
            try:
                archive_accounts = await backend.accounts()
                run.archive_queries += 1
                target = _pick_archive_account(
                    archive_accounts, requested_accounts, account_label
                )
                if target:
                    chats = await backend.recent_chats(
                        account=target,
                        since=since_iso,
                        until=until_iso,
                        chat_ids=list(by_id.keys()),
                        messages_per_chat=limit_messages_per_chat,
                    )
                    run.archive_queries += 1
                    archive_chats = {c.chat_id: c for c in chats}
            except Exception:
                run.errors += 1
                archive_error = "archive_unavailable"
                logger.exception("archive read failed — falling back to live")

    # ── 3. Live top-up only where the archive cannot answer ─────────────────
    max_lag = config.archive_max_freshness_seconds
    needs_live: list[int] = []
    for cid in by_id:
        chat = archive_chats.get(cid)
        if chat is None:
            needs_live.append(cid)
            continue
        lag = _lag_seconds(chat.last_synced_at, now)
        if lag is not None:
            run.archive_lag_seconds_max = max(run.archive_lag_seconds_max or 0, lag)
        state = by_id[cid]
        archived_top = chat.last_archived_msg_id or 0
        behind = state.top_message_id is not None and state.top_message_id > archived_top
        if behind or (lag is not None and lag > max_lag):
            needs_live.append(cid)
            run.chats_stale_topped_up += 1

    live_results: dict[int, tuple[list[ArchiveMessage], bool, str | None]] = {}
    if needs_live:
        with run.phase("live_topup"):
            live_results = await fetch_many(
                client,
                chat_ids=needs_live,
                account=account_label,
                since_iso=since_iso,
                until_iso=until_iso,
                limit=limit_messages_per_chat,
                run=run,
            )

    # ── 4. Merge, keeping provenance visible ────────────────────────────────
    payload_chats: list[dict[str, Any]] = []
    for state in candidates:
        cid = state.chat_id
        chat = archive_chats.get(cid)
        live_msgs, live_more, live_error = live_results.get(cid, ([], False, None))

        merged: dict[int, ArchiveMessage] = {}
        source = "none"
        if chat is not None and chat.messages:
            for msg in chat.messages:
                merged[msg.id] = msg
            source = "archive"
            run.chats_from_archive += 1
            run.messages_from_archive += len(chat.messages)
        if live_msgs:
            for msg in live_msgs:
                # A live read has no transcript; never let it overwrite an
                # archived message that already carries one.
                if msg.id not in merged:
                    merged[msg.id] = msg
            source = "mixed" if source == "archive" else "live"
            if source == "live":
                run.chats_from_live += 1
            run.messages_from_live += len(live_msgs)

        messages = sorted(merged.values(), key=lambda m: m.date, reverse=True)
        messages = messages[:limit_messages_per_chat]
        for msg in messages:
            run.note_media(msg.media)
        run.messages_returned += len(messages)

        entry: dict[str, Any] = state.to_dict()
        entry["account"] = account_label
        entry["source"] = source
        entry["messages"] = [
            _message_payload(msg, state) for msg in messages
        ]
        entry["has_more"] = bool(
            live_more or (chat.has_more if chat is not None else False)
        )
        if chat is not None:
            entry["archive"] = {
                "last_synced_at": chat.last_synced_at,
                "lag_seconds": _lag_seconds(chat.last_synced_at, now),
                "sync_scope": chat.sync_scope,
            }
        elif backend is not None:
            entry["archive"] = {"covered": False}
        if live_error:
            entry["partial"] = live_error
        entry.update(_direction_summary(messages, state))
        payload_chats.append(entry)

    result: dict[str, Any] = {
        "account": account_label,
        "since": since_iso,
        "until": until_iso,
        "generated_at": now.isoformat(),
        "chats": payload_chats,
        "truncated": truncated or scan.truncated,
        "coverage": {
            "chats_discovered": run.chats_discovered,
            "chats_returned": len(payload_chats),
            "chats_from_archive": run.chats_from_archive,
            "chats_topped_up_live": len(needs_live),
            "dialog_scan_truncated": scan.truncated,
            "archive_enabled": backend is not None,
            "archive_accounts": archive_accounts,
            "voice_pending": run.voice_pending,
            "media_text_pending": run.media_text_pending,
        },
    }
    if archive_error:
        result["coverage"]["archive_error"] = archive_error
    if requested_accounts:
        unavailable = [a for a in requested_accounts if a != account_label]
        if unavailable:
            # One authenticated session is one Telegram account by design, so a
            # second account is a second call with its own token — not a silent
            # gap in this answer.
            result["coverage"]["accounts_requested"] = requested_accounts
            result["coverage"]["accounts_unavailable_in_this_session"] = unavailable

    run.log()
    if include_telemetry:
        result["telemetry"] = run.to_dict()
    return result


def _pick_archive_account(
    available: list[str], requested: list[str], session_label: str
) -> str | None:
    """Choose which archived account this session may read.

    A session is authenticated as exactly one Telegram account, so it reads that
    account's archive and no one else's. An explicit request for a different
    account is refused here rather than quietly served.
    """
    if not available:
        return None
    for candidate in (session_label, *requested):
        if candidate in available:
            return candidate
    if len(available) == 1:
        return available[0]
    return None


def _message_payload(msg: ArchiveMessage, state: DialogState) -> dict[str, Any]:
    payload = msg.to_dict()
    if msg.is_outgoing is not None:
        payload["unread"] = state.is_message_unread(
            message_id=msg.id, is_outgoing=msg.is_outgoing
        )
    return payload
