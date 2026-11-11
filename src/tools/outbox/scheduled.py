"""Scheduled messages of both accounts in one index, and changes to chosen items.

Telegram has no "all scheduled messages" call: the queue is per chat. Each account's
server therefore keeps an index in the shared outbox database, fed by:
- Telegram's own updates (a message scheduled, edited or removed on the phone);
- a short tick over chats that hold scheduled messages and recently active chats;
- a daily sweep over every dialog.
Reading the index is one call for both accounts; each account says how fresh it is.

Changing items is explicit: the caller names each item and the version it read.
If the owner changed it on the phone since, the item is a conflict and untouched.
Parking maintenance only ever moves the date of confirmed parking; text never changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import sqlite3
import time
from typing import Any

from telethon import events
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions, types

from src.tools.outbox import parking, store
from src.tools.outbox.agent import current_agent
from src.tools.outbox.peers import PeerError, PeerRef

logger = logging.getLogger(__name__)

TICK_SECONDS = int(os.environ.get("SCHEDULED_TICK_SECONDS") or 45)
RECENT_DIALOGS = int(os.environ.get("SCHEDULED_RECENT_DIALOGS") or 40)
RECENT_EVERY = dt.timedelta(
    seconds=int(os.environ.get("SCHEDULED_RECENT_SECONDS") or 300)
)
SWEEP_EVERY = dt.timedelta(hours=int(os.environ.get("SCHEDULED_SWEEP_HOURS") or 24))
SWEEP_PAUSE = float(os.environ.get("SCHEDULED_SWEEP_PAUSE") or 0.35)
MAINTENANCE_EVERY = dt.timedelta(
    hours=int(os.environ.get("PARKING_MAINTENANCE_HOURS") or 24)
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled (
    account       TEXT NOT NULL,
    peer_id       INTEGER NOT NULL,
    msg_id        INTEGER NOT NULL,
    peer          TEXT NOT NULL,
    peer_title    TEXT,
    topic_id      INTEGER,
    scheduled_for TEXT NOT NULL,
    text          TEXT,
    has_media     INTEGER NOT NULL DEFAULT 0,
    version       TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    PRIMARY KEY (account, peer_id, msg_id)
);
CREATE TABLE IF NOT EXISTS scheduled_chats (
    account      TEXT NOT NULL,
    peer_id      INTEGER NOT NULL,
    peer         TEXT NOT NULL,
    peer_title   TEXT,
    items        INTEGER NOT NULL DEFAULT 0,
    last_checked TEXT,
    error        TEXT,
    PRIMARY KEY (account, peer_id)
);
CREATE TABLE IF NOT EXISTS scheduled_state (
    account          TEXT PRIMARY KEY,
    last_tick        TEXT,
    last_sweep_start TEXT,
    last_sweep_done  TEXT,
    sweep_chats      INTEGER,
    last_maintenance TEXT,
    listener         TEXT
);
"""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def item_version(text: str | None, scheduled_for: str) -> str:
    """What an agent read: text and date together, so a moved date is also a change."""
    return store.version(f"{scheduled_for}\n{text or ''}")


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


# --------------------------------------------------------------------------- peers


def peer_ref_for(entity: Any) -> PeerRef | None:
    if isinstance(entity, types.User):
        return (
            PeerRef("me", "me") if entity.is_self else PeerRef("user", str(entity.id))
        )
    if isinstance(entity, types.Chat):
        return PeerRef("chat", str(entity.id))
    if isinstance(entity, types.Channel):
        return PeerRef("channel", str(entity.id))
    return None


def _title(entity: Any) -> str | None:
    if isinstance(entity, types.User):
        if entity.is_self:
            return "Saved Messages"
        return (
            " ".join(x for x in (entity.first_name, entity.last_name) if x)
            or entity.username
        )
    return getattr(entity, "title", None)


def _scannable(entity: Any) -> bool:
    """Broadcast channels refuse the scheduled queue unless one is an admin."""
    if isinstance(entity, types.Channel) and entity.broadcast:
        return bool(entity.creator or entity.admin_rights)
    return not isinstance(entity, (types.ChatForbidden, types.ChannelForbidden))


# --------------------------------------------------------------------------- index


def _replace_chat(
    conn,
    account: str,
    peer_id: int,
    ref: str,
    title: str | None,
    items: list[dict],
    error: str | None,
) -> dict:
    _ensure(conn)
    now = store.now_iso()
    old = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT msg_id, version FROM scheduled WHERE account = ? AND peer_id = ?",
            (account, peer_id),
        )
    }
    if error is None:
        seen = set()
        for it in items:
            seen.add(it["msg_id"])
            conn.execute(
                "INSERT INTO scheduled (account, peer_id, msg_id, peer, peer_title, topic_id, scheduled_for,"
                " text, has_media, version, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(account, peer_id, msg_id) DO UPDATE SET peer=excluded.peer,"
                " peer_title=excluded.peer_title, topic_id=excluded.topic_id,"
                " scheduled_for=excluded.scheduled_for, text=excluded.text, has_media=excluded.has_media,"
                " version=excluded.version, last_seen=excluded.last_seen",
                (
                    account,
                    peer_id,
                    it["msg_id"],
                    ref,
                    title,
                    it["topic_id"],
                    it["scheduled_for"],
                    it["text"],
                    int(it["has_media"]),
                    it["version"],
                    now,
                    now,
                ),
            )
        gone = [m for m in old if m not in seen]
        for m in gone:
            conn.execute(
                "DELETE FROM scheduled WHERE account = ? AND peer_id = ? AND msg_id = ?",
                (account, peer_id, m),
            )
    conn.execute(
        "INSERT INTO scheduled_chats (account, peer_id, peer, peer_title, items, last_checked, error)"
        " VALUES (?,?,?,?,?,?,?) ON CONFLICT(account, peer_id) DO UPDATE SET peer=excluded.peer,"
        " peer_title=excluded.peer_title, items=excluded.items, last_checked=excluded.last_checked,"
        " error=excluded.error",
        (
            account,
            peer_id,
            ref,
            title,
            len(items) if error is None else len(old),
            now,
            error,
        ),
    )
    changed = error is None and (
        set(old) != {i["msg_id"] for i in items}
        or any(old.get(i["msg_id"]) != i["version"] for i in items)
    )
    return {"changed": changed, "items": len(items)}


def _message_item(msg: Any) -> dict | None:
    if not isinstance(msg, types.Message) or msg.date is None:
        return None
    when = msg.date.astimezone(dt.UTC).isoformat(timespec="seconds")
    reply = msg.reply_to
    topic = None
    if isinstance(reply, types.MessageReplyHeader) and reply.forum_topic:
        topic = reply.reply_to_top_id or reply.reply_to_msg_id
    return {
        "msg_id": msg.id,
        "scheduled_for": when,
        "text": msg.message or "",
        "has_media": msg.media is not None,
        "topic_id": topic,
        "version": item_version(msg.message, when),
    }


async def fetch_chat(client, account: str, entity: Any, input_peer: Any = None) -> dict:
    """Read one chat's scheduled queue from Telegram and replace it in the index."""
    ref = peer_ref_for(entity)
    if ref is None:
        return {"skipped": True}
    peer_id = await client.get_peer_id(entity)
    error, items = None, []
    try:
        res = await client(
            functions.messages.GetScheduledHistoryRequest(
                peer=input_peer or entity, hash=0
            )
        )
        items = [
            i for i in (_message_item(m) for m in getattr(res, "messages", [])) if i
        ]
    except FloodWaitError:
        raise
    except RPCError as e:
        error = type(e).__name__
    out = await store.call(
        _replace_chat, account, peer_id, ref.text, _title(entity), items, error
    )
    return {**out, "peer_id": peer_id, "error": error}


def _set_state(conn, account: str, **fields) -> None:
    _ensure(conn)
    conn.execute(
        "INSERT INTO scheduled_state (account) VALUES (?) ON CONFLICT(account) DO NOTHING",
        (account,),
    )
    if fields:
        conn.execute(
            f"UPDATE scheduled_state SET {', '.join(f'{k} = ?' for k in fields)} WHERE account = ?",
            [*fields.values(), account],
        )


def _state(conn, account: str | None = None) -> list[dict]:
    _ensure(conn)
    sql = "SELECT * FROM scheduled_state" + (" WHERE account = ?" if account else "")
    return [dict(r) for r in conn.execute(sql, (account,) if account else ())]


def _chats_with_items(conn, account: str) -> list[int]:
    _ensure(conn)
    return [
        r[0]
        for r in conn.execute(
            "SELECT peer_id FROM scheduled_chats WHERE account = ? AND items > 0",
            (account,),
        )
    ]


# --------------------------------------------------------------------------- collector


class Collector:
    """Background indexer for one account, inside that account's server process."""

    def __init__(self, account: str, get_client):
        self.account = account
        self._get_client = get_client
        self._dirty: asyncio.Queue[int] = asyncio.Queue()
        self._handler_on = None
        self._last_recent: dt.datetime | None = None

    async def _client(self):
        client = await self._get_client()
        if self._handler_on is not client:
            client.add_event_handler(
                self._on_update,
                events.Raw(
                    (
                        types.UpdateNewScheduledMessage,
                        types.UpdateDeleteScheduledMessages,
                    )
                ),
            )
            self._handler_on = client
            await store.call(_set_state, self.account, listener=store.now_iso())
        return client

    async def _on_update(self, update) -> None:
        peer = (
            update.message.peer_id
            if isinstance(update, types.UpdateNewScheduledMessage)
            else update.peer
        )
        try:
            client = await self._client()
            await self._dirty.put(await client.get_peer_id(peer))
        except Exception as e:  # an update must never break the listener
            logger.warning("scheduled update not queued: %s", e)

    async def refresh_peer(self, client, peer_id: int) -> dict:
        entity = await client.get_entity(peer_id)
        return await fetch_chat(client, self.account, entity)

    async def _drain(self, client) -> set[int]:
        seen: set[int] = set()
        while not self._dirty.empty():
            pid = self._dirty.get_nowait()
            if pid not in seen:
                seen.add(pid)
                await self.refresh_peer(client, pid)
        if seen:
            await store.call(_set_state, self.account, last_tick=store.now_iso())
        return seen

    async def tick(self) -> None:
        client = await self._client()
        seen = await self._drain(client)
        for pid in await store.call(_chats_with_items, self.account):
            if pid not in seen:
                seen.add(pid)
                await self.refresh_peer(client, pid)
        if self._last_recent is None or _now() - self._last_recent > RECENT_EVERY:
            async for d in client.iter_dialogs(limit=RECENT_DIALOGS):
                if d.id in seen or not _scannable(d.entity):
                    continue
                seen.add(d.id)
                await fetch_chat(client, self.account, d.entity, d.input_entity)
                await asyncio.sleep(SWEEP_PAUSE)
            self._last_recent = _now()
        await store.call(_set_state, self.account, last_tick=store.now_iso())

    async def sweep(self) -> int:
        client = await self._client()
        await store.call(_set_state, self.account, last_sweep_start=store.now_iso())
        n = 0
        async for d in client.iter_dialogs():
            await self._drain(
                client
            )  # a phone change must not wait for the whole sweep
            if not _scannable(d.entity):
                continue
            await fetch_chat(client, self.account, d.entity, d.input_entity)
            n += 1
            await asyncio.sleep(SWEEP_PAUSE)
        await store.call(
            _set_state, self.account, last_sweep_done=store.now_iso(), sweep_chats=n
        )
        return n

    async def run(self) -> None:
        last_sweep = None
        last_maint = None
        while True:
            try:
                state = (await store.call(_state, self.account) or [{}])[0]
                done = state.get("last_sweep_done")
                last_sweep = last_sweep or (
                    dt.datetime.fromisoformat(done) if done else None
                )
                await self.tick()
                if last_sweep is None or _now() - last_sweep > SWEEP_EVERY:
                    await self.sweep()
                    last_sweep = _now()
                maint = state.get("last_maintenance")
                last_maint = last_maint or (
                    dt.datetime.fromisoformat(maint) if maint else None
                )
                if last_maint is None or _now() - last_maint > MAINTENANCE_EVERY:
                    await maintain_parking(await self._client(), self.account)
                    last_maint = _now()
            except FloodWaitError as e:
                logger.warning("scheduled index: FloodWait %ss", e.seconds)
                await asyncio.sleep(e.seconds + 1)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                import traceback

                logger.error(
                    "scheduled index tick failed: %s: %s\n%s",
                    type(e).__name__,
                    e,
                    traceback.format_exc(limit=6),
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wait_dirty(), timeout=TICK_SECONDS)

    async def _wait_dirty(self) -> None:
        pid = await self._dirty.get()
        await self._dirty.put(pid)


# --------------------------------------------------------------------------- parking


def _parking_rows(conn, account: str) -> list[dict]:
    _ensure(conn)
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM scheduled WHERE account = ? ORDER BY peer_id, scheduled_for, msg_id",
            (account,),
        )
    ]


async def maintain_parking(client, account: str) -> dict:
    """Move confirmed parking forward; evacuate what can no longer be kept. Dates only."""
    enabled = (os.environ.get("PARKING_AUTOMATION") or "").lower() in (
        "1",
        "true",
        "yes",
    )
    now = _now()
    rows = await store.call(_parking_rows, account)
    plan, by_chat = [], {}
    for r in rows:
        when = dt.datetime.fromisoformat(r["scheduled_for"])
        kind = parking.classify(when, now)
        if kind == "parking":
            by_chat.setdefault(r["peer_id"], []).append(r)
        act = parking.action(when, now, kind)
        if act:
            plan.append((act, r))
    done = []
    for act, r in plan:
        entry = {
            "action": act,
            "peer": r["peer"],
            "msg_id": r["msg_id"],
            "from": r["scheduled_for"],
        }
        if not enabled:
            entry["result"] = "dry_run"
            done.append(entry)
            continue
        try:
            entity = await client.get_input_entity(_target(r["peer"]))
            if act == "prolong":
                order = [x["msg_id"] for x in by_chat.get(r["peer_id"], [])].index(
                    r["msg_id"]
                )
                new = parking.prolong_target(now, order)
                await client(
                    functions.messages.EditMessageRequest(
                        peer=entity, id=r["msg_id"], schedule_date=new
                    )
                )
                entry.update(result="prolonged", to=new.isoformat())
            else:
                when = dt.datetime.fromisoformat(r["scheduled_for"])
                await client.send_message(
                    "me",
                    parking.evacuation_text(
                        r["peer_title"], account, when, r["text"] or ""
                    ),
                )
                await client(
                    functions.messages.DeleteScheduledMessagesRequest(
                        peer=entity, id=[r["msg_id"]]
                    )
                )
                entry.update(result="evacuated_to_saved_messages")
        except Exception as e:
            entry.update(result="failed", error=f"{type(e).__name__}: {e}")
        done.append(entry)
    await store.call(_set_state, account, last_maintenance=store.now_iso())
    if done:
        logger.info("parking maintenance %s: %s", account, done)
    return {"enabled": enabled, "actions": done}


def _target(peer_text: str):
    if peer_text == "me":
        return "me"
    kind, _, value = peer_text.partition(":")
    return PeerRef(kind, value).telethon_target()


# --------------------------------------------------------------------------- tools


def _list(
    conn,
    account: str | None,
    peer: str | None,
    date_from: str | None,
    date_to: str | None,
) -> list[dict]:
    _ensure(conn)
    sql, args = "SELECT * FROM scheduled WHERE 1=1", []
    if account:
        sql, args = sql + " AND account = ?", [*args, account]
    if peer:
        sql, args = sql + " AND peer = ?", [*args, peer]
    if date_from:
        sql, args = sql + " AND scheduled_for >= ?", [*args, date_from]
    if date_to:
        sql, args = sql + " AND scheduled_for < ?", [*args, date_to]
    return [
        dict(r)
        for r in conn.execute(
            sql + " ORDER BY scheduled_for, account, peer_id, msg_id", args
        )
    ]


def _agent_rows(conn) -> dict[tuple, str]:
    return {
        (r[0], r[1], r[2]): r[3]
        for r in conn.execute(
            "SELECT account, peer_id, tg_msg_id, agent FROM outbox WHERE tg_msg_id IS NOT NULL "
            "AND kind IN ('scheduled', 'parking')"
        )
    }


def _msk_bound(value: str | None, end: bool = False) -> str | None:
    if not value:
        return None
    d = dt.datetime.fromisoformat(value)
    if d.tzinfo is None:
        d = d.replace(tzinfo=parking.MSK)
    if end and len(value) <= 10:
        d += dt.timedelta(days=1)
    return d.astimezone(dt.UTC).isoformat(timespec="seconds")


async def scheduled_list_impl(
    account: str | None = None,
    chat_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    peer = None
    if chat_id:
        from src.tools.outbox.peers import parse_peer

        try:
            peer = parse_peer(chat_id).text
        except PeerError as e:
            return {"ok": False, "error": str(e)}
    rows = await store.call(
        _list, account, peer, _msk_bound(date_from), _msk_bound(date_to, end=True)
    )
    agents = await store.call(_agent_rows)
    states = await store.call(_state, None)
    now = _now()
    items = []
    for r in rows:
        when = dt.datetime.fromisoformat(r["scheduled_for"])
        k = parking.classify(when, now)
        if (kind == "parking" and k is None) or (kind == "real" and k is not None):
            continue
        items.append(
            {
                "ref": f"{r['account']}:{r['peer']}:{r['msg_id']}",
                "account": r["account"],
                "chat": r["peer"],
                "title": r["peer_title"],
                "topic_id": r["topic_id"],
                "at_msk": when.astimezone(parking.MSK).strftime("%Y-%m-%d %H:%M:%S"),
                "kind": {
                    "parking": "parking",
                    "looks_like_parking": "looks_like_parking",
                }.get(k, "real"),
                "text": r["text"],
                "has_media": bool(r["has_media"]),
                "version": r["version"],
                "set_by": agents.get(
                    (r["account"], r["peer_id"], r["msg_id"]), "owner_or_unknown"
                ),
                "indexed_at": r["last_seen"],
            }
        )
    fresh = {
        s["account"]: {
            "last_tick": s["last_tick"],
            "last_full_sweep": s["last_sweep_done"],
            "sweep_chats": s["sweep_chats"],
            "listener_since": s["listener"],
        }
        for s in states
    }
    chats = len({(i["account"], i["chat"]) for i in items})
    return {
        "ok": True,
        "items": items,
        "total": len(items),
        "chats": chats,
        "coverage": fresh,
        "as_of": store.now_iso(),
        "server_ms": round((time.perf_counter() - t0) * 1000),
        "note": "index of both accounts; a phone change normally appears within one tick "
        f"(~{TICK_SECONDS}s) through Telegram updates",
    }


async def scheduled_change_impl(
    items: list[dict], operation_key: str | None = None
) -> dict[str, Any]:
    """Change or cancel chosen items of THIS account. Each item: ref, expected_version,
    and one of text / at_msk / cancel. A changed or missing item is never touched."""
    from src.client.connection import get_connected_client
    from src.tools.outbox.drafts import account_label

    account, agent = account_label(), current_agent()
    if not items:
        return {"ok": False, "error": "no items"}
    prior = {r["seq"]: r for r in await store.find_operation(account, operation_key)}
    client = await get_connected_client()
    results = []
    for seq, it in enumerate(items):
        ref = str(it.get("ref") or "")
        res = {"ref": ref}
        if seq in prior:
            results.append({**res, "status": prior[seq]["status"], "repeat": True})
            continue
        acc, _, rest = ref.partition(":")
        peer_text, _, msg = rest.rpartition(":")
        if acc != account:
            results.append(
                {**res, "status": "other_account", "hint": f"call the {acc} connector"}
            )
            continue
        try:
            msg_id = int(msg)
            entity = await client.get_input_entity(_target(peer_text))
            peer_id = await client.get_peer_id(entity)
            live = await client(
                functions.messages.GetScheduledMessagesRequest(peer=entity, id=[msg_id])
            )
            cur = next(
                (
                    _message_item(m)
                    for m in live.messages
                    if getattr(m, "id", None) == msg_id
                ),
                None,
            )
        except (ValueError, PeerError, RPCError) as e:
            results.append(
                {**res, "status": "error", "error": f"{type(e).__name__}: {e}"}
            )
            continue
        if cur is None:
            results.append({**res, "status": "gone", "note": "already sent or removed"})
            continue
        if cur["version"] != it.get("expected_version"):
            results.append(
                {
                    **res,
                    "status": "conflict",
                    "now": {
                        "text": cur["text"],
                        "at": cur["scheduled_for"],
                        "version": cur["version"],
                    },
                }
            )
            continue
        try:
            if it.get("cancel"):
                await client(
                    functions.messages.DeleteScheduledMessagesRequest(
                        peer=entity, id=[msg_id]
                    )
                )
                status, after = "cancelled", None
            else:
                new_date = None
                if it.get("at_msk"):
                    new_date = dt.datetime.fromisoformat(it["at_msk"])
                    new_date = (
                        new_date
                        if new_date.tzinfo
                        else new_date.replace(tzinfo=parking.MSK)
                    )
                new_text = it.get("text")
                if new_text is None and new_date is None:
                    results.append(
                        {**res, "status": "error", "error": "nothing to change"}
                    )
                    continue
                await client(
                    functions.messages.EditMessageRequest(
                        peer=entity,
                        id=msg_id,
                        message=new_text,
                        schedule_date=new_date
                        or dt.datetime.fromisoformat(cur["scheduled_for"]),
                    )
                )
                live = await client(
                    functions.messages.GetScheduledMessagesRequest(
                        peer=entity, id=[msg_id]
                    )
                )
                after = next(
                    (
                        _message_item(m)
                        for m in live.messages
                        if getattr(m, "id", None) == msg_id
                    ),
                    None,
                )
                status = "changed" if after else "gone"
        except RPCError as e:
            results.append({**res, "status": "error", "error": type(e).__name__})
            continue
        oid = await store.record(
            {
                "account": account,
                "agent": agent,
                "operation_key": operation_key,
                "seq": seq,
                "kind": "parking"
                if parking.classify(
                    dt.datetime.fromisoformat(cur["scheduled_for"]), _now()
                )
                else "scheduled",
                "peer": peer_text,
                "peer_id": peer_id,
                "tg_msg_id": msg_id,
                "scheduled_for": (after or cur)["scheduled_for"],
                "text_agent": (after or cur)["text"],
                "status": status,
                "text_now": after["text"] if after else None,
                "result": f"before:{cur['version']}",
            },
            {"before": cur, "after": after},
        )
        await store.call(_replace_one, account, peer_id, after, msg_id)
        results.append({**res, "status": status, "outbox_id": oid, "after": after})
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "ok": True,
        "account": account,
        "results": results,
        "summary": counts,
        "as_of": store.now_iso(),
    }


def _replace_one(
    conn, account: str, peer_id: int, after: dict | None, msg_id: int
) -> None:
    _ensure(conn)
    if after is None:
        conn.execute(
            "DELETE FROM scheduled WHERE account = ? AND peer_id = ? AND msg_id = ?",
            (account, peer_id, msg_id),
        )
        return
    conn.execute(
        "UPDATE scheduled SET scheduled_for = ?, text = ?, version = ?, last_seen = ? "
        "WHERE account = ? AND peer_id = ? AND msg_id = ?",
        (
            after["scheduled_for"],
            after["text"],
            after["version"],
            store.now_iso(),
            account,
            peer_id,
            msg_id,
        ),
    )


async def start_collector() -> asyncio.Task | None:
    """Start the indexer when SCHEDULED_INDEX=true and the server holds exactly one session."""
    if (os.environ.get("SCHEDULED_INDEX") or "").lower() not in ("1", "true", "yes"):
        return None
    from src.client.connection import _get_client_by_token, ensure_connection
    from src.config.server_config import cfg
    from src.tools.outbox.drafts import account_label

    sessions = sorted(cfg().session_directory.glob("*.session"))
    if len(sessions) != 1:
        logger.error(
            "scheduled index not started: expected one session, found %s", len(sessions)
        )
        return None
    token = sessions[0].stem

    async def get_client():
        client = await _get_client_by_token(token)
        if not await ensure_connection(client, token):
            raise ConnectionError("Telegram connection for the scheduled index failed")
        return client

    logger.info("scheduled index started for account %s", account_label())
    return asyncio.create_task(Collector(account_label(), get_client).run())
