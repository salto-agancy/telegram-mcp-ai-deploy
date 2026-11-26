"""Private Telegram setup endpoints and the pilot's one-chat data source."""

import asyncio
import base64
import json
import os
import re
from pathlib import Path

from starlette.responses import JSONResponse
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError
from telethon.extensions import BinaryReader
from telethon.sessions import StringSession

from src.oauth_proxy import _secret


class TelegramChat:
    def __init__(self, path):
        self.path = Path(path)
        self.pending = None
        self.lock = asyncio.Lock()

    def client(self, session=""):
        return TelegramClient(StringSession(session), int(os.environ["TELEGRAM_API_ID"]),
                              _secret("TELEGRAM_API_HASH"), flood_sleep_threshold=0,
                              request_retries=0, connection_retries=0, timeout=5)

    async def read(self, chat_id, query, limit, cursor):
        async with self.lock:
            if not self.path.exists():
                return None
            state = json.loads(self.path.read_text())
            if chat_id is not None and chat_id != state["chat"]:
                return None
            with BinaryReader(base64.b64decode(state["peer"])) as reader:
                peer = reader.tgread_object()
            client = self.client(state["session"])
            try:
                await client.connect()
                messages = await client.get_messages(peer, limit=limit, offset_id=cursor or 0,
                                                     search=query or None)
                return [{"id": m.id, "text": (m.message or "")[:1500]} for m in messages]
            finally:
                await client.disconnect()

    async def setup(self, request):
        data = await request.json()
        action = data["action"]
        async with self.lock:
            if action == "disconnect":
                self.pending = None
                self.path.with_suffix(".tmp").unlink(missing_ok=True)
                self.path.unlink(missing_ok=True)
                return JSONResponse({})
            if action == "phone":
                if not re.fullmatch(r"\+[1-9][0-9]{6,14}", data["value"]):
                    return JSONResponse({"step": "phone", "retry": True})
                pending = {"flow": data["flow"], "client": self.client(), "step": "phone"}
                self.pending = pending
                asyncio.get_running_loop().call_later(600, self.expire, pending)
            else:
                pending = self.pending
                if not pending or pending["flow"] != data["flow"] or pending["step"] != action:
                    return JSONResponse({"step": "phone", "retry": True})
            if action == "chat":
                selected = pending["choices"].get(data["value"])
                if selected is None:
                    return JSONResponse({"step": "chat", "choices": pending["titles"], "retry": True})
                client = pending["client"]
                state = {"session": client.session.save(), "chat": int(data["value"]),
                         "peer": base64.b64encode(bytes(selected)).decode()}
                temp = self.path.with_suffix(".tmp")
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as file:
                    json.dump(state, file)
                temp.replace(self.path)
                self.pending = None
                return JSONResponse({"step": "done"})

            client = pending["client"]
            try:
                await client.connect()
                if action == "phone":
                    sent = await client.send_code_request(data["value"])
                    pending.update(phone=data["value"], phone_hash=sent.phone_code_hash, step="code")
                    return JSONResponse({"step": "code"})
                if not await client.is_user_authorized():
                    if action == "code":
                        await client.sign_in(phone=pending["phone"], code=data["value"],
                                             phone_code_hash=pending["phone_hash"])
                    else:
                        await client.sign_in(password=data["value"])
                dialogs = await client.get_dialogs(limit=100)
                pending["choices"] = {str(d.id): utils.get_input_peer(d.input_entity) for d in dialogs}
                pending["titles"] = [[str(d.id), d.name or str(d.id)] for d in dialogs]
                pending["step"] = "chat"
                return JSONResponse({"step": "chat", "choices": pending["titles"]})
            except SessionPasswordNeededError:
                pending["step"] = "password"
                return JSONResponse({"step": "password"})
            except FloodWaitError:
                return JSONResponse({"step": action, "retry": True}, status_code=429)
            except (RPCError, OSError):
                return JSONResponse({"step": action, "retry": True})
            finally:
                await client.disconnect()

    def expire(self, pending):
        if self.pending is pending:
            self.pending = None
