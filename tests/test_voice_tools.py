"""Voice tools: reading a transcription must stay read-only, captioning must stay guarded.

No network: the Telegram layer is replaced by stubs, so these assert the rules rather than
Telegram's behaviour — that a caption never lands on someone else's message, that the
stored text is read back after an edit, and that a repeat read reports itself as cached.
"""

from __future__ import annotations

import pytest

from src.tools.messages import voice


class _Me:
    def __init__(self, uid: int):
        self.id = uid


class _Client:
    def __init__(self, uid: int = 100):
        self._me = _Me(uid)

    async def get_me(self):
        return self._me


def _voice_message(msg_id: int = 5, sender_id: int = 100, transcription: str | None = "сказанное"):
    msg = {
        "id": msg_id,
        "date": "2026-09-21T17:05:29+00:00",
        "text": "",
        "sender": {"id": sender_id, "username": "someone"},
        "media": {"type": "voice", "duration_seconds": 21, "mime_type": "audio/ogg"},
    }
    if transcription is not None:
        msg["transcription"] = transcription
    return msg


@pytest.fixture
def stub(monkeypatch):
    """Patch the Telegram-facing edges of the module and record what was asked of them."""
    state = {"edits": [], "message": _voice_message(), "after": None}

    async def fake_fetch(chat_id, message_id):
        if state["after"] is not None and state["edits"]:
            return state["after"]
        return state["message"]

    async def fake_edit(chat_id, message_id, new_text, parse_mode=None):
        state["edits"].append({"chat_id": chat_id, "message_id": message_id,
                               "text": new_text, "parse_mode": parse_mode})
        return {"status": "edited"}

    async def fake_client():
        return _Client()

    monkeypatch.setattr(voice, "_fetch_one", fake_fetch)
    monkeypatch.setattr(voice, "edit_message_impl", fake_edit)
    monkeypatch.setattr(voice, "get_connected_client", fake_client)
    monkeypatch.setattr(voice, "get_entity_by_id", lambda chat_id: _async(object()))
    monkeypatch.setattr(voice, "_transcription_cache_key", lambda entity, mid: ("user", 1, mid))
    monkeypatch.setattr(voice, "_transcription_cache_get", lambda key: None)
    return state


def _async(value):
    async def _coro():
        return value
    return _coro()


@pytest.mark.asyncio
async def test_transcription_is_returned_without_touching_telegram(stub):
    result = await voice.transcribe_voice_message_impl("chat", 5)
    assert result["status"] == "ready"
    assert result["transcription"] == "сказанное"
    assert result["transcription_source"] == "telegram"
    assert result["duration_seconds"] == 21
    assert result["outgoing"] is True
    assert stub["edits"] == [], "reading a voice message must never edit anything"


@pytest.mark.asyncio
async def test_a_message_without_speech_is_not_transcribed(stub):
    stub["message"] = dict(_voice_message(), media={"type": "photo"}, transcription=None)
    result = await voice.transcribe_voice_message_impl("chat", 5)
    assert result["status"] == "not_voice"
    assert "transcription" not in result


@pytest.mark.asyncio
async def test_cached_flag_reports_the_second_read(stub, monkeypatch):
    class _Done:
        def state(self, now=None):
            return "done"

    monkeypatch.setattr(voice, "_transcription_cache_get", lambda key: _Done())
    result = await voice.transcribe_voice_message_impl("chat", 5)
    assert result["status"] == "ready"
    assert result["cached"] is True


@pytest.mark.asyncio
async def test_caption_refuses_someone_elses_voice(stub):
    stub["message"] = _voice_message(sender_id=999)
    result = await voice.set_voice_caption_impl("chat", 5, "текст")
    assert result["status"] == "not_editable"
    assert stub["edits"] == [], "a refused caption must not reach Telegram at all"


@pytest.mark.asyncio
async def test_caption_refuses_a_message_that_is_not_a_voice(stub):
    stub["message"] = dict(_voice_message(), media={"type": "document"})
    result = await voice.set_voice_caption_impl("chat", 5, "текст")
    assert result["status"] == "not_voice"
    assert stub["edits"] == []


@pytest.mark.asyncio
async def test_caption_refuses_empty_text(stub):
    result = await voice.set_voice_caption_impl("chat", 5, "   ")
    assert result.get("error")
    assert stub["edits"] == []


@pytest.mark.asyncio
async def test_caption_is_read_back_after_the_edit(stub):
    text = "**1.** первое\n\n**2.** второе"
    stub["after"] = dict(_voice_message(), text=text)
    result = await voice.set_voice_caption_impl("chat", 5, text)
    assert result["status"] == "ok"
    assert result["caption_matches_requested"] is True
    assert result["media_unchanged"] is True, "the audio itself must be the same message"
    assert stub["edits"][0]["parse_mode"] == "markdown"


@pytest.mark.asyncio
async def test_a_silently_unchanged_message_is_reported_as_mismatch(stub):
    # Telegram accepted the edit and kept the old text: success here would be a lie.
    stub["after"] = dict(_voice_message(), text="старое")
    result = await voice.set_voice_caption_impl("chat", 5, "новое")
    assert result["status"] == "mismatch"
    assert result["caption_matches_requested"] is False
