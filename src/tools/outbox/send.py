"""send_message: send NOW — one message or a series — and say what happened to each.

Owner's decision (10.11.2026): agents do not send to people themselves; they leave a
draft or a scheduled message. TELEGRAM_INSTANT_SEND=false enforces that. The tool
stays visible anyway, because an absent tool made the agent improvise; instead:

- Saved Messages of this account and the owner's own test chats are sent;
- any other chat gets a draft (one message) or, for a series, ordered parking in the
  scheduled queue (owner, 14.11.2026) — with an explicit status either way.

Each message of a series is journaled (``sends`` table) under an operation key, and
its Telegram random_id is derived from that key: a retry after a timeout or a partial
failure skips what was delivered, and Telegram itself refuses a second copy of a
message whose first attempt did arrive (RANDOM_ID_DUPLICATE).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
from typing import Any

from telethon.errors import FloodWaitError, RPCError
from telethon.extensions import html as tl_html
from telethon.extensions import markdown as tl_markdown
from telethon.tl import functions, types

from src.client.connection import get_connected_client
from src.tools.messages.core import _normalize_parse_mode, detect_message_formatting
from src.tools.outbox import store
from src.tools.outbox.agent import current_agent
from src.tools.outbox.drafts import account_label, draft_save_impl
from src.tools.outbox.fixtures import FIXTURE_PREFIX, fixtures_enabled
from src.tools.outbox.peers import PeerError, check_resolved_id, parse_peer

logger = logging.getLogger(__name__)

MAX_SERIES = 20
MAX_CHARS = 4096
# Without a client key, the same text to the same chat within this window is a retry.
DERIVED_KEY_WINDOW = dt.timedelta(minutes=10)

_PEER_NOT_FOUND = {
    "PEER_ID_INVALID",
    "CHAT_ID_INVALID",
    "CHANNEL_INVALID",
    "USER_ID_INVALID",
    "INPUT_USER_DEACTIVATED",
    "USERNAME_NOT_OCCUPIED",
    "USERNAME_INVALID",
}
_PERMISSION_DENIED = {
    "CHANNEL_PRIVATE",
    "CHAT_WRITE_FORBIDDEN",
    "CHAT_ADMIN_REQUIRED",
    "CHAT_RESTRICTED",
    "USER_BANNED_IN_CHANNEL",
    "YOU_BLOCKED_USER",
    "USER_IS_BLOCKED",
    "PRIVACY_PREMIUM_REQUIRED",
}


def instant_send_enabled() -> bool:
    """TELEGRAM_INSTANT_SEND=false: no message reaches a person at once (owner, 10.11.2026)."""
    return (os.environ.get("TELEGRAM_INSTANT_SEND") or "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def random_id(account: str, operation_key: str, seq: int) -> int:
    digest = hashlib.sha256(f"send:{account}:{operation_key}:{seq}".encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 1  # positive int64


def message_link(entity: Any, message_id: int | None) -> str | None:
    """t.me link where Telegram has one: channels and supergroups only."""
    if not message_id or not (
        getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)
    ):
        return None
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{entity.id}/{message_id}"


def classify_error(e: BaseException) -> dict[str, Any]:
    """A concrete reason the agent can act on, not an exception name."""
    if isinstance(e, FloodWaitError):
        return {"error_code": "flood_wait", "retry_after": e.seconds, "error": str(e)}
    if isinstance(e, RPCError):
        name = (e.message or "").upper()
        if name.startswith("SLOWMODE_WAIT"):
            return {
                "error_code": "flood_wait",
                "retry_after": getattr(e, "seconds", None),
                "error": name,
            }
        if name in _PEER_NOT_FOUND:
            return {"error_code": "peer_not_found", "error": name}
        if name in _PERMISSION_DENIED or e.code == 403:
            return {"error_code": "permission_denied", "error": name}
        if name == "MESSAGE_TOO_LONG":
            return {"error_code": "message_too_long", "error": name}
        return {"error_code": "telegram_error", "error": name or type(e).__name__}
    if isinstance(e, (TimeoutError, ConnectionError, OSError)):
        return {"error_code": "timeout", "error": type(e).__name__}
    return {"error_code": "internal_error", "error": f"{type(e).__name__}: {e}"}


# --- journal -------------------------------------------------------------------------


def _rows(conn, account: str, key: str) -> dict[int, dict]:
    return {
        r["seq"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM sends WHERE account = ? AND operation_key = ?",
            (account, key),
        )
    }


def _recent_key(conn, account: str, prefix: str, since: str) -> str | None:
    row = conn.execute(
        "SELECT operation_key FROM sends WHERE account = ? AND operation_key LIKE ? "
        "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
        (account, prefix + "%", since),
    ).fetchone()
    return row[0] if row else None


def _upsert(conn, row: dict) -> None:
    now = store.now_iso()
    cols = ["created_at", "updated_at", *row]
    conn.execute(
        f"INSERT INTO sends ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        "ON CONFLICT(account, operation_key, seq) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in ["updated_at", *row]),
        [now, now, *row.values()],
    )


async def _derived_key(account: str, peer_id: int, texts: list[str], extra: str) -> str:
    base = (
        "auto:"
        + hashlib.sha256(
            "\x1f".join([account, str(peer_id), extra, *texts]).encode()
        ).hexdigest()[:16]
    )
    since = (dt.datetime.now(dt.UTC) - DERIVED_KEY_WINDOW).isoformat(timespec="seconds")
    found = await store.call(_recent_key, account, base, since)
    return found or f"{base}:{int(dt.datetime.now(dt.UTC).timestamp())}"


# --- Telegram ------------------------------------------------------------------------


def _parse(text: str, parse_mode: str | None) -> tuple[str, list]:
    mode = _normalize_parse_mode(parse_mode) or "auto"
    if mode == "auto":
        mode = detect_message_formatting(text)
    if mode == "markdown":
        return tl_markdown.parse(text)
    if mode == "html":
        return tl_html.parse(text)
    return text, []


def _sent_id(result: Any, rid: int) -> int | None:
    if isinstance(result, types.UpdateShortSentMessage):
        return result.id
    for u in getattr(result, "updates", []) or []:
        if isinstance(u, types.UpdateMessageID) and u.random_id == rid:
            return u.id
    for u in getattr(result, "updates", []) or []:
        if isinstance(u, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            return u.message.id
    return None


async def _send_text(client, entity, text, parse_mode, reply_to, rid) -> int | None:
    body, entities = _parse(text, parse_mode)
    request = functions.messages.SendMessageRequest(
        peer=entity,
        message=body,
        entities=entities or None,
        random_id=rid,
        reply_to=types.InputReplyToMessage(reply_to_msg_id=reply_to)
        if reply_to
        else None,
    )
    return _sent_id(await client(request), rid)


async def _send_files(client, entity, text, files, parse_mode, reply_to) -> int | None:
    from src.tools.messages.sending import _send_message_or_files

    mode = _normalize_parse_mode(parse_mode) or "auto"
    if mode == "auto":
        mode = detect_message_formatting(text)
    error, msg = await _send_message_or_files(
        client, entity, text, files, reply_to, mode, "send_message", {"files": files}
    )
    if error:
        raise RuntimeError(error.get("error") or error.get("note") or str(error))
    return getattr(msg, "id", None)


async def _allowed_as(client, peer_id: int, full: Any) -> str | None:
    if instant_send_enabled():
        return "instant_send"
    me = await client.get_me()
    if peer_id == getattr(me, "id", None):
        return "saved_messages"
    if (
        fixtures_enabled()
        and isinstance(full, types.Chat)
        and full.creator
        and (full.title or "").startswith(FIXTURE_PREFIX)
        and (full.participants_count or 0) <= 2
    ):
        return "test_chat"
    return None


async def _resolve(client, chat_id: str):
    """(ref, input entity, peer id, full entity) or an error dict."""
    try:
        ref = parse_peer(chat_id)
    except PeerError as e:
        return {
            "ok": False,
            "status": "failed",
            "error_code": "invalid_peer",
            "error": str(e),
        }
    try:
        entity = await client.get_input_entity(ref.telethon_target())
        peer_id = await client.get_peer_id(entity)
        check_resolved_id(peer_id)
        full = await client.get_entity(entity)
    except PeerError as e:
        return {
            "ok": False,
            "status": "failed",
            "error_code": "blocked_peer",
            "error": str(e),
        }
    except (ValueError, RPCError) as e:
        info = classify_error(e)
        if info["error_code"] in ("telegram_error", "internal_error"):
            info["error_code"] = "peer_not_found"
        return {"ok": False, "status": "failed", "chat": ref.text, **info}
    return ref, entity, peer_id, full


def _series_key(account: str, peer_text: str, texts: list[str]) -> str:
    """Same series to the same chat on the same day = one operation (a retry)."""
    day = dt.datetime.now(dt.UTC).date().isoformat()
    digest = hashlib.sha256(
        "\x1f".join([account, peer_text, *texts]).encode()
    ).hexdigest()[:16]
    return f"auto-series:{digest}:{day}"


async def _not_allowed(
    ref, texts: list[str], files, operation_key, fallback
) -> dict[str, Any]:
    """Owner, 14.11.2026: «отправь» to a person gives a draft or a scheduled message,
    never an absent tool. The existing rule (draft_save): one draft per chat; several
    messages go to the scheduled queue as parking, in order."""
    base = {"sent": False, "reason": "instant_send_disabled", "chat": ref.text}
    if files:
        return {
            **base,
            "ok": False,
            "status": "not_sent",
            "next": "Files are not sent to people from this server and cannot be left as a "
            "draft; tell the owner. Nothing was sent.",
        }
    mode = (
        fallback
        if fallback in ("draft", "parking")
        else ("draft" if len(texts) == 1 else "parking")
    )
    if mode == "draft" and len(texts) > 1:
        return {
            **base,
            "ok": False,
            "status": "not_sent",
            "next": "Telegram keeps one draft per chat: a series goes as fallback='parking' "
            "(or schedule_batch with times). Nothing was written.",
        }
    if mode == "draft":
        draft = await draft_save_impl(ref.text, texts[0], operation_key=operation_key)
        status = (
            "draft"
            if draft.get("ok") and draft.get("status") != "conflict"
            else "not_sent"
        )
        return {
            **base,
            "ok": status == "draft",
            "status": status,
            "draft": draft,
            "note": "Sending to people is switched off on this server: the text was left as "
            "a draft in the chat's input field; the owner presses Send himself.",
        }
    from src.tools.outbox.batch import schedule_batch_impl

    key = operation_key or _series_key(account_label(), ref.text, texts)
    batch = await schedule_batch_impl(
        "apply", [{"to": ref.text, "text": t} for t in texts], key
    )
    return {
        **base,
        "ok": bool(batch.get("ok"))
        and batch.get("summary", {}).get("with_issues", 0) == 0,
        "status": "scheduled",
        "scheduled_as": "parking",
        "operation_id": key,
        "batch": batch,
        "note": "Sending to people is switched off on this server: the series was put into "
        "Telegram's scheduled queue as parking (23:59 Moscow a year ahead), in this order; the "
        "owner sends it from the phone. Per-message confirmation: operation_status(operation_id).",
    }


async def send_message_now(
    chat_id: str,
    message: str,
    more_messages: list[str] | None = None,
    reply_to_id: int | None = None,
    parse_mode: str | None = "auto",
    files: str | list[str] | None = None,
    operation_key: str | None = None,
    fallback: str = "auto",
) -> dict[str, Any]:
    texts = [message, *(more_messages or [])]
    if len(texts) > MAX_SERIES:
        return {
            "ok": False,
            "status": "failed",
            "error_code": "too_many_messages",
            "error": f"at most {MAX_SERIES} messages per call",
        }
    if any(not (t or "").strip() for t in texts) and not files:
        return {
            "ok": False,
            "status": "failed",
            "error_code": "empty_message",
            "error": "every message of the series needs text",
        }
    if any(len(t) > MAX_CHARS for t in texts):
        return {
            "ok": False,
            "status": "failed",
            "error_code": "message_too_long",
            "error": f"a message is longer than {MAX_CHARS} characters",
        }

    client = await get_connected_client()
    resolved = await _resolve(client, chat_id)
    if isinstance(resolved, dict):
        return resolved
    ref, entity, peer_id, full = resolved
    allowed_as = await _allowed_as(client, peer_id, full)
    if allowed_as is None:
        return await _not_allowed(ref, texts, files, operation_key, fallback)

    account = account_label()
    key_source = "client" if operation_key else "derived"
    key = operation_key or await _derived_key(
        account, peer_id, texts, f"{reply_to_id}|{files}"
    )
    done = await store.call(_rows, account, key)
    for seq, prior in done.items():
        if seq < len(texts) and prior["text_version"] != store.version(texts[seq]):
            return {
                "ok": False,
                "status": "failed",
                "error_code": "operation_key_conflict",
                "error": "this operation_key was used for a different text; use a new key",
                "operation_key": key,
            }

    results = await _send_series(
        client,
        entity,
        full,
        ref,
        peer_id,
        account,
        key,
        texts,
        done,
        reply_to_id=reply_to_id,
        parse_mode=parse_mode,
        files=files,
    )
    return _summary(results, account, ref, peer_id, full, key, key_source, allowed_as)


async def _send_series(
    client,
    entity,
    full,
    ref,
    peer_id,
    account,
    key,
    texts,
    done,
    *,
    reply_to_id,
    parse_mode,
    files,
) -> list[dict]:
    agent = current_agent()
    results: list[dict] = []
    stopped = False
    for seq, text in enumerate(texts):
        prior = done.get(seq)
        if prior and prior["status"] == "sent":
            results.append(
                {
                    "seq": seq,
                    "state": "sent",
                    "repeat": True,
                    "message_id": prior["tg_msg_id"],
                    "link": prior["link"],
                }
            )
            continue
        if stopped:
            results.append({"seq": seq, "state": "not_attempted"})
            continue
        with_files = files if seq == 0 else None
        if prior and prior["status"] == "sending" and with_files:
            # A file upload has no random_id we control: it may or may not have arrived.
            results.append(
                {
                    "seq": seq,
                    "state": "unknown",
                    "error_code": "unknown_after_timeout",
                    "error": "an earlier attempt with files did not finish; check the chat",
                }
            )
            stopped = True
            continue
        row = {
            "account": account,
            "agent": agent,
            "operation_key": key,
            "seq": seq,
            "peer": ref.text,
            "peer_id": peer_id,
            "text_version": store.version(text),
        }
        await store.call(_upsert, {**row, "status": "sending"})
        outcome = await _send_one(
            client,
            entity,
            full,
            account,
            key,
            seq,
            text,
            reply_to_id if seq == 0 else None,
            parse_mode,
            with_files,
        )
        if outcome["state"] in ("sent", "failed"):
            await store.call(
                _upsert,
                {
                    **row,
                    "status": outcome["state"],
                    "tg_msg_id": outcome.get("message_id"),
                    "link": outcome.get("link"),
                    "error_code": outcome.get("error_code"),
                    "error": outcome.get("error"),
                },
            )
        results.append({"seq": seq, **outcome})
        stopped = outcome["state"] != "sent"
    return results


async def _send_one(
    client, entity, full, account, key, seq, text, reply_to, parse_mode, files
) -> dict[str, Any]:
    rid = random_id(account, key, seq)
    try:
        if files:
            msg_id = await _send_files(
                client, entity, text, files, parse_mode, reply_to
            )
        else:
            msg_id = await _send_text(client, entity, text, parse_mode, reply_to, rid)
    except RPCError as e:
        if "RANDOM_ID_DUPLICATE" in (e.message or ""):
            return {
                "state": "sent",
                "delivered_earlier": True,
                "message_id": None,
                "link": None,
            }
        return {"state": "failed", **classify_error(e)}
    except (TimeoutError, ConnectionError, OSError) as e:
        # Delivery unknown: the row stays 'sending'; a retry with the same key reuses
        # the random_id, so Telegram refuses a duplicate if the first one arrived.
        return {"state": "unknown", **classify_error(e)}
    except Exception as e:  # reported per message, never swallowed
        logger.exception("send_message %s/%s failed", key, seq)
        return {"state": "failed", **classify_error(e)}
    return {"state": "sent", "message_id": msg_id, "link": message_link(full, msg_id)}


def _summary(
    results, account, ref, peer_id, full, key, key_source, allowed_as
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    sent = counts.get("sent", 0)
    status = "sent" if sent == len(results) else ("failed" if sent == 0 else "partial")
    out = {
        "ok": status == "sent",
        "status": status,
        "account": account,
        "chat": ref.text,
        "peer_id": peer_id,
        "title": getattr(full, "title", None) or getattr(full, "first_name", None),
        "allowed_as": allowed_as,
        "operation_key": key,
        "key_source": key_source,
        "messages": results,
        "summary": counts,
    }
    if status != "sent":
        out["next"] = (
            "retry with the same operation_key: delivered messages are not sent again"
            + (
                "; wait retry_after seconds first"
                if any(r.get("retry_after") for r in results)
                else ""
            )
        )
    if ref.kind == "me" or not message_link(full, 1):
        out["link_note"] = (
            "Telegram has no t.me links for private chats, Saved Messages and basic groups"
        )
    return out
