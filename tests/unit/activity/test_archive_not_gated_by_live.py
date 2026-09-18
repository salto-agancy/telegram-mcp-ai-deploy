"""A live search that finds nothing must not silence the archive.

Reported from production: asking for a word that exists only in text recognised
on a screenshot, with a chat-type filter, returned "No messages found". The hits
were in the archive the whole time — but a live search that finds nothing returns
an error, an error carries no `messages` key, and the augmentation step treated
that as "nothing to add to". The one case the archive exists for was the one case
it was never asked about.
"""

import pytest

from src.tools.search.core import _augment_with_archive

LIVE_EMPTY_ERROR = {
    "ok": False,
    "error": "No messages found matching query 'checkoutservice'",
    "operation": "get_messages",
}


@pytest.fixture
def archive_with(monkeypatch):
    def _apply(hits, error=None):
        async def fake_search(**kwargs):
            return hits, error, None

        monkeypatch.setattr(
            "src.tools.search.archive_search.search_archive_messages", fake_search
        )

    return _apply


async def test_archive_answers_even_when_live_errored(archive_with):
    archive_with([{"id": 1, "chat_id": 5, "source": "archive", "text": "screenshot"}])

    out = await _augment_with_archive(
        dict(LIVE_EMPTY_ERROR), mode="global_search", query="checkoutservice",
        chat_id=None, limit=20, min_date=None, max_date=None, source="auto",
    )

    assert out.get("messages"), "the archive hit was swallowed by the live error"
    assert out["messages"][0]["id"] == 1


async def test_the_live_error_survives_when_the_archive_is_empty_too(archive_with):
    archive_with([])

    out = await _augment_with_archive(
        dict(LIVE_EMPTY_ERROR), mode="global_search", query="checkoutservice",
        chat_id=None, limit=20, min_date=None, max_date=None, source="auto",
    )

    assert out["ok"] is False
    assert "No messages found" in out["error"]


async def test_a_broken_archive_does_not_invent_success(archive_with):
    archive_with([], error="archive_unavailable")

    out = await _augment_with_archive(
        dict(LIVE_EMPTY_ERROR), mode="global_search", query="checkoutservice",
        chat_id=None, limit=20, min_date=None, max_date=None, source="auto",
    )

    assert out["ok"] is False


async def test_a_normal_live_result_is_still_merged(archive_with):
    archive_with([{"id": 2, "chat_id": 5, "source": "archive", "text": "from archive"}])

    out = await _augment_with_archive(
        {"messages": [{"id": 1, "chat_id": 5, "text": "from live"}], "has_more": False},
        mode="global_search", query="checkoutservice", chat_id=None, limit=20,
        min_date=None, max_date=None, source="auto",
    )

    ids = {m["id"] for m in out["messages"]}
    assert ids == {1, 2}
