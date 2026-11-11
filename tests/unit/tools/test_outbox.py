"""Drafts never send, never overwrite the owner's text, and are journaled with the agent."""

from unittest.mock import AsyncMock

import pytest

from src.tools import mtproto
from src.tools.outbox import drafts, peers, store

# --- recipients -------------------------------------------------------------


@pytest.mark.parametrize(
    "ref,target",
    [
        ("me", "me"),
        ("user:100000001", 100000001),
        ("chat:200000002", -200000002),
        ("channel:300000003", -100300000003),
        ("@example_user", "example_user"),
        ("https://t.me/example_user", "example_user"),
    ],
)
def test_typed_peer_resolves_to_one_target(ref, target):
    assert peers.parse_peer(ref).telethon_target() == target


@pytest.mark.parametrize("ref", ["200000002", "-200000002", "", "Андрей", "user:abc"])
def test_bare_or_untyped_peer_is_refused(ref):
    with pytest.raises(peers.PeerError):
        peers.parse_peer(ref)


@pytest.mark.parametrize("ref", ["user:777000", "@BotFather", "@spambot"])
def test_service_chats_are_closed(ref):
    with pytest.raises(peers.PeerError):
        peers.parse_peer(ref)


def test_resolved_service_account_is_refused():
    with pytest.raises(peers.PeerError):
        peers.check_resolved_id(777000)


# --- versions ----------------------------------------------------------------


def test_edit_ratio_bounds():
    assert store.edit_ratio("abc", "abc") == 0.0
    assert store.edit_ratio("abc", "xyz") == 1.0
    assert 0 < store.edit_ratio("в четверг в 20:00", "в четверг в 22:00") < 0.2


# --- raw MTProto is read-only --------------------------------------------------


@pytest.mark.parametrize(
    "method,ok",
    [
        ("messages.GetScheduledHistory", True),
        ("messages.GetAllDrafts", True),
        ("contacts.ResolveUsername", True),
        ("messages.SendMessage", False),
        ("messages.SaveDraft", False),
        ("messages.EditMessage", False),
        ("messages.DeleteScheduledMessages", False),
        ("messages.ReadHistory", False),
        ("messages.GetBotCallbackAnswer", False),
        ("auth.GetPassword", False),
    ],
)
def test_read_method_gate(method, ok):
    assert mtproto.is_read_method(method) is ok


@pytest.mark.asyncio
async def test_invoke_mtproto_refuses_write_in_read_only_mode(monkeypatch):
    monkeypatch.setenv("MTPROTO_READ_ONLY", "true")
    res = await mtproto.invoke_mtproto_impl(
        "messages.SendMessage", '{"peer": "me", "message": "x"}'
    )
    assert res.get("ok") is False and "read-only" in res["error"]


# --- drafts --------------------------------------------------------------------


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTBOX_DB", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("ARCHIVE_ACCOUNT", "personal")
    monkeypatch.setattr(drafts, "current_agent", lambda: "chatgpt")
    client = AsyncMock()
    monkeypatch.setattr(drafts, "get_connected_client", AsyncMock(return_value=client))
    monkeypatch.setattr(
        drafts, "_resolve", AsyncMock(return_value=(object(), 100000001))
    )
    return client


def _draft(text):
    return (
        {"text": text, "version": store.version(text), "date": None} if text else None
    )


@pytest.mark.asyncio
async def test_owner_text_is_a_conflict_not_overwritten(outbox, monkeypatch):
    monkeypatch.setattr(
        drafts, "_read_draft", AsyncMock(return_value=(_draft("мой текст"), "Contact"))
    )
    res = await drafts.draft_save_impl("user:100000001", "текст агента")
    assert res["status"] == "conflict" and res["saved"] is False
    assert res["existing_draft"]["text"] == "мой текст"
    outbox.assert_not_called()  # SaveDraft never sent to Telegram


@pytest.mark.asyncio
async def test_owner_text_is_never_replaced_even_with_its_version(outbox, monkeypatch):
    """Live incident 11.11.2026: a test prompt told the agent to replace on conflict and
    the owner's real draft was overwritten. The owner's text is never replaced now."""
    owner = _draft("мой настоящий черновик")
    monkeypatch.setattr(
        drafts, "_read_draft", AsyncMock(return_value=(owner, "Contact"))
    )
    res = await drafts.draft_save_impl(
        "user:100000001", "текст агента", replace=True, if_version=owner["version"]
    )
    assert res["status"] == "conflict" and res["existing_is"] == "owner_text"
    outbox.assert_not_called()


@pytest.mark.asyncio
async def test_replace_needs_the_version_that_was_read(outbox, monkeypatch):
    monkeypatch.setattr(
        drafts, "_read_draft", AsyncMock(return_value=(_draft("мой текст"), "Contact"))
    )
    res = await drafts.draft_save_impl(
        "user:100000001", "новый", replace=True, if_version="stale"
    )
    assert res["status"] == "conflict"
    outbox.assert_not_called()


@pytest.mark.asyncio
async def test_empty_field_is_saved_read_back_and_journaled(outbox, monkeypatch):
    reads = AsyncMock(side_effect=[(None, "Contact"), (_draft("итоги"), "Contact")])
    monkeypatch.setattr(drafts, "_read_draft", reads)
    res = await drafts.draft_save_impl("user:100000001", "итоги", operation_key="op-1")
    assert (
        res["status"] == "saved"
        and res["readback"]["equal"]
        and res["sends_itself"] is False
    )
    outbox.assert_awaited_once()  # exactly one SaveDraft
    rows = await store.find_operation("personal", "op-1")
    assert (
        len(rows) == 1
        and rows[0]["agent"] == "chatgpt"
        and rows[0]["text_agent"] == "итоги"
    )


@pytest.mark.asyncio
async def test_retry_with_same_operation_key_writes_nothing(outbox, monkeypatch):
    reads = AsyncMock(side_effect=[(None, "Contact"), (_draft("итоги"), "Contact")])
    monkeypatch.setattr(drafts, "_read_draft", reads)
    await drafts.draft_save_impl("user:100000001", "итоги", operation_key="op-2")
    again = await drafts.draft_save_impl(
        "user:100000001", "итоги", operation_key="op-2"
    )
    assert again["repeat"] is True
    assert outbox.await_count == 1
    assert len(await store.find_operation("personal", "op-2")) == 1


@pytest.mark.asyncio
async def test_bare_number_never_reaches_telegram(outbox):
    res = await drafts.draft_save_impl("200000002", "текст")
    assert res["ok"] is False
    outbox.assert_not_called()


# --- instant send can be switched off -------------------------------------------


def test_instant_send_switch(monkeypatch):
    from src.server_components import tools_register as tr

    monkeypatch.setenv("TELEGRAM_INSTANT_SEND", "false")
    assert tr.instant_send_enabled() is False
    monkeypatch.delenv("TELEGRAM_INSTANT_SEND")
    assert tr.instant_send_enabled() is True


@pytest.mark.asyncio
async def test_send_tools_are_not_registered_when_switched_off(monkeypatch):
    from fastmcp import FastMCP

    from src.server_components import tools_register as tr

    monkeypatch.setenv("TELEGRAM_INSTANT_SEND", "false")
    mcp = FastMCP("t")
    tr.register_tools(mcp)
    names = {t.name for t in await mcp.list_tools()}
    assert {"draft_get", "draft_save", "outbox_report"} <= names
    assert not names & {
        "send_message",
        "send_rich_message",
        "send_message_to_phone",
        "edit_message",
        "set_voice_caption",
    }


@pytest.mark.asyncio
async def test_saved_messages_resolve_to_own_id():
    """Regression (live, 11.11.2026): 'me' resolves to InputPeerSelf, which the sync
    telethon.utils.get_peer_id cannot turn into an id."""
    from telethon.tl import types

    client = AsyncMock()
    client.get_input_entity.return_value = types.InputPeerSelf()
    client.get_peer_id.return_value = 42
    entity, peer_id = await drafts._resolve(client, peers.parse_peer("me"))
    assert isinstance(entity, types.InputPeerSelf) and peer_id == 42


@pytest.mark.asyncio
async def test_agent_replacing_its_own_draft_is_not_an_owner_edit(outbox, monkeypatch):
    first = AsyncMock(side_effect=[(None, "Contact"), (_draft("v1"), "Contact")])
    monkeypatch.setattr(drafts, "_read_draft", first)
    r1 = await drafts.draft_save_impl("user:100000001", "v1")
    v1 = r1["draft"]["version"]
    second = AsyncMock(
        side_effect=[(_draft("v1"), "Contact"), (_draft("v2"), "Contact")]
    )
    monkeypatch.setattr(drafts, "_read_draft", second)
    await drafts.draft_save_impl("user:100000001", "v2", replace=True, if_version=v1)

    def status_of(conn, oid):
        return conn.execute(
            "SELECT status FROM outbox WHERE id = ?", (oid,)
        ).fetchone()[0]

    assert await store.call(status_of, r1["outbox_id"]) == "superseded"
