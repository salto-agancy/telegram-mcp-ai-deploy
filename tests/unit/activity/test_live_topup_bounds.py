"""The live top-up must not spend round trips or time it cannot justify.

Both shapes here come from a production run on the work account, reported by the
owner: 36 Telegram round trips where 31 chats could not possibly hold anything
in the window, and a 31.3s answer caused by a single 30.1s channel read.
"""

import asyncio
from types import SimpleNamespace

import pytest

from src.tools.activity.live_messages import fetch_many
from src.tools.activity.recent import recent_activity_impl
from src.tools.activity.run_telemetry import RetrievalRun
from tests.unit.activity.conftest import FakeClient, FakeDialog


@pytest.fixture
def patched(monkeypatch):
    def _apply(client, archive=None):
        async def fake_client():
            return client

        monkeypatch.setattr(
            "src.tools.activity.recent.get_connected_client", fake_client
        )
        monkeypatch.setattr(
            "src.tools.activity.recent.get_archive_backend", lambda: archive
        )

    return _apply


# ── chats whose newest message predates the window ──────────────────────────


async def test_chat_older_than_window_is_never_read_live(patched):
    """Unread keeps the chat in the answer; it does not buy it a round trip."""
    fresh = FakeDialog(100, minutes_ago=30, top_message=10)
    stale = FakeDialog(200, minutes_ago=60 * 24 * 7, unread=5, top_message=3)
    client = FakeClient([fresh, stale])
    patched(client)

    result = await recent_activity_impl(since="24h", include_telemetry=True)

    assert 200 not in client.opened_chats, "chat older than the window was read live"
    assert 100 in client.opened_chats
    assert result["telemetry"]["chats_outside_window"] == 1


async def test_chat_older_than_window_still_reports_its_unread(patched):
    stale = FakeDialog(200, minutes_ago=60 * 24 * 7, unread=5, top_message=3)
    client = FakeClient([stale])
    patched(client)

    result = await recent_activity_impl(since="24h")

    by_id = {c["id"]: c for c in result["chats"]}
    assert by_id[200]["unread_count"] == 5
    assert by_id[200]["messages"] == []


# ── one slow chat does not decide the whole request ─────────────────────────


class SlowClient:
    """Reading chat 999 never finishes within the test's budget."""

    def __init__(self, slow_id: int, delay: float):
        self.slow_id = slow_id
        self.delay = delay
        self.opened: list[int] = []

    def iter_messages(self, chat_id, limit=None):
        self.opened.append(chat_id)
        delay = self.delay if chat_id == self.slow_id else 0

        async def gen():
            if delay:
                await asyncio.sleep(delay)
            yield SimpleNamespace(
                id=1,
                date=None,
                message="hi",
                out=False,
                sender_id=8,
                media=None,
                reply_to=None,
                action=None,
                edit_date=None,
            )

        return gen()


async def test_one_slow_chat_does_not_hold_the_others(monkeypatch):
    client = SlowClient(slow_id=999, delay=10)
    run = RetrievalRun()

    out = await fetch_many(
        client,
        chat_ids=[1, 999],
        account="acct",
        since_iso="2000-01-01T00:00:00+00:00",
        until_iso=None,
        limit=20,
        run=run,
        per_read_timeout=0.05,
        budget_seconds=1.0,
    )

    assert 999 not in out, "the slow chat was waited on"
    assert 999 in run.live_topup_unfinished
    assert run.errors == 0, "a timeout is a budget decision, not a failure"


async def test_budget_stops_the_phase_and_says_so(monkeypatch):
    client = SlowClient(slow_id=999, delay=10)
    run = RetrievalRun()

    out = await fetch_many(
        client,
        chat_ids=[999],
        account="acct",
        since_iso="2000-01-01T00:00:00+00:00",
        until_iso=None,
        limit=20,
        run=run,
        per_read_timeout=30.0,
        budget_seconds=0.1,
    )

    assert out == {}
    assert run.live_topup_unfinished == [999]
