"""What the agents left, what the owner changed, what was actually sent.

The journal holds what each agent wrote. With refresh, open drafts of this account
are checked against Telegram now: still waiting, edited by the owner, sent (and how
much it was changed before sending), or cleared without sending. Rows of the other
account are reported as last checked by that account's own server.
"""

from __future__ import annotations

import datetime
from typing import Any

from telethon.tl import types

from src.client.connection import get_connected_client
from src.tools.outbox import store
from src.tools.outbox.drafts import _read_draft, account_label
from src.tools.outbox.peers import PeerRef
from src.tools.outbox.store import OPEN_DRAFT_STATES

REFRESH_LIMIT = 50
# An outgoing message counts as "this draft, sent" when it is at least this similar.
SENT_MATCH = 0.5

STATUS_TEXT = {
    "saved": "in the input field",
    "waiting": "in the input field, untouched",
    "already_there": "the same text was already in the input field",
    "edited": "the owner is editing it in the input field",
    "sent": "sent",
    "cleared": "removed from the input field, not sent",
    "conflict": "not written: the field held the owner's text",
    "readback_mismatch": "Telegram returned a different text",
    "superseded": "replaced by a later draft of an agent",
}


def _since(value: str | None) -> str:
    if value:
        return (
            datetime.datetime.fromisoformat(value).astimezone(datetime.UTC).isoformat()
        )
    return (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=7)
    ).isoformat()


def _rows(conn, since: str, agent: str | None, kind: str | None) -> list[dict]:
    sql, args = "SELECT * FROM outbox WHERE created_at >= ?", [since]
    if agent:
        sql, args = sql + " AND agent = ?", [*args, agent.lower()]
    if kind:
        sql, args = sql + " AND kind = ?", [*args, kind]
    return [
        dict(r)
        for r in conn.execute(
            sql + " ORDER BY created_at DESC, id DESC", args
        ).fetchall()
    ]


def _peer_ref(text: str) -> PeerRef:
    if text == "me":
        return PeerRef("me", "me")
    if text.startswith("@"):
        return PeerRef("username", text[1:])
    kind, _, value = text.partition(":")
    return PeerRef(kind, value)


async def _refresh_draft(client, row: dict) -> dict:
    entity = await client.get_input_entity(_peer_ref(row["peer"]).telethon_target())
    draft, _ = await _read_draft(client, entity, row["topic_id"])
    if draft and draft["text"] == row["text_agent"]:
        return {
            "status": "waiting",
            "text_now": draft["text"],
            "version_now": draft["version"],
            "edit_ratio": 0.0,
        }
    if draft:
        return {
            "status": "edited",
            "text_now": draft["text"],
            "version_now": draft["version"],
            "edit_ratio": store.edit_ratio(row["text_agent"], draft["text"]),
        }
    created = datetime.datetime.fromisoformat(row["created_at"])
    best, best_score = None, 0.0
    async for msg in client.iter_messages(
        entity, limit=30, reply_to=row["topic_id"] or None
    ):
        if msg.date and msg.date < created:
            break
        if not msg.out or not isinstance(msg, types.Message) or not msg.message:
            continue
        score = 1.0 - store.edit_ratio(row["text_agent"], msg.message)
        if score > best_score:
            best, best_score = msg, score
    if best is not None and best_score >= SENT_MATCH:
        return {
            "status": "sent",
            "text_now": best.message,
            "version_now": store.version(best.message),
            "edit_ratio": round(1.0 - best_score, 3),
            "sent_msg_id": best.id,
            "sent_at": best.date.isoformat(),
        }
    return {"status": "cleared", "text_now": None, "version_now": None}


async def outbox_report_impl(
    since: str | None = None,
    agent: str | None = None,
    kind: str | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    since_iso = _since(since)
    rows = await store.call(_rows, since_iso, agent, kind)
    account = account_label()
    refreshed, errors = 0, []
    if refresh:
        client = await get_connected_client()
        for row in rows:
            if refreshed >= REFRESH_LIMIT:
                break
            if (
                row["account"] != account
                or row["kind"] != "draft"
                or row["status"] not in OPEN_DRAFT_STATES
            ):
                continue
            try:
                upd = await _refresh_draft(client, row)
            except Exception as e:  # one unreadable chat must not hide the rest
                errors.append(
                    {"outbox_id": row["id"], "error": f"{type(e).__name__}: {e}"}
                )
                continue
            upd["checked_at"] = store.now_iso()
            if upd["status"] != row["status"] or upd.get("text_now") != row.get(
                "text_now"
            ):
                await store.add_event(
                    row["id"],
                    upd["status"],
                    {"edit_ratio": upd.get("edit_ratio")},
                    **upd,
                )
            else:
                await store.add_event(
                    row["id"], "checked", {}, checked_at=upd["checked_at"]
                )
            row.update(upd)
            refreshed += 1

    items, summary = [], {}
    for r in rows:
        summary.setdefault(r["agent"], {}).setdefault(r["status"], 0)
        summary[r["agent"]][r["status"]] += 1
        items.append(
            {
                "outbox_id": r["id"],
                "created_at": r["created_at"],
                "agent": r["agent"],
                "account": r["account"],
                "kind": r["kind"],
                "chat": r["peer"],
                "title": r["peer_title"],
                "topic_id": r["topic_id"],
                "status": r["status"],
                "status_text": STATUS_TEXT.get(r["status"], r["status"]),
                "text_agent": r["text_agent"],
                "text_now": r["text_now"] if r["text_now"] != r["text_agent"] else None,
                "edit_ratio": r["edit_ratio"],
                "sent_msg_id": r["sent_msg_id"],
                "sent_at": r["sent_at"],
                "checked_at": r["checked_at"],
            }
        )
    return {
        "ok": True,
        "since": since_iso,
        "account_checked_live": account if refresh else None,
        "refreshed": refreshed,
        "refresh_errors": errors,
        "summary_by_agent": summary,
        "items": items,
        "as_of": store.now_iso(),
        "note": "rows of the other account are shown as last checked by its own server",
    }
