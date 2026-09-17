"""Merging live Telegram search with archive search.

The archive contributes what Telegram cannot index: what a voice message said and
what a screenshot showed. Live search contributes chats the archive does not cover.
Neither side may silently replace the other, and the same message must not appear
twice.
"""

from src.tools.search.archive_search import merge_search_results


def _live(msg_id: int, chat_id: int, text: str = "live text") -> dict:
    return {"id": msg_id, "chat": {"id": chat_id}, "text": text}


def _archived(msg_id: int, chat_id: int, **extra) -> dict:
    base = {"id": msg_id, "chat_id": chat_id, "source": "archive"}
    base.update(extra)
    return base


def test_archive_only_hit_is_added():
    """A phrase that exists only inside a voice transcript must come back."""
    merged, stats = merge_search_results(
        [_live(1, 10)],
        [_archived(2, 10, matched_in=["voice_transcription"])],
        limit=10,
    )
    ids = [m["id"] for m in merged]
    assert ids == [1, 2]
    assert stats["archive_only"] == 1
    assert merged[1]["matched_in"] == ["voice_transcription"]


def test_same_message_is_not_duplicated_and_keeps_the_richer_copy():
    merged, _stats = merge_search_results(
        [_live(5, 10, text="written text")],
        [_archived(5, 10, text="written text", matched_in=["text"],
                   media={"transcription": "spoken"})],
        limit=10,
    )
    assert len(merged) == 1
    assert merged[0]["source"] == "both"
    assert merged[0]["media"]["transcription"] == "spoken"


def test_live_only_hits_survive_because_the_archive_is_partial():
    merged, _ = merge_search_results([_live(7, 99)], [], limit=10)
    assert [m["id"] for m in merged] == [7]
    assert merged[0]["source"] == "live"


def test_live_results_keep_their_order_ahead_of_archive_extras():
    merged, _ = merge_search_results(
        [_live(1, 10), _live(2, 10)],
        [_archived(3, 10), _archived(4, 11)],
        limit=10,
    )
    assert [m["id"] for m in merged] == [1, 2, 3, 4]


def test_limit_is_respected():
    merged, stats = merge_search_results(
        [_live(i, 10) for i in range(5)],
        [_archived(100 + i, 10) for i in range(5)],
        limit=3,
    )
    assert len(merged) == 3
    assert stats["returned"] == 3


def test_messages_from_different_chats_with_equal_ids_stay_separate():
    """Telegram message ids are per chat; collapsing them would lose a result."""
    merged, _ = merge_search_results(
        [_live(42, 10)], [_archived(42, 11)], limit=10
    )
    assert len(merged) == 2


# ── neither channel may crowd the other out ─────────────────────────────────


def test_archive_only_hit_survives_a_full_page_of_live_results():
    """The failure this rule exists for, reproduced from production.

    A search for a phrase spoken in a voice message returned ten live matches and
    dropped the one message that actually contained the phrase: archive-only hits
    were appended after every live result and cut off by the limit. Live search
    cannot answer that question at all — Telegram indexes message text and nothing
    else — so the only useful result was the one discarded.
    """
    live = [_live(i, 10) for i in range(10)]
    archived = [_archived(732337, 11, matched_in=["voice_transcription"])]

    merged, stats = merge_search_results(live, archived, limit=10)

    ids = [m["id"] for m in merged]
    assert 732337 in ids, "the only message answering the query was dropped"
    assert len(merged) == 10
    assert stats["archive_only"] == 1


def test_live_keeps_the_head_of_the_answer():
    """Telegram's own relevance is good for text, so live stays first."""
    live = [_live(i, 10) for i in range(10)]
    archived = [_archived(900 + i, 11) for i in range(5)]

    merged, _ = merge_search_results(live, archived, limit=9)
    ids = [m["id"] for m in merged]

    assert ids[0] == 0, "live result should lead"
    assert any(i >= 900 for i in ids), "archive-only hits must still appear"


def test_reservation_never_shrinks_the_answer():
    """A reserved share must not leave empty slots when one side has few hits."""
    live = [_live(i, 10) for i in range(10)]
    merged, _ = merge_search_results(live, [], limit=10)
    assert len(merged) == 10

    archived = [_archived(900 + i, 11) for i in range(10)]
    merged, _ = merge_search_results([], archived, limit=10)
    assert len(merged) == 10


def test_many_archive_hits_do_not_push_live_out_entirely():
    live = [_live(i, 10) for i in range(10)]
    archived = [_archived(900 + i, 11) for i in range(50)]

    merged, _ = merge_search_results(live, archived, limit=12)
    ids = [m["id"] for m in merged]

    assert sum(1 for i in ids if i < 10) >= 8, "live must keep most of its share"
    assert sum(1 for i in ids if i >= 900) >= 4, "archive must keep its reservation"


def test_a_small_limit_still_leaves_room_for_the_archive():
    """With limit 3 the reservation must not round down to zero."""
    live = [_live(i, 10) for i in range(3)]
    archived = [_archived(732337, 11, matched_in=["voice_transcription"])]

    merged, _ = merge_search_results(live, archived, limit=3)
    assert 732337 in [m["id"] for m in merged]
