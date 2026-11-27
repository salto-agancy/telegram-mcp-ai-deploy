"""Read-only Telegram tools for a slot whose key arrives with each request."""

import asyncio
import json
from base64 import b64decode
from typing import Annotated

from cryptography.exceptions import InvalidTag
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import Response
from starlette.routing import Route
from telethon.errors import FloodWaitError, RPCError, UnauthorizedError
from telethon.extensions import BinaryReader

from src.cloud_secure import SessionFile, decode
from src.cloud_setup import TelegramChat


class SlotTelegram(TelegramChat):
    def __init__(self, path, slot):
        super().__init__(path)
        self.encrypted = SessionFile(path, slot)

    def state(self, key):
        return json.loads(self.encrypted.read(key))

    async def run(self, state, call):
        for attempt in range(2):
            client = self.client(state["session"])
            try:
                await client.connect()
                return await call(client)
            except FloodWaitError as exc:
                if attempt or exc.seconds > 4:
                    raise
                await asyncio.sleep(exc.seconds)
            except (OSError, TimeoutError):
                if attempt:
                    raise
            finally:
                await client.disconnect()
        raise OSError("Telegram retry exhausted")

    async def list_chats(self, key):
        async with self.lock:
            state = self.state(key)
            allowed = state["peers"]

            async def names(client):
                return [{"chat_id": dialog.id, "title": dialog.name}
                        async for dialog in client.iter_dialogs() if str(dialog.id) in allowed]

            return await self.run(state, names)

    async def messages(self, key, chat_id, query, limit, cursor):
        async with self.lock:
            state = self.state(key)
            encoded = state["peers"].get(str(chat_id))
            if encoded is None:
                raise ToolError("Этот чат не разрешён. Измените доступ в Telegram.")
            with BinaryReader(b64decode(encoded)) as reader:
                peer = reader.tgread_object()

            async def read(client):
                rows = await client.get_messages(peer, limit=limit + 1, search=query or None, offset_id=cursor or 0)
                page = [{"id": row.id, "text": (row.message or "")[:1500]} for row in rows[:limit]]
                more = len(rows) > limit
                return {"messages": page, "has_more": more, "next_cursor": page[-1]["id"] if more else None}

            return await self.run(state, read)


def slot_backend(telegram):
    server = FastMCP("Telegram cloud", mask_error_details=True)
    readonly = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    async def invoke(call, *args):
        try:
            key = decode(get_http_headers().get("x-cloud-session-key", ""))
            if len(key) != 32:
                raise ValueError
            return await call(key, *args)
        except (InvalidTag, FileNotFoundError, ValueError):
            raise ToolError("Сессия заблокирована. Откройте Mini App и проверьте подключение.") from None
        except UnauthorizedError:
            raise ToolError("Сессия Telegram отозвана. Подключитесь заново через Mini App.") from None
        except (RPCError, OSError):
            raise ToolError("Telegram временно недоступен. Повторите запрос позже.") from None

    @server.tool(annotations=readonly)
    async def list_chats() -> dict:
        """List Telegram chats that the user has allowed this service to read."""
        return {"chats": await invoke(telegram.list_chats)}

    @server.tool(annotations=readonly)
    async def get_messages(
        chat_id: int,
        query: str = "",
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
        cursor: Annotated[int, Field(ge=1)] | None = None,
    ) -> dict:
        """Read or search one allowed chat. Pass next_cursor to read the next page."""
        return await invoke(telegram.messages, chat_id, query, limit, cursor)

    async def health(request):
        return Response(status_code=200)

    app = server.http_app(path="/v1/mcp", stateless_http=True, json_response=True)
    app.router.routes.append(Route("/health", health))
    return app
