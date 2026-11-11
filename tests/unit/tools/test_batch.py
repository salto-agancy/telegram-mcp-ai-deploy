"""Batch scheduling: checked by code, ordered, never immediate, never duplicated."""

import datetime as dt
import pathlib
from unittest.mock import AsyncMock

import pytest
from telethon.errors import RPCError
from telethon.tl import types

from src.tools.outbox import batch, parking, store

NOW = dt.datetime(2026, 11, 11, 7, 0, tzinfo=dt.UTC)


def row(to="user:100000001", text="hi", at=None, **kw):
    return {"to": to, "text": text, **({"at_msk": at} if at else {}), **kw}


def test_rows_without_date_become_parking_in_dictated_order():
    rows = batch.plan_rows(
        [row(text="1"), row(text="2"), row(text="3")], "work", NOW, {}
    )
    assert [r["kind"] for r in rows] == ["parking"] * 3
    assert [r["at"].second for r in rows] == [0, 1, 2]
    assert all(
        r["at"].astimezone(parking.MSK).strftime("%H:%M") == "23:59" for r in rows
    )
    assert all(parking.classify(r["at"], NOW) == "parking" for r in rows)


def test_same_time_in_one_chat_keeps_order():
    at = "2027-03-01T10:00"
    rows = batch.plan_rows(
        [row(text="first", at=at), row(text="second", at=at)], "work", NOW, {}
    )
    assert rows[0]["at"] < rows[1]["at"]


def test_date_is_moscow_time():
    rows = batch.plan_rows([row(at="2027-03-01T10:00")], "work", NOW, {})
    assert rows[0]["at"].astimezone(dt.UTC).hour == 7


def test_beyond_horizon_and_too_soon_are_refused_not_queued():
    rows = batch.plan_rows(
        [
            row(at="2030-01-01T10:00"),
            row(
                at=(NOW + dt.timedelta(seconds=30)).astimezone(parking.MSK).isoformat()
            ),
        ],
        "work",
        NOW,
        {},
    )
    assert not rows[0]["ok"] and "limit" in rows[0]["issues"][0]
    assert not rows[1]["ok"] and "2 minutes" in rows[1]["issues"][0]


def test_per_chat_limit_counts_what_is_already_there():
    rows = batch.plan_rows(
        [row(at="2027-03-01T10:00"), row(at="2027-03-02T10:00")],
        "work",
        NOW,
        {"user:100000001": 99},
    )
    assert [r["ok"] for r in rows] == [True, False]


def test_bare_number_and_empty_text_are_issues():
    rows = batch.plan_rows([row(to="200000002"), row(text="  ")], "work", NOW, {})
    assert not rows[0]["ok"] and not rows[1]["ok"]


def test_random_id_is_stable_per_row():
    assert (
        batch.random_id("op", 3) == batch.random_id("op", 3) != batch.random_id("op", 4)
    )
    assert 0 < batch.random_id("op", 3) < 2**63


# --- apply -----------------------------------------------------------------------


class FakeClient:
    def __init__(self, duplicate_seqs=()):
        self.sent, self._dup = [], set(duplicate_seqs)

    async def get_entity(self, target):
        return types.User(id=100000001, first_name="Contact")

    async def get_peer_id(self, entity):
        return 100000001

    async def __call__(self, request):
        if isinstance(request, batch.functions.messages.SendMessageRequest):
            seq = next(
                s for s in range(50) if batch.random_id("op-1", s) == request.random_id
            )
            assert request.schedule_date is not None  # never an immediate send
            if seq in self._dup:
                err = RPCError(request, "RANDOM_ID_DUPLICATE", 500)
                raise err
            self.sent.append(seq)
            msg = types.Message(
                id=1000 + seq,
                peer_id=types.PeerUser(100000001),
                date=request.schedule_date,
                message=request.message,
            )
            return types.Updates(
                updates=[types.UpdateNewScheduledMessage(message=msg)],
                users=[],
                chats=[],
                date=NOW,
                seq=0,
            )
        return type("R", (), {"messages": []})()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTBOX_DB", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("ARCHIVE_ACCOUNT", "work")
    monkeypatch.setattr(batch, "current_agent", lambda: "chatgpt")


async def _run(monkeypatch, client, items, key="op-1"):
    monkeypatch.setattr(
        "src.client.connection.get_connected_client", AsyncMock(return_value=client)
    )
    res = await batch.schedule_batch_impl("apply", items, key)
    if key in batch._tasks:
        await batch._tasks[key]
    return res


@pytest.mark.asyncio
async def test_preview_writes_nothing(env, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(
        "src.client.connection.get_connected_client", AsyncMock(return_value=client)
    )
    res = await batch.schedule_batch_impl("preview", [row(at="2027-03-01T10:00")], None)
    assert res["summary"]["ok"] == 1 and client.sent == []
    assert res["rows"][0]["title"] == "Contact"


@pytest.mark.asyncio
async def test_apply_then_retry_never_duplicates_and_resumes_the_rest(env, monkeypatch):
    items = [row(text="a", at="2027-03-01T10:00"), row(text="b", at="2027-03-01T10:05")]
    first = await _run(
        monkeypatch, FakeClient(), [*items[:1], dict(items[1], to="5")]
    )  # 2nd invalid
    assert first["status"] == "started"
    client = FakeClient()
    again = await _run(monkeypatch, client, items)
    assert client.sent == [1]  # only the row that was not applied yet
    assert again["queued"] == 1
    status = await batch.operation_status_impl("op-1")
    assert status["summary"] == {"scheduled": 2}


@pytest.mark.asyncio
async def test_telegram_duplicate_is_reported_as_accepted_earlier(env, monkeypatch):
    await _run(
        monkeypatch, FakeClient(duplicate_seqs={0}), [row(at="2027-03-01T10:00")]
    )
    status = await batch.operation_status_impl("op-1")
    assert status["summary"] == {"scheduled_earlier": 1}


def test_send_message_request_lives_only_in_the_batch_module():
    root = pathlib.Path(__file__).resolve().parents[3] / "src"
    places = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "SendMessageRequest" in p.read_text(encoding="utf-8")
    }
    assert places == {"tools/outbox/batch.py"}


def test_store_has_no_duplicate_rows_per_operation_seq(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("OUTBOX_DB", str(tmp_path / "o.db"))
    conn = store._connect()
    row_ = {
        "created_at": "t",
        "account": "work",
        "agent": "a",
        "operation_key": "k",
        "seq": 0,
        "kind": "scheduled",
        "peer": "user:1",
        "text_agent": "x",
        "version_agent": "v",
        "status": "scheduled",
    }
    store._insert(conn, row_, None)
    with pytest.raises(sqlite3.IntegrityError):
        store._insert(conn, row_, None)
