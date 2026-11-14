"""send_message: sends now, one message or a series, idempotent, explicit per-message state.

With TELEGRAM_INSTANT_SEND=false (owner, 10.11.2026) only Saved Messages and the
owner's own test chats receive a real send; anyone else gets a draft or a clear
refusal — never a silent absence of the tool.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions, types

from src.tools.outbox import send, store

ME = 42


class FakeTg:
    """Telethon stand-in: resolves typed refs, records every SendMessageRequest."""

    def __init__(self, entity=None, peer_id=ME, fail=None):
        self.entity = (
            entity if entity is not None else SimpleNamespace(id=ME, first_name="Me")
        )
        self.peer_id = peer_id
        self.sent: list[functions.messages.SendMessageRequest] = []
        self.fail = list(fail or [])  # exceptions raised by the next sends, None = ok
        self.resolve_error: Exception | None = None
        self._next_id = 100

    async def get_input_entity(self, target):
        if self.resolve_error:
            raise self.resolve_error
        return (
            types.InputPeerSelf()
            if target == "me"
            else types.InputPeerUser(self.peer_id, 0)
        )

    async def get_peer_id(self, entity):
        return ME if isinstance(entity, types.InputPeerSelf) else self.peer_id

    async def get_me(self):
        return SimpleNamespace(id=ME)

    async def get_entity(self, entity):
        return self.entity

    async def __call__(self, request):
        assert isinstance(request, functions.messages.SendMessageRequest)
        if self.fail:
            err = self.fail.pop(0)
            if err is not None:
                raise err
        self.sent.append(request)
        self._next_id += 1
        return types.UpdateShortSentMessage(
            out=True, id=self._next_id, pts=1, pts_count=1, date=None
        )


@pytest.fixture
def tg(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTBOX_DB", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("ARCHIVE_ACCOUNT", "personal")
    monkeypatch.setenv("TELEGRAM_INSTANT_SEND", "false")
    monkeypatch.setenv("ENABLE_TEST_FIXTURES", "true")
    monkeypatch.setattr(send, "current_agent", lambda: "chatgpt")
    fake = FakeTg()
    monkeypatch.setattr(send, "get_connected_client", AsyncMock(return_value=fake))
    return fake


# --- what may be sent while instant send is off -----------------------------------


async def test_saved_messages_are_sent_while_instant_send_is_off(tg):
    res = await send.send_message_now("me", "[SALTO test] one", operation_key="k1")
    assert res["ok"] is True and res["status"] == "sent"
    assert res["allowed_as"] == "saved_messages"
    assert len(tg.sent) == 1 and res["messages"][0]["message_id"] == 101
    assert res["messages"][0]["state"] == "sent"


async def test_third_party_gets_a_draft_and_nothing_is_sent(tg, monkeypatch):
    tg.peer_id = 100000001
    tg.entity = SimpleNamespace(id=100000001, first_name="Someone")
    draft = AsyncMock(return_value={"ok": True, "status": "saved", "saved": True})
    monkeypatch.setattr(send, "draft_save_impl", draft)
    res = await send.send_message_now("user:100000001", "привет")
    assert tg.sent == []
    assert res["status"] == "draft" and res["reason"] == "instant_send_disabled"
    assert res["sent"] is False
    draft.assert_awaited_once()


async def test_series_to_third_party_becomes_ordered_parking(tg, monkeypatch):
    """Owner, 14.11.2026: a series to a person is an ordered set of scheduled messages."""
    from src.tools.outbox import batch

    tg.peer_id = 100000001
    tg.entity = SimpleNamespace(id=100000001, first_name="Someone")
    draft = AsyncMock()
    apply = AsyncMock(
        return_value={"ok": True, "status": "started", "summary": {"with_issues": 0}}
    )
    monkeypatch.setattr(send, "draft_save_impl", draft)
    monkeypatch.setattr(batch, "schedule_batch_impl", apply)
    res = await send.send_message_now("user:100000001", "a", more_messages=["b"])
    assert tg.sent == [] and draft.await_count == 0
    assert res["status"] == "scheduled" and res["scheduled_as"] == "parking"
    assert res["reason"] == "instant_send_disabled" and res["sent"] is False
    mode, items, key = apply.await_args.args
    assert mode == "apply" and [i["text"] for i in items] == ["a", "b"]
    assert all(i["to"] == "user:100000001" and "at_msk" not in i for i in items)
    await send.send_message_now("user:100000001", "a", more_messages=["b"])
    assert apply.await_args.args[2] == key, "a retry reuses the operation key"


async def test_series_cannot_be_one_draft(tg, monkeypatch):
    tg.peer_id = 100000001
    tg.entity = SimpleNamespace(id=100000001, first_name="Someone")
    monkeypatch.setattr(send, "draft_save_impl", AsyncMock())
    res = await send.send_message_now(
        "user:100000001", "a", more_messages=["b"], fallback="draft"
    )
    assert res["status"] == "not_sent" and "one draft per chat" in res["next"]


async def test_third_party_is_sent_when_instant_send_is_on(tg, monkeypatch):
    monkeypatch.setenv("TELEGRAM_INSTANT_SEND", "true")
    tg.peer_id = 100000001
    tg.entity = SimpleNamespace(id=100000001, first_name="Someone")
    res = await send.send_message_now("user:100000001", "привет", operation_key="k")
    assert res["status"] == "sent" and res["allowed_as"] == "instant_send"
    assert len(tg.sent) == 1


async def test_own_test_chat_is_sent(tg):
    tg.peer_id = -5001
    tg.entity = types.Chat(
        id=5001,
        title="MCP-тест-01",
        photo=types.ChatPhotoEmpty(),
        participants_count=2,
        date=None,
        version=1,
        creator=True,
    )
    res = await send.send_message_now("chat:5001", "x", operation_key="k")
    assert res["status"] == "sent" and res["allowed_as"] == "test_chat"


async def test_a_group_merely_named_like_a_test_chat_is_not(tg, monkeypatch):
    """Anyone can name a group 'MCP-тест-01' and add the owner; creator is the proof."""
    tg.peer_id = -5002
    tg.entity = types.Chat(
        id=5002,
        title="MCP-тест-01",
        photo=types.ChatPhotoEmpty(),
        participants_count=2,
        date=None,
        version=1,
        creator=False,
    )
    monkeypatch.setattr(
        send, "draft_save_impl", AsyncMock(return_value={"ok": True, "status": "saved"})
    )
    res = await send.send_message_now("chat:5002", "x")
    assert tg.sent == [] and res["status"] == "draft"


# --- series, order, idempotency ----------------------------------------------------


async def test_two_messages_are_two_sends_in_dictated_order(tg):
    res = await send.send_message_now(
        "me", "первое", more_messages=["второе"], operation_key="k2"
    )
    assert [r.message for r in tg.sent] == ["первое", "второе"]
    assert [m["seq"] for m in res["messages"]] == [0, 1]
    assert [m["message_id"] for m in res["messages"]] == [101, 102]
    assert res["summary"] == {"sent": 2}


async def test_retry_after_partial_failure_sends_only_the_missing_part(tg):
    tg.fail = [None, FloodWaitError(request=None, capture=37)]
    first = await send.send_message_now(
        "me", "a", more_messages=["b", "c"], operation_key="k3"
    )
    assert first["status"] == "partial" and first["ok"] is False
    states = [m["state"] for m in first["messages"]]
    assert states == ["sent", "failed", "not_attempted"]
    assert first["messages"][1]["error_code"] == "flood_wait"
    assert first["messages"][1]["retry_after"] == 37

    again = await send.send_message_now(
        "me", "a", more_messages=["b", "c"], operation_key="k3"
    )
    assert [r.message for r in tg.sent] == ["a", "b", "c"], "a must not be sent twice"
    assert again["status"] == "sent"
    assert again["messages"][0]["repeat"] is True


async def test_same_random_id_on_retry_so_telegram_can_refuse_a_duplicate(tg):
    tg.fail = [TimeoutError()]
    lost = await send.send_message_now("me", "a", operation_key="k4")
    assert lost["messages"][0]["state"] == "unknown"
    await send.send_message_now("me", "a", operation_key="k4")
    # the first attempt raised before recording; the retry used the same random_id
    assert len(tg.sent) == 1
    assert tg.sent[0].random_id == send.random_id("personal", "k4", 0)


async def test_random_id_duplicate_means_it_was_delivered(tg):
    tg.fail = [RPCError(request=None, message="RANDOM_ID_DUPLICATE", code=500)]
    res = await send.send_message_now("me", "a", operation_key="k5")
    assert res["status"] == "sent" and res["messages"][0]["state"] == "sent"
    assert res["messages"][0].get("delivered_earlier") is True


async def test_without_key_an_immediate_retry_does_not_resend(tg):
    first = await send.send_message_now("me", "один раз")
    second = await send.send_message_now("me", "один раз")
    assert len(tg.sent) == 1
    assert (
        first["key_source"] == "derived"
        and second["operation_key"] == first["operation_key"]
    )
    assert second["messages"][0]["repeat"] is True


async def test_key_reused_for_another_text_is_refused(tg):
    await send.send_message_now("me", "a", operation_key="k6")
    res = await send.send_message_now("me", "другой", operation_key="k6")
    assert res["ok"] is False and res["error_code"] == "operation_key_conflict"
    assert len(tg.sent) == 1


# --- error reasons -------------------------------------------------------------------


async def test_unknown_peer_is_peer_not_found(tg):
    tg.resolve_error = ValueError(
        'Cannot find any entity corresponding to "nobody_here"'
    )
    res = await send.send_message_now("@nobody_here", "x")
    assert res["ok"] is False and res["error_code"] == "peer_not_found"
    assert tg.sent == []


async def test_bare_number_is_refused_as_ambiguous(tg):
    res = await send.send_message_now("123456", "x")
    assert res["ok"] is False and res["error_code"] == "invalid_peer"
    assert tg.sent == []


async def test_write_forbidden_is_permission_denied(tg, monkeypatch):
    monkeypatch.setenv("TELEGRAM_INSTANT_SEND", "true")
    tg.peer_id = 100000001
    tg.fail = [RPCError(request=None, message="CHAT_WRITE_FORBIDDEN", code=403)]
    res = await send.send_message_now("user:100000001", "x", operation_key="k7")
    assert res["status"] == "failed"
    assert res["messages"][0]["error_code"] == "permission_denied"


# --- links -------------------------------------------------------------------------


def test_links_for_public_and_private_channels_none_for_dm():
    pub = SimpleNamespace(id=1234, username="salto", broadcast=True)
    priv = types.Channel(
        id=1234, title="t", photo=types.ChatPhotoEmpty(), date=None, megagroup=True
    )
    assert send.message_link(pub, 7) == "https://t.me/salto/7"
    assert send.message_link(priv, 7) == "https://t.me/c/1234/7"
    assert send.message_link(SimpleNamespace(id=1, first_name="A"), 7) is None


# --- registration ------------------------------------------------------------------


async def test_send_message_is_registered_when_instant_send_is_off(monkeypatch):
    """The tool stays visible so the agent gets an answer instead of not finding it;
    the other write-now tools stay off."""
    from fastmcp import FastMCP

    from src.server_components import tools_register as tr

    monkeypatch.setenv("TELEGRAM_INSTANT_SEND", "false")
    mcp = FastMCP("t")
    tr.register_tools(mcp)
    names = {t.name for t in await mcp.list_tools()}
    assert "send_message" in names
    assert not names & {
        "send_rich_message",
        "send_message_to_phone",
        "edit_message",
        "set_voice_caption",
    }


async def test_journal_keeps_the_state_of_every_message(tg):
    await send.send_message_now("me", "a", more_messages=["b"], operation_key="k8")

    def rows(conn):
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT seq, status, tg_msg_id FROM sends WHERE operation_key='k8' ORDER BY seq"
            )
        ]

    assert await store.call(rows) == [(0, "sent", 101), (1, "sent", 102)]
