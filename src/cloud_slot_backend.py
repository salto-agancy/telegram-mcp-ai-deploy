"""Read-only Telegram tools for a slot whose key arrives with each request."""

import asyncio
import json
import re
from base64 import b64decode, b64encode
from typing import Annotated

from cryptography.exceptions import InvalidTag
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from telethon import utils
from telethon.errors import (
    FloodWaitError,
    RPCError,
    SessionPasswordNeededError,
    UnauthorizedError,
)
from telethon.extensions import BinaryReader

from src.cloud_secure import SessionFile, decode
from src.cloud_setup import TelegramChat


class SlotTelegram(TelegramChat):
    def __init__(self, path, slot):
        super().__init__(path)
        self.encrypted = SessionFile(path, slot)

    def state(self, key):
        return json.loads(self.encrypted.read(key))

    async def choices(self, client):
        dialogs = await client.get_dialogs()
        return {
            str(d.id): (d.name or str(d.id), b64encode(bytes(utils.get_input_peer(d.input_entity))).decode())
            for d in dialogs if d.input_entity
        }

    async def configuration(self, request):
        data = await request.json()
        action, user = data["action"], data["user_id"]
        value = data.get("value")
        try:
            key = decode(request.headers.get("x-cloud-session-key", ""))
            if len(key) != 32:
                return Response(status_code=403)
            async with self.lock:
                return JSONResponse(await self.configure(key, user, action, value))
        except UnauthorizedError:
            return JSONResponse({"error": "Подключите Telegram заново."}, status_code=409)
        except (ValueError, InvalidTag, OSError, RPCError):
            return JSONResponse({"error": "Проверьте введённые данные и повторите действие."}, status_code=400)

    async def configure(self, key, user, action, value):
        state = self.state(key) if self.encrypted.path.exists() else None
        pending = self.pending if self.pending and self.pending["user"] == user else None
        if state and state["user"] != user:
            raise ValueError
        if action == "status":
            if pending:
                result = {"step": pending["step"]}
                if pending["step"] == "chats":
                    result["choices"] = [(i, c[0]) for i, c in pending["choices"].items()]
                return result
            return {"step": "connected" if state else "phone"}
        if action == "delete":
            if state:
                try:
                    if not await self.run(state, lambda client: client.log_out()):
                        raise ValueError
                except UnauthorizedError:
                    pass
            self.encrypted.delete()
            self.pending = None
            return {"step": "deleted"}
        if action == "edit" and state:
            pending = {"user": user, "step": "chats", "session": state["session"],
                       "choices": await self.run(state, self.choices)}
            self.pending = pending
            asyncio.get_running_loop().call_later(300, self.expire, pending)
            return {"step": "chats", "choices": [(i, c[0]) for i, c in pending["choices"].items()],
                    "selected": list(state["peers"])}
        if action == "chats" and pending and pending["step"] == action:
            choices = pending["choices"]
            if not isinstance(value, list) or not value or any(i not in choices for i in value):
                raise ValueError
            state = {"user": user, "session": pending["session"], "peers": {i: choices[i][1] for i in value}}
            self.encrypted.write(key, json.dumps(state).encode())
            self.pending = None
            return {"step": "connected"}
        if action == "phone" and isinstance(value, str) and re.fullmatch(r"\+[1-9][0-9]{6,14}", value):
            pending = {"user": user, "client": self.client(), "phone": value, "step": "phone"}
            self.pending = pending
            asyncio.get_running_loop().call_later(300, self.expire, pending)
        if not pending or pending["step"] != action or action not in {"phone", "code", "password"}:
            raise ValueError
        return await self.sign_in(pending, action, value)

    async def sign_in(self, pending, action, value):
        client = pending["client"]
        try:
            await client.connect()
            if action == "phone":
                sent = await client.send_code_request(value)
                pending.update(phone_hash=sent.phone_code_hash, step="code")
                return {"step": "code"}
            if action == "code":
                await client.sign_in(phone=pending["phone"], code=value, phone_code_hash=pending["phone_hash"])
            else:
                await client.sign_in(password=value)
            if (await client.get_me()).id != pending["user"]:
                self.pending = None
                await client.log_out()
                return {"step": "phone", "error": "Введите номер аккаунта, который открыл Mini App."}
            choices = await self.choices(client)
            user = pending["user"]
            pending.clear()
            pending.update(user=user, session=client.session.save(), step="chats", choices=choices)
            return {"step": "chats", "choices": [(i, c[0]) for i, c in choices.items()]}
        except SessionPasswordNeededError:
            pending["step"] = "password"
            return {"step": "password"}
        finally:
            await client.disconnect()

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
    app.router.routes.append(Route("/setup", telegram.configuration, methods=["POST"]))
    return app
