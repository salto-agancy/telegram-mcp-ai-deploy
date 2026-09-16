"""Live Telegram dialog state.

Unread counters and read markers are the one thing an archive must never answer:
they change the instant the owner opens a chat, and a stale snapshot presented as
current is worse than no answer. Telegram returns the whole picture cheaply —
a single ``GetDialogs`` page carries state for a hundred dialogs — so this module
reads it live and hands facts, not judgements, to the caller.

What "unread" means here is Telegram's own definition, applied deterministically:

    incoming message and id > read_inbox_max_id  ->  unread

Whether an unread message *deserves a reply* is not decided in this gateway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Telegram serves dialogs in pages; this bounds one discovery pass.
DEFAULT_DIALOG_SCAN_LIMIT = 200


@dataclass(slots=True)
class DialogState:
    """Telegram-side state of one dialog at the moment of the call."""

    chat_id: int
    title: str | None = None
    username: str | None = None
    chat_type: str | None = None
    is_broadcast: bool = False
    is_forum: bool = False
    last_activity_date: str | None = None
    top_message_id: int | None = None
    unread_count: int = 0
    unread_mentions_count: int = 0
    unread_reactions_count: int = 0
    unread_mark: bool = False
    read_inbox_max_id: int | None = None
    read_outbox_max_id: int | None = None
    pinned: bool = False
    archived: bool = False

    def is_message_unread(self, *, message_id: int, is_outgoing: bool | None) -> bool:
        """Telegram's own rule, applied without guessing.

        An outgoing message is never unread for the owner. An incoming message is
        unread when it sits above the inbox read marker. When the marker is
        missing the honest answer is "not known to be unread", so this returns
        False rather than inventing a state.
        """
        if is_outgoing:
            return False
        if self.read_inbox_max_id is None:
            return False
        return message_id > self.read_inbox_max_id

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.chat_id,
            "type": self.chat_type,
            "unread_count": self.unread_count,
            "read_inbox_max_id": self.read_inbox_max_id,
            "read_outbox_max_id": self.read_outbox_max_id,
        }
        if self.title:
            out["title"] = self.title
        if self.username:
            out["username"] = self.username
        if self.last_activity_date:
            out["last_activity_date"] = self.last_activity_date
        if self.top_message_id is not None:
            out["top_message_id"] = self.top_message_id
        if self.unread_mentions_count:
            out["unread_mentions_count"] = self.unread_mentions_count
        if self.unread_reactions_count:
            out["unread_reactions_count"] = self.unread_reactions_count
        if self.unread_mark:
            out["unread_mark"] = True
        if self.is_forum:
            out["is_forum"] = True
        if self.pinned:
            out["pinned"] = True
        if self.archived:
            out["archived"] = True
        return out


@dataclass(slots=True)
class DialogScan:
    """Result of one discovery pass over the dialog list."""

    dialogs: list[DialogState] = field(default_factory=list)
    scanned: int = 0
    truncated: bool = False
    rpc_calls: int = 0


def _chat_type(entity: Any) -> tuple[str, bool, bool]:
    """Classify an entity into the vocabulary the gateway already uses."""
    if getattr(entity, "bot", False):
        return "bot", False, False
    if getattr(entity, "broadcast", False):
        return "channel", True, False
    if getattr(entity, "megagroup", False) or getattr(entity, "participants_count", None) is not None:
        return "group", False, bool(getattr(entity, "forum", False))
    if hasattr(entity, "first_name") or hasattr(entity, "phone"):
        return "private", False, False
    if getattr(entity, "title", None) is not None:
        return "group", False, bool(getattr(entity, "forum", False))
    return "unknown", False, False


def _state_from_dialog(dialog: Any) -> DialogState | None:
    entity = getattr(dialog, "entity", None)
    if entity is None:
        return None
    raw = getattr(dialog, "dialog", None)
    chat_type, is_broadcast, is_forum = _chat_type(entity)
    date = getattr(dialog, "date", None)

    return DialogState(
        chat_id=int(getattr(dialog, "id", 0) or getattr(entity, "id", 0)),
        title=getattr(dialog, "title", None) or getattr(dialog, "name", None),
        username=getattr(entity, "username", None),
        chat_type=chat_type,
        is_broadcast=is_broadcast,
        is_forum=is_forum,
        last_activity_date=date.isoformat() if date is not None else None,
        top_message_id=getattr(raw, "top_message", None),
        unread_count=int(getattr(dialog, "unread_count", 0) or 0),
        unread_mentions_count=int(getattr(dialog, "unread_mentions_count", 0) or 0),
        unread_reactions_count=int(getattr(raw, "unread_reactions_count", 0) or 0),
        unread_mark=bool(getattr(raw, "unread_mark", False)),
        read_inbox_max_id=getattr(raw, "read_inbox_max_id", None),
        read_outbox_max_id=getattr(raw, "read_outbox_max_id", None),
        pinned=bool(getattr(dialog, "pinned", False)),
        archived=bool(getattr(dialog, "archived", False)),
    )


async def scan_dialogs(
    client: Any,
    *,
    since_iso: str | None = None,
    limit: int = DEFAULT_DIALOG_SCAN_LIMIT,
    include_archived: bool = False,
) -> DialogScan:
    """Read dialog state for up to *limit* dialogs in one pass.

    Pinned dialogs break strict chronological ordering, so an early break on the
    first old dialog would silently drop active chats. The pass therefore walks
    the requested window and filters by date afterwards.
    """
    scan = DialogScan()
    try:
        async for dialog in client.iter_dialogs(limit=limit, archived=include_archived):
            scan.scanned += 1
            state = _state_from_dialog(dialog)
            if state is None:
                continue
            if (
                since_iso
                and state.last_activity_date
                and state.last_activity_date < since_iso
                and not state.unread_count
            ):
                # Older than the window and nothing unread: not part of this answer.
                continue
            scan.dialogs.append(state)
        scan.truncated = scan.scanned >= limit
    except Exception:
        logger.exception("dialog scan failed")
        raise
    # One iter_dialogs pass is paged by 100 server-side.
    scan.rpc_calls = max(1, (scan.scanned + 99) // 100)
    return scan
