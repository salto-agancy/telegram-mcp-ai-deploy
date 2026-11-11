"""Test chats for write tests, created once and only with ENABLE_TEST_FIXTURES=true.

Write tests (batches of scheduled messages, the per-chat limit, retries) must not
run in chats with people or in the owner's Saved Messages. A basic group needs a
second member, so the owner's own bot is invited; the groups go to the archive.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from telethon.tl import functions, types

from src.client.connection import get_connected_client


def fixtures_enabled() -> bool:
    return (os.environ.get("ENABLE_TEST_FIXTURES") or "").lower() in (
        "1",
        "true",
        "yes",
    )


async def test_chats_create_impl(
    count: int, prefix: str, bot_username: str
) -> dict[str, Any]:
    if not fixtures_enabled():
        return {"ok": False, "error": "test fixtures are disabled on this server"}
    count = max(1, min(int(count), 20))
    client = await get_connected_client()
    existing = {}
    async for d in client.iter_dialogs(archived=True):
        if isinstance(d.entity, types.Chat) and (d.entity.title or "").startswith(
            prefix
        ):
            existing[d.entity.title] = d.entity.id
    bot = await client.get_input_entity(bot_username)
    created, kept = [], []
    for i in range(1, count + 1):
        title = f"{prefix}{i:02d}"
        if title in existing:
            kept.append({"title": title, "chat": f"chat:{existing[title]}"})
            continue
        res = await client(
            functions.messages.CreateChatRequest(users=[bot], title=title)
        )
        chat = next(c for c in res.updates.chats if isinstance(c, types.Chat))
        await client(
            functions.folders.EditPeerFoldersRequest(
                folder_peers=[
                    types.InputFolderPeer(
                        peer=types.InputPeerChat(chat.id), folder_id=1
                    )
                ]
            )
        )
        created.append({"title": title, "chat": f"chat:{chat.id}"})
        await asyncio.sleep(3)
    return {"ok": True, "created": created, "already_there": kept}
