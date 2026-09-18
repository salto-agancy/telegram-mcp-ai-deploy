"""A question aimed at one channel must be expressible.

Reported from production: the owner's archive held 12 051 voice transcripts, yet
a search for a common word returned fifty typed messages and not one transcript.
The hits were not missing — they were outvoted. `match_in` states the channel
instead of hoping ranking picks it.
"""

from types import SimpleNamespace

import pytest

from src.tools.search.archive_search import search_archive_messages


class RecordingBackend:
    """Captures the filters the search layer actually sends down."""

    def __init__(self):
        self.calls: list[dict] = []

    async def accounts(self):
        return ["personal"]

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return []


@pytest.fixture
def backend(monkeypatch):
    b = RecordingBackend()
    monkeypatch.setattr("src.tools.search.archive_search.get_archive_backend", lambda: b)
    monkeypatch.setattr(
        "src.tools.search.archive_search.cfg",
        lambda: SimpleNamespace(archive_account="personal"),
    )
    return b


async def test_the_search_is_confined_to_this_session_account(backend):
    """Both connectors were served the same rows before this was passed down."""
    await search_archive_messages(query="договор", limit=20)

    assert backend.calls[0]["accounts"] == ["personal"]


async def test_an_undecidable_account_skips_the_archive(monkeypatch):
    """Two accounts and no way to choose: refuse rather than serve the wrong one."""

    class TwoAccounts(RecordingBackend):
        async def accounts(self):
            return ["personal", "work"]

    b = TwoAccounts()
    monkeypatch.setattr("src.tools.search.archive_search.get_archive_backend", lambda: b)
    monkeypatch.setattr(
        "src.tools.search.archive_search.cfg",
        lambda: SimpleNamespace(archive_account=""),
    )

    hits, error = await search_archive_messages(query="договор", limit=20)

    assert hits == []
    assert error == "archive_account_undetermined"
    assert b.calls == [], "the archive was queried without knowing whose rows to read"


async def test_channel_filter_reaches_the_backend(backend):
    await search_archive_messages(query="оплата", limit=20, match_in=["voice_transcription"])

    assert backend.calls[0]["match_in"] == ["voice_transcription"]


async def test_no_filter_means_every_channel(backend):
    await search_archive_messages(query="оплата", limit=20)

    assert backend.calls[0]["match_in"] is None


async def test_comma_separated_request_becomes_a_list(monkeypatch):
    seen = {}

    async def fake_search(**kwargs):
        seen.update(kwargs)
        return [], None

    monkeypatch.setattr(
        "src.tools.search.archive_search.search_archive_messages", fake_search
    )
    from src.tools.search.core import _augment_with_archive

    await _augment_with_archive(
        {"messages": []},
        mode="global",
        query="оплата",
        chat_id=None,
        limit=10,
        min_date=None,
        max_date=None,
        source="archive",
        match_in="voice_transcription, media_text",
    )

    assert seen["match_in"] == ["voice_transcription", "media_text"]
