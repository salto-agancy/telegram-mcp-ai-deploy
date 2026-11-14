"""Coverage regression, audit 13.11.2026.

ChatGPT asked recent_activity for 7 days with include_archived_dialogs=true and got
2 private chats, while find_chats listed 47 for the same week. The flag was passed to
Telethon as archived=True, which means ONLY the archive folder: asking for "also the
archive" silently dropped the whole main list. The answer did not say anything was
left out. These tests pin the meaning of the flag and the coverage counts.
"""

import pytest

from src.tools.activity.dialog_state import scan_dialogs
from src.tools.activity.recent import recent_activity_impl
from tests.unit.activity.conftest import FakeClient, FakeDialog, iso


@pytest.fixture
def patched(monkeypatch):
    def _apply(client):
        async def fake_client():
            return client

        monkeypatch.setattr(
            "src.tools.activity.recent.get_connected_client", fake_client
        )
        monkeypatch.setattr(
            "src.tools.activity.recent.get_archive_backend", lambda: None
        )

    return _apply


def _week():
    return [
        FakeDialog(1, minutes_ago=10),
        FakeDialog(2, minutes_ago=60),
        FakeDialog(3, minutes_ago=120, archived=True),
        FakeDialog(4, minutes_ago=30, chat_type="channel", title="feed"),
    ]


async def test_include_archived_adds_the_archive_instead_of_replacing_the_main_list():
    scan = await scan_dialogs(
        FakeClient(_week()), since_iso=iso(60 * 24 * 7), include_archived=True
    )
    assert sorted(d.chat_id for d in scan.dialogs) == [1, 2, 3, 4]


async def test_default_leaves_the_archive_out_but_counts_it():
    scan = await scan_dialogs(FakeClient(_week()), since_iso=iso(60 * 24 * 7))
    assert sorted(d.chat_id for d in scan.dialogs) == [1, 2, 4]
    assert scan.excluded_archived == 1


async def test_recent_activity_with_archive_returns_main_list_too(patched):
    patched(FakeClient(_week()))
    res = await recent_activity_impl(since="7d", include_archived_dialogs=True)
    assert sorted(c["id"] for c in res["chats"]) == [1, 2, 3]
    assert res["coverage"]["excluded"]["channels"] == 1


async def test_coverage_names_what_was_found_read_and_excluded(patched):
    patched(FakeClient(_week()))
    res = await recent_activity_impl(since="7d")
    cov = res["coverage"]
    assert cov["chats_found"] == 4
    assert cov["chats_returned"] == 2
    assert cov["excluded"] == {"archived_folder": 1, "channels": 1}
    assert cov["partial"] is False
    assert "include_archived_dialogs" in cov["how_to_include"]["archived_folder"]


async def test_cut_by_limit_is_partial_and_says_why(patched):
    patched(FakeClient([FakeDialog(i, minutes_ago=i) for i in range(1, 6)]))
    res = await recent_activity_impl(since="7d", limit_chats=2)
    cov = res["coverage"]
    assert len(res["chats"]) == 2
    assert cov["partial"] is True
    assert cov["excluded"]["over_limit_chats"] == 3
    assert "limit_chats" in cov["partial_reasons"][0]
