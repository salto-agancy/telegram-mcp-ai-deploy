"""Scheduled index, parking rules and changes to chosen items."""

import datetime as dt
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.tools.outbox import parking, scheduled, store

NOW = dt.datetime(2026, 11, 11, 7, 0, tzinfo=dt.UTC)


def msk(y, m, d, hh, mm, ss=0):
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=parking.MSK)


# --- parking rules -------------------------------------------------------------


def test_far_and_2359_is_parking():
    assert parking.classify(msk(2027, 11, 9, 23, 59), NOW) == "parking"


def test_one_sign_only_looks_like_parking_and_is_never_touched():
    far_daytime = msk(2027, 6, 12, 21, 32)  # the owner's existing parked note
    assert parking.classify(far_daytime, NOW) == "looks_like_parking"
    assert parking.action(far_daytime, NOW, parking.classify(far_daytime, NOW)) is None
    soon_2359 = msk(2026, 12, 1, 23, 59)
    assert parking.classify(soon_2359, NOW) == "looks_like_parking"


def test_birthday_greeting_two_months_ahead_is_real():
    assert parking.classify(msk(2027, 1, 10, 10, 0), NOW) is None


def test_prolong_keeps_order_within_a_chat_and_stays_inside_telegram_limit():
    a, b = parking.prolong_target(NOW, 0), parking.prolong_target(NOW, 1)
    assert (a.hour, a.minute, a.second, b.second) == (23, 59, 0, 1)
    assert b - NOW < dt.timedelta(days=367)


def test_actions_by_time_left():
    assert parking.action(NOW + dt.timedelta(days=340), NOW, "parking") is None
    assert parking.action(NOW + dt.timedelta(days=300), NOW, "parking") == "prolong"
    assert parking.action(NOW + dt.timedelta(days=5), NOW, "parking") == "evacuate"


# --- index ---------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTBOX_DB", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("ARCHIVE_ACCOUNT", "work")
    monkeypatch.setattr(scheduled, "current_agent", lambda: "claude")


def item(msg_id, when, text):
    return {
        "msg_id": msg_id,
        "scheduled_for": when,
        "text": text,
        "has_media": False,
        "topic_id": None,
        "version": scheduled.item_version(text, when),
    }


@pytest.mark.asyncio
async def test_index_tracks_new_edited_and_removed_items(db):
    a = item(1, "2027-06-12T18:32:07+00:00", "note")
    out = await store.call(
        scheduled._replace_chat, "work", -100, "chat:100", "G", [a], None
    )
    assert out["changed"]
    same = await store.call(
        scheduled._replace_chat, "work", -100, "chat:100", "G", [a], None
    )
    assert not same["changed"]
    edited = item(1, "2027-06-12T18:32:07+00:00", "note, edited on the phone")
    assert (
        await store.call(
            scheduled._replace_chat, "work", -100, "chat:100", "G", [edited], None
        )
    )["changed"]
    await store.call(scheduled._replace_chat, "work", -100, "chat:100", "G", [], None)
    listed = await scheduled.scheduled_list_impl()
    assert listed["total"] == 0


@pytest.mark.asyncio
async def test_a_read_error_keeps_the_known_items(db):
    a = item(1, "2027-06-12T18:32:07+00:00", "note")
    await store.call(scheduled._replace_chat, "work", -100, "chat:100", "G", [a], None)
    await store.call(
        scheduled._replace_chat,
        "work",
        -100,
        "chat:100",
        "G",
        [],
        "ChatAdminRequiredError",
    )
    assert (await scheduled.scheduled_list_impl())["total"] == 1


@pytest.mark.asyncio
async def test_list_covers_both_accounts_and_marks_parking(db):
    far = (
        (NOW + dt.timedelta(days=360))
        .astimezone(parking.MSK)
        .replace(hour=23, minute=59, second=0)
    )
    await store.call(
        scheduled._replace_chat,
        "work",
        -100,
        "chat:100",
        "G",
        [item(1, far.astimezone(dt.UTC).isoformat(), "parked")],
        None,
    )
    await store.call(
        scheduled._replace_chat,
        "personal",
        5,
        "user:5",
        "P",
        [item(2, "2026-12-01T07:00:00+00:00", "real")],
        None,
    )
    res = await scheduled.scheduled_list_impl()
    kinds = {i["account"]: i["kind"] for i in res["items"]}
    assert kinds == {"work": "parking", "personal": "real"}
    assert {i["ref"] for i in res["items"]} == {"work:chat:100:1", "personal:user:5:2"}
    only = await scheduled.scheduled_list_impl(kind="parking")
    assert [i["account"] for i in only["items"]] == ["work"]


# --- changes -------------------------------------------------------------------


def live(msg_id, when, text):
    from telethon.tl import types

    m = MagicMock(spec=types.Message)
    m.id, m.date, m.message, m.media, m.reply_to = msg_id, when, text, None, None
    return m


@pytest.fixture
def client(monkeypatch, db):
    c = AsyncMock()
    c.get_input_entity.return_value = object()
    c.get_peer_id.return_value = -100
    monkeypatch.setattr(
        "src.client.connection.get_connected_client", AsyncMock(return_value=c)
    )
    monkeypatch.setattr(scheduled.types, "Message", type(live(1, NOW, "x")))
    return c


@pytest.mark.asyncio
async def test_item_changed_on_the_phone_is_a_conflict_and_untouched(client):
    when = dt.datetime(2027, 6, 12, 18, 32, 7, tzinfo=dt.UTC)
    client.return_value = MagicMock(messages=[live(1, when, "edited on the phone")])
    stale = scheduled.item_version("original", when.isoformat(timespec="seconds"))
    res = await scheduled.scheduled_change_impl(
        [{"ref": "work:chat:100:1", "expected_version": stale, "text": "agent text"}]
    )
    assert res["results"][0]["status"] == "conflict"
    assert client.await_count == 1  # only the read; no edit was sent


@pytest.mark.asyncio
async def test_other_account_items_are_not_executed_here(client):
    res = await scheduled.scheduled_change_impl(
        [{"ref": "personal:user:5:2", "expected_version": "x", "cancel": True}]
    )
    assert res["results"][0]["status"] == "other_account"
    client.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_with_current_version_and_retry_changes_nothing_twice(client):
    when = dt.datetime(2027, 6, 12, 18, 32, 7, tzinfo=dt.UTC)
    client.return_value = MagicMock(messages=[live(1, when, "note")])
    ver = scheduled.item_version("note", when.isoformat(timespec="seconds"))
    args = [{"ref": "work:chat:100:1", "expected_version": ver, "cancel": True}]
    first = await scheduled.scheduled_change_impl(args, operation_key="op-c")
    calls = client.await_count
    again = await scheduled.scheduled_change_impl(args, operation_key="op-c")
    assert first["results"][0]["status"] == "cancelled"
    assert again["results"][0]["repeat"] is True and client.await_count == calls


# --- one write path ------------------------------------------------------------


def test_scheduled_writes_live_only_in_the_guarded_module():
    root = pathlib.Path(__file__).resolve().parents[3] / "src"
    places = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "EditMessageRequest" in text or "DeleteScheduledMessagesRequest" in text:
            places.add(path.relative_to(root).as_posix())
    assert places == {"tools/outbox/scheduled.py"}


@pytest.mark.asyncio
async def test_index_state_is_written_and_read(db):
    """Regression (live, 11.11.2026): the collector passes state fields by keyword."""
    await store.call(scheduled._set_state, "work", listener="t1", last_tick="t2")
    state = await store.call(scheduled._state, "work")
    assert state[0]["listener"] == "t1" and state[0]["last_tick"] == "t2"


class FakeClient:
    """Just enough of TelegramClient for one collector tick."""

    def __init__(self, dialogs, scheduled):
        self._dialogs, self._scheduled, self.requests = dialogs, scheduled, []

    async def iter_dialogs(self, limit=None, **_):
        for d in self._dialogs[:limit]:
            yield d

    async def get_peer_id(self, entity):
        return -entity.id

    async def __call__(self, request):
        self.requests.append(type(request).__name__)
        return MagicMock(messages=self._scheduled)


@pytest.mark.asyncio
async def test_collector_tick_indexes_recent_dialogs(db, monkeypatch):
    from telethon.tl import types

    chat = types.Chat(
        id=100,
        title="G",
        photo=types.ChatPhotoEmpty(),
        participants_count=2,
        date=NOW,
        version=1,
    )
    when = dt.datetime(2027, 6, 12, 18, 32, 7, tzinfo=dt.UTC)
    client = FakeClient(
        [MagicMock(id=-100, entity=chat, input_entity=object())],
        [live(7, when, "parked note")],
    )
    monkeypatch.setattr(scheduled, "SWEEP_PAUSE", 0)
    collector = scheduled.Collector("work", AsyncMock(return_value=client))
    collector._handler_on = client  # skip real event registration
    await collector.tick()
    res = await scheduled.scheduled_list_impl()
    assert [i["ref"] for i in res["items"]] == ["work:chat:100:7"]
    assert res["coverage"]["work"]["last_tick"]
    assert client.requests == ["GetScheduledHistoryRequest"]
