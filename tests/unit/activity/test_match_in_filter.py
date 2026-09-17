"""A question aimed at one channel must be expressible.

Reported from production: the owner's archive held 12 051 voice transcripts, yet
a search for a common word returned fifty typed messages and not one transcript.
The hits were not missing — they were outvoted. `match_in` states the channel
instead of hoping ranking picks it.
"""

import pytest

from src.tools.search.archive_search import search_archive_messages


class RecordingBackend:
    """Captures the filters the search layer actually sends down."""

    def __init__(self):
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return []


@pytest.fixture
def backend(monkeypatch):
    b = RecordingBackend()
    monkeypatch.setattr("src.tools.search.archive_search.get_archive_backend", lambda: b)
    return b


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
