"""find_chats by dates must say when it stopped at `limit` (audit 13.11.2026)."""

import pytest

from src.tools.chat_discovery import find_chats as fc


def _fake_dialogs(n):
    async def gen(query, limit, chat_type, public, min_dt, max_dt, folder_id):
        for i in range(min(n, limit)):
            yield {"id": i, "title": f"c{i}", "type": "private"}

    return gen


@pytest.mark.asyncio
async def test_hitting_the_limit_is_reported_as_partial(monkeypatch):
    monkeypatch.setattr(fc, "search_dialogs_impl", _fake_dialogs(30))
    res = await fc.find_chats_impl(min_date="2026-11-06", limit=20)
    assert len(res["chats"]) == 20
    assert res["coverage"]["partial"] is True
    assert res["coverage"]["more_available"] is True


@pytest.mark.asyncio
async def test_everything_found_is_not_partial(monkeypatch):
    monkeypatch.setattr(fc, "search_dialogs_impl", _fake_dialogs(5))
    res = await fc.find_chats_impl(min_date="2026-11-06", limit=20)
    assert len(res["chats"]) == 5
    assert res["coverage"] == {
        "returned": 5,
        "limit": 20,
        "more_available": False,
        "partial": False,
        "folders": "all (main list and archive)",
    }
