"""Archive backend protocol.

An archive backend answers *content* questions — what was written, what a voice
message said, which file was attached — from a locally maintained projection, so
ordinary retrieval does not re-read Telegram and does not re-run speech-to-text.

It never answers *dialog state* questions. Unread counters and read markers are
live Telegram facts that change the moment the owner opens a chat; serving them
from a snapshot would look authoritative and be wrong. Those come from the live
client (see ``src/tools/activity/dialog_state.py``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.archive.models import ArchiveChat, ArchiveMessage, ArchiveSearchHit


@runtime_checkable
class ArchiveBackend(Protocol):
    """Read-only access to an archived projection of one or more accounts."""

    async def health(self) -> dict[str, object]:
        """Return reachability, schema readiness and the freshest sync timestamp."""
        ...

    async def accounts(self) -> list[str]:
        """Account labels this backend holds data for."""
        ...

    async def recent_chats(
        self,
        *,
        account: str,
        since: str,
        until: str | None = None,
        chat_ids: list[int] | None = None,
        messages_per_chat: int = 20,
    ) -> list[ArchiveChat]:
        """Chats with activity in the window, each carrying its recent messages.

        One database round trip per call, not one per chat.
        """
        ...

    async def messages_for_chats(
        self,
        *,
        account: str,
        chat_ids: list[int],
        since: str,
        until: str | None = None,
        messages_per_chat: int = 20,
    ) -> dict[int, list[ArchiveMessage]]:
        """Recent messages for an explicit chat list, keyed by chat id."""
        ...

    async def search(
        self,
        *,
        query: str,
        accounts: list[str] | None = None,
        chat_ids: list[int] | None = None,
        sender_ids: list[int] | None = None,
        since: str | None = None,
        until: str | None = None,
        media_kinds: list[str] | None = None,
        match_in: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ArchiveSearchHit]:
        """Lexical search across message text, voice transcript, attachment name
        and recognised image text, with structured filters applied inside the
        query rather than after ranking. ``match_in`` restricts which of those
        four channels may produce a hit."""
        ...

    async def close(self) -> None:
        """Release pooled connections."""
        ...
