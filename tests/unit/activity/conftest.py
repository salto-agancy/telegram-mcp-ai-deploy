"""Synthetic fixtures for activity retrieval tests.

Everything here is invented. Real Telegram content, chat ids and transcripts stay
out of the repository; the regression dataset built from the owner's account is
kept outside version control.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.archive.models import ArchiveChat, ArchiveMedia, ArchiveMessage

# Anchored to the moment the test runs, not to a fixed date. Freshness is judged
# against the real clock inside the implementation, so a frozen anchor made these
# fixtures look stale an hour after they were written and quietly turned an
# archive-served test into a live-fallback one.
NOW = datetime.now(UTC).replace(microsecond=0)


def iso(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def make_message(
    msg_id: int,
    *,
    chat_id: int = 100,
    account: str = "acct",
    minutes_ago: int = 10,
    outgoing: bool = False,
    text: str | None = "hello",
    media: ArchiveMedia | None = None,
    reply_to: int | None = None,
    service: bool = False,
) -> ArchiveMessage:
    return ArchiveMessage(
        id=msg_id,
        date=iso(minutes_ago),
        account=account,
        chat_id=chat_id,
        sender_id=7 if outgoing else 8,
        is_outgoing=outgoing,
        is_service=service,
        reply_to_msg_id=reply_to,
        text=text,
        media=media,
    )


def voice_media(*, ready: bool) -> ArchiveMedia:
    return ArchiveMedia(
        kind="voice",
        duration_seconds=45,
        file_reference="voice-ref",
        transcription_status="ready" if ready else "pending",
        transcription="already transcribed" if ready else None,
    )


def document_media(name: str = "prices.xlsx") -> ArchiveMedia:
    return ArchiveMedia(
        kind="document",
        file_name=name,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=20480,
        file_reference="doc-ref",
    )


class FakeDialog:
    def __init__(
        self,
        chat_id: int,
        *,
        minutes_ago: int = 5,
        unread: int = 0,
        read_inbox_max_id: int = 0,
        chat_type: str = "private",
        title: str = "chat",
        top_message: int | None = None,
    ):
        self.id = chat_id
        self.date = NOW - timedelta(minutes=minutes_ago)
        self.unread_count = unread
        self.unread_mentions_count = 0
        self.title = title
        self.name = title
        self.pinned = False
        self.archived = False
        if chat_type == "private":
            self.entity = SimpleNamespace(id=chat_id, first_name="A", username=None)
        elif chat_type == "bot":
            self.entity = SimpleNamespace(id=chat_id, bot=True, username="b", first_name="B")
        elif chat_type == "channel":
            self.entity = SimpleNamespace(id=chat_id, broadcast=True, title=title, username=None)
        else:
            self.entity = SimpleNamespace(
                id=chat_id, megagroup=True, title=title, username=None, forum=False
            )
        self.dialog = SimpleNamespace(
            read_inbox_max_id=read_inbox_max_id,
            read_outbox_max_id=0,
            top_message=top_message if top_message is not None else 0,
            unread_mark=False,
            unread_reactions_count=0,
        )


class FakeClient:
    """Minimal Telethon stand-in that counts how often each chat is opened."""

    def __init__(self, dialogs, *, live_messages=None, me_username="acct"):
        self._dialogs = dialogs
        self._live = live_messages or {}
        self.opened_chats: list[int] = []
        self.dialog_passes = 0
        self._me = SimpleNamespace(id=7, username=me_username)

    async def get_me(self):
        return self._me

    def iter_dialogs(self, limit=None, archived=False):
        self.dialog_passes += 1
        dialogs = self._dialogs

        async def gen():
            for d in dialogs[: limit or len(dialogs)]:
                yield d

        return gen()

    def iter_messages(self, chat_id, limit=None):
        self.opened_chats.append(chat_id)
        items = self._live.get(chat_id, [])

        async def gen():
            for m in items[: limit or len(items)]:
                yield m

        return gen()


class FakeArchive:
    """In-memory archive backend for merge behaviour tests."""

    def __init__(self, chats: dict[int, ArchiveChat], accounts=("acct",)):
        self._chats = chats
        self._accounts = list(accounts)
        self.queries = 0
        self.fail = False

    async def accounts(self):
        return self._accounts

    async def recent_chats(self, *, account, since, until=None, chat_ids=None,
                           messages_per_chat=20):
        self.queries += 1
        if self.fail:
            raise RuntimeError("archive down")
        out = []
        for cid, chat in self._chats.items():
            if chat_ids is not None and cid not in chat_ids:
                continue
            out.append(chat)
        return out

    async def messages_for_chats(self, **kwargs):  # pragma: no cover - unused here
        return {}

    async def search(self, **kwargs):  # pragma: no cover - unused here
        return []

    async def health(self):  # pragma: no cover - unused here
        return {"reachable": True}

    async def close(self):  # pragma: no cover - unused here
        return None


def archived_chat(
    chat_id: int,
    messages,
    *,
    account: str = "acct",
    synced_minutes_ago: int = 20,
    last_msg_id: int | None = None,
    has_more: bool = False,
) -> ArchiveChat:
    return ArchiveChat(
        account=account,
        chat_id=chat_id,
        title="archived chat",
        chat_type="private",
        last_synced_at=iso(synced_minutes_ago),
        last_archived_msg_id=last_msg_id if last_msg_id is not None
        else max((m.id for m in messages), default=0),
        messages=list(messages),
        has_more=has_more,
    )


@pytest.fixture
def now() -> datetime:
    return NOW
