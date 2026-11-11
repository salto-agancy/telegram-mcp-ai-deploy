"""Read and leave the draft in a chat's input field. Never sends.

Telegram keeps one cloud draft per chat, and one per forum topic. If the owner
already typed something there, it is his text: it is returned as a conflict and
replaced only when the caller saw that exact version and asked to replace it.
Every save is read back from Telegram before it is reported as saved.
"""

from __future__ import annotations

import os
import time
from typing import Any

from telethon.tl import functions, types

from src.client.connection import get_connected_client
from src.tools.outbox import store
from src.tools.outbox.agent import current_agent
from src.tools.outbox.peers import PeerError, check_resolved_id, parse_peer

MAX_DRAFT_CHARS = 4096


def account_label() -> str:
    return (os.environ.get("ARCHIVE_ACCOUNT") or "").strip() or "unknown"


def _draft_view(draft: Any) -> dict | None:
    if not isinstance(draft, types.DraftMessage) or not (draft.message or "").strip():
        return None
    return {
        "text": draft.message,
        "version": store.version(draft.message),
        "date": draft.date.isoformat() if draft.date else None,
    }


async def _resolve(client, ref):
    entity = await client.get_input_entity(ref.telethon_target())
    # The async variant resolves InputPeerSelf ('me'); telethon.utils.get_peer_id cannot.
    peer_id = await client.get_peer_id(entity)
    check_resolved_id(peer_id)
    return entity, peer_id


async def _read_draft(
    client, entity, topic_id: int | None
) -> tuple[dict | None, str | None]:
    """(draft, chat title). A forum topic keeps its own draft on the topic object."""
    if topic_id:
        res = await client(
            functions.messages.GetForumTopicsByIDRequest(peer=entity, topics=[topic_id])
        )
        topic = next(
            (t for t in res.topics if getattr(t, "id", None) == topic_id), None
        )
        if topic is None:
            raise PeerError(f"topic {topic_id} not found in this chat")
        title = getattr(topic, "title", None)
        return _draft_view(getattr(topic, "draft", None)), title
    res = await client(
        functions.messages.GetPeerDialogsRequest(peers=[types.InputDialogPeer(entity)])
    )
    if not res.dialogs:
        return None, None
    title = None
    for coll in (res.users, res.chats):
        for e in coll:
            title = (
                title
                or getattr(e, "title", None)
                or " ".join(
                    x
                    for x in (
                        getattr(e, "first_name", None),
                        getattr(e, "last_name", None),
                    )
                    if x
                )
                or None
            )
    return _draft_view(res.dialogs[0].draft), title


async def draft_get_impl(chat: str, topic_id: int | None = None) -> dict[str, Any]:
    try:
        ref = parse_peer(chat)
    except PeerError as e:
        return {"ok": False, "error": str(e)}
    client = await get_connected_client()
    try:
        entity, peer_id = await _resolve(client, ref)
        draft, title = await _read_draft(client, entity, topic_id)
    except PeerError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "account": account_label(),
        "chat": ref.text,
        "peer_id": peer_id,
        "title": title,
        "topic_id": topic_id,
        "draft": draft,
        "as_of": store.now_iso(),
        "source": "Telegram, read now",
    }


async def draft_save_impl(
    chat: str,
    text: str,
    topic_id: int | None = None,
    if_version: str | None = None,
    replace: bool = False,
    operation_key: str | None = None,
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {
            "ok": False,
            "error": "an empty draft is not saved; only the owner clears the input field",
        }
    if len(text) > MAX_DRAFT_CHARS:
        return {
            "ok": False,
            "error": f"draft is longer than {MAX_DRAFT_CHARS} characters; Telegram will not accept it",
        }
    try:
        ref = parse_peer(chat)
    except PeerError as e:
        return {"ok": False, "error": str(e)}
    account, agent = account_label(), current_agent()

    for prior in await store.find_operation(account, operation_key):
        if prior["kind"] == "draft":
            return {
                "ok": True,
                "repeat": True,
                "status": prior["status"],
                "outbox_id": prior["id"],
                "note": "this operation already ran; the retry wrote nothing",
            }

    client = await get_connected_client()
    try:
        entity, peer_id = await _resolve(client, ref)
        current, title = await _read_draft(client, entity, topic_id)
    except PeerError as e:
        return {"ok": False, "error": str(e)}

    base = {
        "account": account,
        "agent": agent,
        "operation_key": operation_key,
        "kind": "draft",
        "peer": ref.text,
        "peer_id": peer_id,
        "peer_title": title,
        "topic_id": topic_id,
        "text_agent": text,
        "seq": 0,
    }

    if current and current["text"] == text:
        oid = await store.record(
            {
                **base,
                "status": "already_there",
                "text_now": text,
                "version_now": current["version"],
                "edit_ratio": 0.0,
            }
        )
        return {
            "ok": True,
            "status": "already_there",
            "outbox_id": oid,
            "draft": current,
            "account": account,
            "chat": ref.text,
            "title": title,
            "topic_id": topic_id,
        }

    if current:
        # Only a draft an agent left may be replaced; the owner's own text never is,
        # whatever the arguments (owner rule: "keep my text and show the conflict").
        agent_wrote_it = await store.agent_wrote(
            account, peer_id, topic_id, current["text"]
        )
        if not (agent_wrote_it and replace and if_version == current["version"]):
            oid = await store.record(
                {
                    **base,
                    "status": "conflict",
                    "text_now": current["text"],
                    "version_now": current["version"],
                }
            )
            return {
                "ok": True,
                "status": "conflict",
                "outbox_id": oid,
                "saved": False,
                "account": account,
                "chat": ref.text,
                "title": title,
                "topic_id": topic_id,
                "existing_draft": current,
                "existing_is": "agent_draft" if agent_wrote_it else "owner_text",
                "how_to_resolve": (
                    "the field holds the owner's own text: it is never replaced by a tool. "
                    "Show both texts to the owner; he edits the field himself."
                    if not agent_wrote_it
                    else "the field holds an earlier agent draft; to replace it repeat with "
                    f"replace=true and if_version='{current['version']}'"
                ),
            }

    reply_to = (
        types.InputReplyToMessage(reply_to_msg_id=topic_id, top_msg_id=topic_id)
        if topic_id
        else None
    )
    t0 = time.perf_counter()
    await client(
        functions.messages.SaveDraftRequest(
            peer=entity, message=text, reply_to=reply_to
        )
    )
    write_ms = round((time.perf_counter() - t0) * 1000)
    t1 = time.perf_counter()
    after, _ = await _read_draft(client, entity, topic_id)
    read_ms = round((time.perf_counter() - t1) * 1000)
    equal = bool(after and after["text"] == text)
    oid = await store.record(
        {
            **base,
            "status": "saved" if equal else "readback_mismatch",
            "text_now": after["text"] if after else None,
            "version_now": after["version"] if after else None,
            "edit_ratio": 0.0 if equal else None,
        },
        {"replaced": current, "write_ms": write_ms, "read_ms": read_ms},
    )
    if equal:
        await store.supersede_open_drafts(account, peer_id, topic_id, oid)
    return {
        "ok": equal,
        "status": "saved" if equal else "readback_mismatch",
        "outbox_id": oid,
        "account": account,
        "chat": ref.text,
        "title": title,
        "topic_id": topic_id,
        "draft": after,
        "replaced": current,
        "readback": {"equal": equal, "write_ms": write_ms, "read_ms": read_ms},
        "sends_itself": False,
    }
