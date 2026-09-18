"""A list of hits should cost what a list costs, and a page should say it is one.

Both shapes come from a real session. Asking which attachments have "договор" in
the filename returned an answer of roughly 76 000 tokens because every matching
message arrived in full — the caller needed one field. Asking how many voice
messages mention payment returned the fifty that were requested, and fifty was
reported as the answer; the archive held 242.
"""

from types import SimpleNamespace

import pytest

from src.tools.search.archive_search import _hit_to_message, search_archive_messages


class Hit:
    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return dict(self._payload)


FULL = {
    "id": 7,
    "date": "2026-09-18T06:00:00+00:00",
    "chat_id": 42,
    "account": "personal",
    "matched_in": ["file_name"],
    "text": "и" * 4000,
    "media": {
        "kind": "document",
        "file_name": "договор.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 20480,
        "transcription": "ц" * 5000,
    },
}


def test_brief_keeps_what_identifies_a_hit():
    out = _hit_to_message(Hit(FULL), brief=True)

    assert out["id"] == 7
    assert out["media"]["file_name"] == "договор.pdf"
    assert out["matched_in"] == ["file_name"]


def test_brief_cuts_the_long_fields_instead_of_dropping_them():
    out = _hit_to_message(Hit(FULL), brief=True)

    assert out["text"].endswith("…")
    assert len(out["text"]) < 200, "the excerpt is not an excerpt"
    assert len(str(out)) < len(str(_hit_to_message(Hit(FULL)))) / 10


def test_full_mode_is_untouched():
    out = _hit_to_message(Hit(FULL))

    assert len(out["text"]) == 4000


class CountingBackend:
    def __init__(self, hits, total):
        self._hits = hits
        self._total = total
        self.counted = False

    async def accounts(self):
        return ["personal"]

    async def search(self, **kwargs):
        return self._hits[: kwargs["limit"]]

    async def count_matches(self, **kwargs):
        self.counted = True
        return self._total


@pytest.fixture
def backend(monkeypatch):
    def _apply(hit_count, total):
        b = CountingBackend([Hit(FULL)] * hit_count, total)
        monkeypatch.setattr(
            "src.tools.search.archive_search.get_archive_backend", lambda: b
        )
        monkeypatch.setattr(
            "src.tools.search.archive_search.cfg",
            lambda: SimpleNamespace(archive_account="personal"),
        )
        return b

    return _apply


async def test_a_full_page_reports_how_many_there_are(backend):
    b = backend(hit_count=500, total=242)

    _hits, _error, total = await search_archive_messages(query="оплата", limit=5)

    assert b.counted
    assert total == 242


async def test_a_short_page_needs_no_count(backend):
    b = backend(hit_count=2, total=2)

    _hits, _error, total = await search_archive_messages(query="оплата", limit=50)

    assert b.counted is False, "counted a page that was already the whole answer"
    assert total is None
