"""Unread is derived from Telegram's markers, never guessed (TG-002, TG-010)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.tools.activity.dialog_state import DialogState, scan_dialogs
from tests.unit.activity.conftest import iso


def _state(**kwargs) -> DialogState:
    base = {"chat_id": 1, "read_inbox_max_id": 100}
    base.update(kwargs)
    return DialogState(**base)


def test_incoming_above_the_read_marker_is_unread():
    state = _state(read_inbox_max_id=734589)
    assert state.is_message_unread(message_id=734590, is_outgoing=False) is True


def test_incoming_at_or_below_the_marker_is_read():
    state = _state(read_inbox_max_id=734610)
    assert state.is_message_unread(message_id=734610, is_outgoing=False) is False
    assert state.is_message_unread(message_id=734600, is_outgoing=False) is False


def test_outgoing_is_never_unread_even_above_the_marker():
    """The owner's own message sitting above the marker is not something to read."""
    state = _state(read_inbox_max_id=10)
    assert state.is_message_unread(message_id=999, is_outgoing=True) is False


def test_missing_marker_does_not_invent_unread():
    state = _state(read_inbox_max_id=None)
    assert state.is_message_unread(message_id=999, is_outgoing=False) is False


def test_unread_count_zero_with_later_incoming_message_stays_read():
    """A voice message that is already listened to must not resurface as unread."""
    state = _state(read_inbox_max_id=734610, unread_count=0)
    assert state.is_message_unread(message_id=734610, is_outgoing=False) is False


def test_serialisation_keeps_read_markers_and_drops_empty_noise():
    state = _state(
        chat_id=42,
        title="T",
        chat_type="private",
        unread_count=3,
        read_inbox_max_id=10,
        read_outbox_max_id=12,
    )
    payload = state.to_dict()
    assert payload["unread_count"] == 3
    assert payload["read_inbox_max_id"] == 10
    assert payload["read_outbox_max_id"] == 12
    assert "pinned" not in payload
    assert "unread_mark" not in payload


class _FakeDialog:
    def __init__(self, chat_id, *, date, unread=0, entity=None, raw=None, title="chat"):
        self.id = chat_id
        self.date = date
        self.unread_count = unread
        self.unread_mentions_count = 0
        self.entity = entity or SimpleNamespace(id=chat_id, first_name="X", username=None)
        self.dialog = raw or SimpleNamespace(
            read_inbox_max_id=5, read_outbox_max_id=4, top_message=9,
            unread_mark=False, unread_reactions_count=0,
        )
        self.title = title
        self.name = title
        self.pinned = False
        self.archived = False


class _FakeClient:
    def __init__(self, dialogs):
        self._dialogs = dialogs
        self.calls = 0

    def iter_dialogs(self, limit=None, archived=False):
        self.calls += 1
        dialogs = self._dialogs

        async def gen():
            for d in dialogs[: limit or len(dialogs)]:
                yield d

        return gen()


@pytest.mark.asyncio
async def test_scan_filters_by_window_but_keeps_unread_older_chats():
    """An old chat with something unread still belongs in the answer."""
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    old = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    client = _FakeClient(
        [
            _FakeDialog(1, date=now),
            _FakeDialog(2, date=old),
            _FakeDialog(3, date=old, unread=2),
        ]
    )
    scan = await scan_dialogs(client, since_iso="2026-09-15T12:00:00+00:00")
    ids = [d.chat_id for d in scan.dialogs]
    assert ids == [1, 3]
    assert client.calls == 1, "discovery must be one pass, not one call per chat"


@pytest.mark.asyncio
async def test_scan_reports_truncation():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    client = _FakeClient([_FakeDialog(i, date=now) for i in range(10)])
    scan = await scan_dialogs(client, since_iso=None, limit=10)
    assert scan.truncated is True
    assert len(scan.dialogs) == 10


# ── what "truncated" is allowed to mean ─────────────────────────────────────


async def test_reaching_the_cap_past_the_window_is_not_truncation():
    """The window was covered; the cap simply arrived afterwards."""
    from tests.unit.activity.conftest import FakeClient, FakeDialog

    dialogs = [FakeDialog(1, minutes_ago=10), FakeDialog(2, minutes_ago=60 * 24 * 30)]
    scan = await scan_dialogs(
        FakeClient(dialogs), since_iso=iso(60 * 24), limit=2
    )

    assert scan.scanned == 2, "the cap was not actually reached"
    assert scan.truncated is False


async def test_reaching_the_cap_inside_the_window_is_truncation():
    """Every dialog seen was still inside the window: more may be waiting."""
    from tests.unit.activity.conftest import FakeClient, FakeDialog

    dialogs = [FakeDialog(1, minutes_ago=10), FakeDialog(2, minutes_ago=20)]
    scan = await scan_dialogs(
        FakeClient(dialogs), since_iso=iso(60 * 24), limit=2
    )

    assert scan.truncated is True
