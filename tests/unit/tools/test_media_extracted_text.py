"""An attachment must come back with whatever has already been read from it.

Reported from production: an agent received the pixels of a payment screen, could
not name the service, and had no way to tell "nobody has read this image yet"
from "there is nothing on it". Both now say so in words.
"""

from types import SimpleNamespace

import pytest

from src.tools.messages.media_content import _extracted_text


class FakeArchive:
    def __init__(self, rows):
        self.rows = rows
        self.asked = None

    async def accounts(self):
        return ["personal"]

    async def enrichment_for_messages(self, *, account, chat_id, msg_ids):
        self.asked = (account, chat_id, sorted(msg_ids))
        return {mid: self.rows[mid] for mid in msg_ids if mid in self.rows}


@pytest.fixture
def archive(monkeypatch):
    def _apply(rows):
        fake = FakeArchive(rows)
        monkeypatch.setattr(
            "src.tools.messages.media_content.get_archive_backend", lambda: fake
        )
        monkeypatch.setattr(
            "src.tools.messages.media_content.cfg",
            lambda: SimpleNamespace(archive_account="personal"),
        )
        return fake

    return _apply


async def test_recognised_screenshot_text_comes_back(archive):
    archive({1: {"media_text": "Payment failed — checkout.example.com",
                 "media_text_status": "ready",
                 "voice_transcription": None}})

    out = await _extracted_text(392236379, [1])

    assert "checkout.example.com" in out[1]


async def test_voice_transcript_wins_over_a_pointer(archive):
    archive({2: {"media_text": None, "media_text_status": None,
                 "voice_transcription": "я оплатил счёт"}})

    out = await _extracted_text(392236379, [2])

    assert out[2] == "transcript: я оплатил счёт"


async def test_queued_says_queued_rather_than_nothing(archive):
    archive({3: {"media_text": None, "media_text_status": "pending",
                 "voice_transcription": None}})

    out = await _extracted_text(392236379, [3])

    assert "queued" in out[3]
    assert "guessing" in out[3], "the agent must be told not to invent the content"


async def test_nothing_on_the_image_is_said_out_loud(archive):
    archive({4: {"media_text": None, "media_text_status": "skipped",
                 "voice_transcription": None}})

    out = await _extracted_text(392236379, [4])

    assert "no text" in out[4]


async def test_archive_failure_never_blocks_the_media(monkeypatch):
    class Broken:
        async def accounts(self):
            raise RuntimeError("archive down")

    monkeypatch.setattr(
        "src.tools.messages.media_content.get_archive_backend", lambda: Broken()
    )

    assert await _extracted_text(1, [1]) == {}


async def test_no_archive_configured_is_not_an_error(monkeypatch):
    monkeypatch.setattr(
        "src.tools.messages.media_content.get_archive_backend", lambda: None
    )

    assert await _extracted_text(1, [1]) == {}
