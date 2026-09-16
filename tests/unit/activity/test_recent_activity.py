"""recent_activity contract: correctness first, latency second.

These cover the regression shapes the benchmark names: attachment-only messages,
voice with and without a transcript, service messages, empty periods, noisy
high-volume feeds, stale archive copies and partially unavailable chats.
"""

from types import SimpleNamespace

import pytest

from src.tools.activity.recent import recent_activity_impl
from tests.unit.activity.conftest import (
    NOW,
    FakeArchive,
    FakeClient,
    FakeDialog,
    archived_chat,
    document_media,
    make_message,
    voice_media,
)


async def _async(value):
    return value


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


# ── batching ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_covered_chats_are_never_opened_one_by_one(patched):
    """The whole point: no live read per chat when the archive is fresh."""
    dialogs = [FakeDialog(i, minutes_ago=5, top_message=10) for i in range(1, 6)]
    archive = FakeArchive(
        {
            i: archived_chat(i, [make_message(10, chat_id=i)], last_msg_id=10)
            for i in range(1, 6)
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    result = await recent_activity_impl(since="24h", include_telemetry=True)

    assert client.opened_chats == [], "no per-chat live read should happen"
    assert client.dialog_passes == 1
    assert len(result["chats"]) == 5
    assert result["coverage"]["chats_from_archive"] == 5
    assert result["coverage"]["chats_topped_up_live"] == 0


@pytest.mark.asyncio
async def test_without_archive_it_still_works_live(patched):
    """A deployment with no archive configured degrades, it does not break."""
    dialogs = [FakeDialog(1, minutes_ago=5, top_message=3)]
    live = {
        1: [
            SimpleNamespace(
                id=3, date=NOW, text="live text", message="live text", out=False,
                sender_id=8, media=None, reply_to=None, reply_to_msg_id=None,
                edit_date=None, action=None,
            )
        ]
    }
    client = FakeClient(dialogs, live_messages=live)
    patched(client, None)

    result = await recent_activity_impl(since="24h")

    assert result["coverage"]["archive_enabled"] is False
    assert client.opened_chats == [1]
    assert result["chats"][0]["messages"][0]["text"] == "live text"


# ── unread ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unread_flag_follows_the_read_marker(patched):
    dialogs = [FakeDialog(1, unread=1, read_inbox_max_id=99, top_message=101)]
    archive = FakeArchive(
        {
            1: archived_chat(
                1,
                [
                    make_message(101, chat_id=1, outgoing=False, minutes_ago=5),
                    make_message(99, chat_id=1, outgoing=False, minutes_ago=30),
                ],
                last_msg_id=101,
            )
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    chat = (await recent_activity_impl(since="24h"))["chats"][0]

    assert chat["unread_count"] == 1
    assert chat["read_inbox_max_id"] == 99
    by_id = {m["id"]: m for m in chat["messages"]}
    assert by_id[101]["unread"] is True
    assert by_id[99]["unread"] is False
    assert chat["last_incoming"]["id"] == 101
    assert chat["last_incoming"]["unread"] is True


@pytest.mark.asyncio
async def test_last_outgoing_after_last_incoming_is_reported_not_interpreted(patched):
    """The gateway states the order of events. It does not say 'no reply needed'."""
    dialogs = [FakeDialog(1, read_inbox_max_id=200, top_message=201)]
    archive = FakeArchive(
        {
            1: archived_chat(
                1,
                [
                    make_message(201, chat_id=1, outgoing=True, minutes_ago=2),
                    make_message(200, chat_id=1, outgoing=False, minutes_ago=9),
                ],
                last_msg_id=201,
            )
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    chat = (await recent_activity_impl(since="24h"))["chats"][0]

    assert chat["outgoing_after_incoming"] is True
    assert chat["last_outgoing"]["id"] == 201
    assert chat["last_incoming"]["id"] == 200
    assert "needs_reply" not in chat
    assert "action_required" not in chat


@pytest.mark.asyncio
async def test_unread_only_filters_chats(patched):
    dialogs = [
        FakeDialog(1, unread=2, read_inbox_max_id=1, top_message=5),
        FakeDialog(2, unread=0, read_inbox_max_id=9, top_message=9),
    ]
    archive = FakeArchive(
        {
            1: archived_chat(1, [make_message(5, chat_id=1)], last_msg_id=5),
            2: archived_chat(2, [make_message(9, chat_id=2)], last_msg_id=9),
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    result = await recent_activity_impl(since="24h", unread_only=True)
    assert [c["id"] for c in result["chats"]] == [1]


# ── voice and media ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ready_transcript_is_served_without_re_running_speech_to_text(patched):
    dialogs = [FakeDialog(1, top_message=50)]
    archive = FakeArchive(
        {
            1: archived_chat(
                1,
                [make_message(50, chat_id=1, text=None, media=voice_media(ready=True))],
                last_msg_id=50,
            )
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    result = await recent_activity_impl(since="24h", include_telemetry=True)
    media = result["chats"][0]["messages"][0]["media"]

    assert media["transcription_status"] == "ready"
    assert media["transcription"] == "already transcribed"
    assert result["telemetry"]["voice"]["transcribed_in_request"] == 0
    assert client.opened_chats == []


@pytest.mark.asyncio
async def test_voice_without_transcript_is_pending_not_silently_dropped(patched):
    """TG-015: the message must be present and honestly marked."""
    dialogs = [FakeDialog(1, top_message=51)]
    archive = FakeArchive(
        {
            1: archived_chat(
                1,
                [make_message(51, chat_id=1, text=None, media=voice_media(ready=False))],
                last_msg_id=51,
            )
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    result = await recent_activity_impl(since="24h")
    messages = result["chats"][0]["messages"]

    assert len(messages) == 1
    assert messages[0]["media"]["transcription_status"] == "pending"
    assert "transcription" not in messages[0]["media"]
    assert result["coverage"]["voice_pending"] == 1


@pytest.mark.asyncio
async def test_attachment_only_message_survives_without_text(patched):
    """TG-008: six spreadsheets and no caption is still six messages."""
    dialogs = [FakeDialog(1, top_message=734569)]
    archive = FakeArchive(
        {
            1: archived_chat(
                1,
                [
                    make_message(
                        734564 + n, chat_id=1, text=None,
                        media=document_media(f"prices_{n}.xlsx"), minutes_ago=10 + n,
                    )
                    for n in range(6)
                ],
                last_msg_id=734569,
            )
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    messages = (await recent_activity_impl(since="24h"))["chats"][0]["messages"]

    assert len(messages) == 6
    assert all("text" not in m for m in messages)
    assert all(m["media"]["file_name"].endswith(".xlsx") for m in messages)
    assert all(m["media"]["file_reference"] for m in messages), "must stay retrievable"


@pytest.mark.asyncio
async def test_service_message_is_typed_not_judged(patched):
    """TG-013: mark the kind, do not decide whether it matters."""
    dialogs = [FakeDialog(1, top_message=5)]
    archive = FakeArchive(
        {
            1: archived_chat(
                1,
                [make_message(5, chat_id=1, text=None, service=True)],
                last_msg_id=5,
            )
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    message = (await recent_activity_impl(since="24h"))["chats"][0]["messages"][0]
    assert message["is_service"] is True
    assert "importance" not in message


# ── noise, staleness, failures ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_channels_are_excluded_by_default_and_opt_in_works(patched):
    """TG-012: a loud feed must not bury the working chats."""
    dialogs = [
        FakeDialog(1, chat_type="private", top_message=1),
        FakeDialog(2, chat_type="channel", title="loud feed", top_message=2),
    ]
    archive = FakeArchive(
        {
            1: archived_chat(1, [make_message(1, chat_id=1)], last_msg_id=1),
            2: archived_chat(2, [make_message(2, chat_id=2)], last_msg_id=2),
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    default = await recent_activity_impl(since="24h")
    assert [c["id"] for c in default["chats"]] == [1]

    opted_in = await recent_activity_impl(since="24h", include_channels=True)
    assert {c["id"] for c in opted_in["chats"]} == {1, 2}


@pytest.mark.asyncio
async def test_stale_archive_copy_is_topped_up_from_live(patched):
    """An archived chat behind the live top message must not answer alone."""
    dialogs = [FakeDialog(1, top_message=20, read_inbox_max_id=15)]
    archive = FakeArchive(
        {1: archived_chat(1, [make_message(15, chat_id=1)], last_msg_id=15)}
    )
    live = {
        1: [
            SimpleNamespace(
                id=20, date=NOW, text="newer", message="newer", out=False,
                sender_id=8, media=None, reply_to=None, reply_to_msg_id=None,
                edit_date=None, action=None,
            )
        ]
    }
    client = FakeClient(dialogs, live_messages=live)
    patched(client, archive)

    result = await recent_activity_impl(since="24h", include_telemetry=True)
    chat = result["chats"][0]

    assert client.opened_chats == [1]
    assert {m["id"] for m in chat["messages"]} == {15, 20}
    assert chat["source"] == "mixed"
    assert result["telemetry"]["chats_stale_topped_up"] == 1


@pytest.mark.asyncio
async def test_archive_failure_degrades_to_live_instead_of_erroring(patched):
    dialogs = [FakeDialog(1, top_message=3)]
    archive = FakeArchive({1: archived_chat(1, [make_message(3, chat_id=1)])})
    archive.fail = True
    live = {
        1: [
            SimpleNamespace(
                id=3, date=NOW, text="live", message="live", out=False, sender_id=8,
                media=None, reply_to=None, reply_to_msg_id=None, edit_date=None,
                action=None,
            )
        ]
    }
    client = FakeClient(dialogs, live_messages=live)
    patched(client, archive)

    result = await recent_activity_impl(since="24h")
    assert result["coverage"]["archive_error"] == "archive_unavailable"
    assert result["chats"][0]["messages"][0]["text"] == "live"


@pytest.mark.asyncio
async def test_empty_period_returns_an_empty_snapshot_not_an_error(patched):
    client = FakeClient([])
    patched(client, None)
    result = await recent_activity_impl(since="24h")
    assert result["chats"] == []
    assert result["coverage"]["chats_discovered"] == 0
    assert "error" not in result


@pytest.mark.asyncio
async def test_second_account_is_reported_as_unavailable_not_silently_missing(patched):
    """TG-011: one session is one account; the gap must be visible."""
    dialogs = [FakeDialog(1, top_message=1)]
    archive = FakeArchive(
        {1: archived_chat(1, [make_message(1, chat_id=1)], last_msg_id=1)},
        accounts=("acct",),
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    result = await recent_activity_impl(since="24h", accounts=["acct", "other"])
    coverage = result["coverage"]
    assert coverage["accounts_unavailable_in_this_session"] == ["other"]


@pytest.mark.asyncio
async def test_invalid_limits_are_rejected(patched):
    client = FakeClient([])
    patched(client, None)
    assert "error" in await recent_activity_impl(since="24h", limit_chats=0)
    assert "error" in await recent_activity_impl(since="24h", limit_messages_per_chat=0)
    assert "error" in await recent_activity_impl(since="not a time")
    assert "error" in await recent_activity_impl(since="24h", chat_type="nonsense")


@pytest.mark.asyncio
async def test_reply_relations_are_preserved(patched):
    """TG-009: the raw chronology and reply links, without a CRM verdict."""
    dialogs = [FakeDialog(1, top_message=4)]
    archive = FakeArchive(
        {
            1: archived_chat(
                1,
                [
                    make_message(4, chat_id=1, outgoing=False, reply_to=3, minutes_ago=1),
                    make_message(3, chat_id=1, outgoing=True, reply_to=2, minutes_ago=5),
                    make_message(2, chat_id=1, outgoing=False, minutes_ago=9),
                ],
                last_msg_id=4,
            )
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    messages = (await recent_activity_impl(since="24h"))["chats"][0]["messages"]
    chain = {m["id"]: m.get("reply_to_msg_id") for m in messages}
    assert chain == {4: 3, 3: 2, 2: None}
    assert [m["id"] for m in messages] == [4, 3, 2], "newest first, real chronology"


@pytest.mark.asyncio
async def test_telemetry_carries_no_content(patched):
    dialogs = [FakeDialog(1, title="Secret Client Chat", top_message=1)]
    archive = FakeArchive(
        {
            1: archived_chat(
                1,
                [make_message(1, chat_id=1, text="confidential body text")],
                last_msg_id=1,
            )
        }
    )
    client = FakeClient(dialogs)
    patched(client, archive)

    result = await recent_activity_impl(since="24h", include_telemetry=True)
    blob = repr(result["telemetry"])
    assert "confidential" not in blob
    assert "Secret Client Chat" not in blob


# ── choosing which archived account a session may read ──────────────────────


def test_configured_label_wins_over_username_matching():
    """The two naming schemes never agree on their own.

    An archive labels accounts the way its operator thinks about them; a session
    knows itself by Telegram username. A live run with no configured label read
    the archive zero times while reporting success, and every message came from
    live Telegram instead.
    """
    from src.tools.activity.recent import _pick_archive_account

    assert _pick_archive_account(
        ["personal", "work"], [], "real_username", configured="personal"
    ) == "personal"


def test_configured_label_that_is_not_in_the_archive_reads_nothing():
    from src.tools.activity.recent import _pick_archive_account

    assert _pick_archive_account(
        ["personal", "work"], [], "someone", configured="missing"
    ) is None


def test_username_match_still_works_without_configuration():
    from src.tools.activity.recent import _pick_archive_account

    assert _pick_archive_account(["alice", "bob"], [], "alice") == "alice"


def test_single_account_archive_needs_no_configuration():
    from src.tools.activity.recent import _pick_archive_account

    assert _pick_archive_account(["only"], [], "whoever") == "only"


def test_ambiguous_archive_without_configuration_reads_nothing():
    """Guessing between two accounts would mean serving someone else's messages."""
    from src.tools.activity.recent import _pick_archive_account

    assert _pick_archive_account(["personal", "work"], [], "stranger") is None


# ── waiting that the client library hides ───────────────────────────────────


def test_a_long_read_is_recorded_as_a_stall():
    """Telethon sleeps off a short FloodWait and retries without raising.

    Measured on a real account: after roughly sixty rapid reads Telegram answered
    every request with FloodWait 29s, the exception never surfaced, and telemetry
    reported a clean run that merely looked slow. Counting elapsed time is the
    only honest signal available.
    """
    from src.tools.activity.run_telemetry import RetrievalRun

    run = RetrievalRun()
    run.note_read_duration(0.2)
    run.note_read_duration(29.4)
    record = run.to_dict()

    assert record["stalled_reads"]["count"] == 1
    assert record["stalled_reads"]["seconds"] >= 29
    assert record["slowest_live_read_seconds"] == 29.4


def test_fast_reads_report_no_stall():
    from src.tools.activity.run_telemetry import RetrievalRun

    run = RetrievalRun()
    for _ in range(10):
        run.note_read_duration(0.3)
    record = run.to_dict()

    assert "stalled_reads" not in record
    assert record["slowest_live_read_seconds"] == 0.3
