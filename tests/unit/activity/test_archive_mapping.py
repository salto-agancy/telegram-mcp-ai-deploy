"""Row mapping and enrichment status rules for the Postgres archive backend.

The central rule under test: a voice message whose transcript has not been
produced yet reports ``pending``. Reporting it as absent would let a caller
conclude nothing was said.
"""

from src.archive.postgres import _media_from_row, _message_from_row


def _row(**kwargs):
    base = {
        "msg_id": 1,
        "chat_id": 10,
        "account": "acct",
        "from_id": 8,
        "date": "2026-09-16T12:00:00+00:00",
        "text": None,
        "media_type": None,
        "voice_duration": None,
        "voice_transcription": None,
        "voice_file_id": None,
        "file_name": None,
        "file_mime": None,
        "file_size": None,
        "media_file_id": None,
        "media_text": None,
        "media_text_status": None,
        "reply_to_msg_id": None,
        "is_outgoing": None,
        "is_service": 0,
        "edit_date": None,
    }
    base.update(kwargs)
    return base


def test_plain_text_message_has_no_media_block():
    message = _message_from_row(_row(text="hello"))
    assert message.media is None
    assert message.to_dict() == {"id": 1, "date": "2026-09-16T12:00:00+00:00", "sender_id": 8,
                                 "text": "hello"}


def test_voice_with_transcript_is_ready():
    media = _media_from_row(
        _row(media_type="voice", voice_file_id="v", voice_duration=45,
             voice_transcription="spoken words")
    )
    assert media.transcription_status == "ready"
    assert media.transcription == "spoken words"
    assert media.duration_seconds == 45


def test_voice_without_transcript_is_pending_not_absent():
    media = _media_from_row(_row(media_type="voice", voice_file_id="v", voice_duration=12))
    assert media.transcription_status == "pending"
    assert media.transcription is None
    assert "transcription" not in media.to_dict()
    assert media.to_dict()["transcription_status"] == "pending"


def test_round_video_counts_as_voice_for_transcription():
    media = _media_from_row(_row(media_type="round", voice_file_id="v"))
    assert media.transcription_status == "pending"


def test_photo_awaiting_recognition_is_pending():
    media = _media_from_row(_row(media_type="photo", media_file_id="p"))
    assert media.media_text_status == "pending"
    assert media.transcription_status == "absent"


def test_photo_with_recognised_text_is_ready():
    media = _media_from_row(
        _row(media_type="photo", media_file_id="p", media_text="text on screen",
             media_text_status="ready")
    )
    assert media.media_text_status == "ready"
    assert media.media_text == "text on screen"


def test_failed_recognition_is_reported_as_failed():
    media = _media_from_row(
        _row(media_type="photo", media_file_id="p", media_text_status="failed")
    )
    assert media.media_text_status == "failed"


def test_document_without_caption_keeps_its_file_facts():
    media = _media_from_row(
        _row(media_type="document", file_name="prices.xlsx", file_size=2048,
             file_mime="application/vnd.ms-excel", media_file_id="d")
    )
    payload = media.to_dict()
    assert payload["file_name"] == "prices.xlsx"
    assert payload["size_bytes"] == 2048
    assert payload["file_reference"] == "d"


def test_direction_and_reply_are_carried_through():
    message = _message_from_row(
        _row(is_outgoing=1, reply_to_msg_id=99, is_service=0, text="answer")
    )
    assert message.is_outgoing is True
    assert message.reply_to_msg_id == 99
    payload = message.to_dict()
    assert payload["is_outgoing"] is True
    assert payload["reply_to_msg_id"] == 99


def test_unknown_direction_stays_unknown_rather_than_defaulting_to_incoming():
    """Rows collected before direction was recorded must not claim a direction."""
    message = _message_from_row(_row(is_outgoing=None, text="legacy row"))
    assert message.is_outgoing is None
    assert "is_outgoing" not in message.to_dict()


def test_service_message_flag_survives():
    message = _message_from_row(_row(is_service=1))
    assert message.is_service is True
    assert message.to_dict()["is_service"] is True
