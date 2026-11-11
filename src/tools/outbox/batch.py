"""A batch of messages into the scheduled queue: preview first, then apply.

Nothing here sends a message now. Every message gets a schedule date: the one asked
for, or — with no date — parking (23:59 Moscow, one year ahead), which the server
keeps alive and the owner sends himself from the phone.

The code, not the agent, checks each row: typed recipient, Moscow date, Telegram's
horizon, the per-chat limit, and order within a chat. Apply returns an operation id
at once and works in the background. Each row's random_id is derived from the
operation key, so a retry after a lost connection cannot create a duplicate:
Telegram itself refuses the same random_id twice.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import itertools
import logging
import time
from typing import Any

from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions, types

from src.tools.outbox import parking, scheduled, store
from src.tools.outbox.agent import current_agent
from src.tools.outbox.drafts import account_label
from src.tools.outbox.peers import PeerError, check_resolved_id, parse_peer

logger = logging.getLogger(__name__)

MAX_ITEMS = 200
MAX_CHARS = 4096
# Telegram accepted +367 days and refused +368 (measured 10.11.2026). Keep a margin.
HORIZON = dt.timedelta(days=366)
MIN_LEAD = dt.timedelta(minutes=2)
# Telegram's per-chat cap is reported as 100 (not in the official docs); the batch
# refuses rows above it instead of discovering SCHEDULE_TOO_MUCH half way.
PER_CHAT_LIMIT = 100

_tasks: dict[str, asyncio.Task] = {}


def random_id(operation_key: str, seq: int) -> int:
    digest = hashlib.sha256(f"{operation_key}:{seq}".encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 1  # positive int64


def parse_at(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    d = dt.datetime.fromisoformat(value)
    return d if d.tzinfo else d.replace(tzinfo=parking.MSK)


def plan_rows(
    items: list[dict], account: str, now: dt.datetime, existing_per_chat: dict[str, int]
) -> list[dict]:
    """Validate and order rows without Telegram. Pure: tested directly."""
    rows: list[dict] = []
    for seq, it in enumerate(items):
        row = {
            "seq": seq,
            "account": (it.get("account") or account).strip(),
            "to": it.get("to"),
            "topic_id": it.get("topic_id"),
            "text": (it.get("text") or "").strip(),
            "issues": [],
        }
        try:
            row["chat"] = parse_peer(str(it.get("to") or "")).text
        except PeerError as e:
            row["chat"] = None
            row["issues"].append(str(e))
        if not row["text"]:
            row["issues"].append("empty text")
        elif len(row["text"]) > MAX_CHARS:
            row["issues"].append(f"text longer than {MAX_CHARS} characters")
        try:
            at = parse_at(it.get("at_msk"))
        except ValueError:
            at = None
            row["issues"].append("at_msk is not a date: use YYYY-MM-DDTHH:MM (Moscow)")
        row["requested_at"] = at
        rows.append(row)

    by_chat: dict[tuple, list[dict]] = {}
    for r in rows:
        if r["chat"]:
            by_chat.setdefault(
                (r["account"], r["chat"], r["topic_id"] or 0), []
            ).append(r)
    for (_acc, chat, _topic), group in by_chat.items():
        parked = [r for r in group if r["requested_at"] is None]
        for order, r in enumerate(parked):
            r["at"], r["kind"] = parking.prolong_target(now, order), "parking"
        for r in group:
            if r["requested_at"] is not None:
                r["at"], r["kind"] = r["requested_at"], "scheduled"
        # same second in one chat: keep the dictated order with one-second steps
        group.sort(key=lambda r: (r["at"], r["seq"]))
        for prev, cur in itertools.pairwise(group):
            if cur["at"] <= prev["at"]:
                cur["at"] = prev["at"] + dt.timedelta(seconds=1)
        used = existing_per_chat.get(chat, 0)
        for r in group:
            if r["at"] - now > HORIZON:
                r["issues"].append(
                    "beyond Telegram's limit of about one year: not accepted"
                )
            elif r["at"] - now < MIN_LEAD:
                r["issues"].append(
                    "less than 2 minutes ahead: would be sent almost at once"
                )
            if not r["issues"]:
                used += 1
                if used > PER_CHAT_LIMIT:
                    r["issues"].append(
                        f"chat would hold more than {PER_CHAT_LIMIT} scheduled messages"
                    )
    for r in rows:
        r.setdefault("at", None)
        r.setdefault("kind", None)
        r["ok"] = not r["issues"]
    return rows


def _view(r: dict, title: str | None = None) -> dict:
    return {
        "seq": r["seq"],
        "account": r["account"],
        "to": r["to"],
        "chat": r["chat"],
        "title": title,
        "topic_id": r["topic_id"],
        "at_msk": r["at"].astimezone(parking.MSK).strftime("%Y-%m-%d %H:%M:%S")
        if r["at"]
        else None,
        "kind": r["kind"],
        "text": r["text"],
        "ok": r["ok"],
        "issues": r["issues"],
    }


def _existing_counts(conn, account: str) -> dict[str, int]:
    scheduled._ensure(conn)
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT peer, count(*) FROM scheduled WHERE account = ? GROUP BY peer",
            (account,),
        )
    }


async def _titles(client, rows: list[dict], account: str) -> dict[str, tuple]:
    """Resolve this account's recipients once per chat: (title, entity, peer_id) or an error."""
    out: dict[str, tuple] = {}
    for r in rows:
        if r["account"] != account or not r["chat"] or r["chat"] in out:
            continue
        try:
            entity = await client.get_entity(parse_peer(r["chat"]).telethon_target())
            peer_id = await client.get_peer_id(entity)
            check_resolved_id(peer_id)
            out[r["chat"]] = (scheduled._title(entity), entity, peer_id, None)
        except (ValueError, PeerError, RPCError) as e:
            out[r["chat"]] = (
                None,
                None,
                None,
                f"recipient not found on {account}: {type(e).__name__}",
            )
    return out


async def schedule_batch_impl(
    mode: str, items: list[dict], operation_key: str | None
) -> dict[str, Any]:
    from src.client.connection import get_connected_client

    t0 = time.perf_counter()
    account = account_label()
    if mode not in ("preview", "apply"):
        return {"ok": False, "error": "mode: preview | apply"}
    if not items or len(items) > MAX_ITEMS:
        return {"ok": False, "error": f"1..{MAX_ITEMS} items"}
    if mode == "apply" and not operation_key:
        return {
            "ok": False,
            "error": "apply needs operation_key (the same one on retry)",
        }
    if mode == "apply" and operation_key in _tasks and not _tasks[operation_key].done():
        return {
            "ok": True,
            "operation_id": operation_key,
            "status": "running",
            "repeat": True,
        }
    done_seqs = (
        {r["seq"] for r in await store.find_operation(account, operation_key)}
        if mode == "apply"
        else set()
    )

    now = dt.datetime.now(dt.UTC)
    existing = await store.call(_existing_counts, account)
    rows = plan_rows(items, account, now, existing)
    client = await get_connected_client()
    resolved = await _titles(client, rows, account)
    for r in rows:
        if r["account"] == account and r["chat"] in resolved and resolved[r["chat"]][3]:
            r["issues"].append(resolved[r["chat"]][3])
            r["ok"] = False
    view = [_view(r, resolved.get(r["chat"], (None,))[0]) for r in rows]
    other = sorted({r["account"] for r in rows if r["account"] != account})
    summary = {
        "rows": len(rows),
        "ok": sum(r["ok"] for r in rows),
        "with_issues": sum(not r["ok"] for r in rows),
        "chats": len({(r["account"], r["chat"]) for r in rows if r["chat"]}),
        "parking": sum(r["kind"] == "parking" for r in rows),
    }
    base = {
        "ok": True,
        "mode": mode,
        "account": account,
        "summary": summary,
        "rows": view,
        "other_accounts": other,
        "server_ms": round((time.perf_counter() - t0) * 1000),
    }
    if mode == "preview":
        base["next"] = (
            "apply with the same items and an operation_key; rows of other accounts go "
            "through that account's connector"
        )
        return base

    todo = [
        r
        for r in rows
        if r["ok"] and r["account"] == account and r["seq"] not in done_seqs
    ]
    if done_seqs and not todo:
        return {
            **base,
            "operation_id": operation_key,
            "status": "already_applied",
            "repeat": True,
            "next": "operation_status(operation_id)",
        }
    agent = current_agent()
    _tasks[operation_key] = asyncio.create_task(
        _apply(client, account, agent, operation_key, todo, resolved)
    )
    return {
        **base,
        "operation_id": operation_key,
        "status": "started",
        "queued": len(todo),
        "next": "operation_status(operation_id) — per-row result as Telegram confirms",
    }


async def _apply(
    client, account: str, agent: str, key: str, todo: list[dict], resolved: dict
) -> None:
    for r in todo:
        title, entity, peer_id, _ = resolved[r["chat"]]
        reply_to = (
            types.InputReplyToMessage(
                reply_to_msg_id=r["topic_id"], top_msg_id=r["topic_id"]
            )
            if r["topic_id"]
            else None
        )
        base = {
            "account": account,
            "agent": agent,
            "operation_key": key,
            "seq": r["seq"],
            "kind": r["kind"],
            "peer": r["chat"],
            "peer_id": peer_id,
            "peer_title": title,
            "topic_id": r["topic_id"],
            "scheduled_for": r["at"].astimezone(dt.UTC).isoformat(),
            "text_agent": r["text"],
        }
        t0 = time.perf_counter()
        while True:
            try:
                res = await client(
                    functions.messages.SendMessageRequest(
                        peer=entity,
                        message=r["text"],
                        random_id=random_id(key, r["seq"]),
                        schedule_date=r["at"],
                        reply_to=reply_to,
                        no_webpage=False,
                    )
                )
                msg_id = next(
                    (
                        u.message.id
                        for u in getattr(res, "updates", [])
                        if isinstance(u, types.UpdateNewScheduledMessage)
                    ),
                    None,
                )
                await store.record(
                    {
                        **base,
                        "status": "scheduled",
                        "tg_msg_id": msg_id,
                        "result": f"{round((time.perf_counter() - t0) * 1000)}ms",
                    }
                )
                break
            except FloodWaitError as e:
                logger.warning("batch %s: FloodWait %ss", key, e.seconds)
                await asyncio.sleep(e.seconds + 1)
            except RPCError as e:
                # RANDOM_ID_DUPLICATE: Telegram already accepted this very row before a crash
                status = (
                    "scheduled_earlier" if "RANDOM_ID_DUPLICATE" in str(e) else "failed"
                )
                await store.record(
                    {**base, "status": status, "result": type(e).__name__}
                )
                break
    for chat in {r["chat"] for r in todo}:
        _title, entity, _pid, _ = resolved[chat]
        try:
            await scheduled.fetch_chat(client, account, entity)
        except RPCError as e:
            logger.warning("batch %s: index refresh failed for %s: %s", key, chat, e)


def _op_rows(conn, account: str, key: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT seq, peer, peer_title, scheduled_for, kind, status, tg_msg_id, result, created_at "
            "FROM outbox WHERE account = ? AND operation_key = ? ORDER BY seq",
            (account, key),
        )
    ]


async def operation_status_impl(operation_id: str) -> dict[str, Any]:
    account = account_label()
    rows = await store.call(_op_rows, account, operation_id)
    task = _tasks.get(operation_id)
    running = bool(task and not task.done())
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "ok": True,
        "operation_id": operation_id,
        "account": account,
        "state": "running" if running else ("done" if rows else "unknown"),
        "summary": counts,
        "rows": rows,
        "as_of": store.now_iso(),
        "note": "scheduled_earlier = Telegram had already accepted this row; not a duplicate",
    }
