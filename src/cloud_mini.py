"""Signed Mini App entry points shared with the OAuth facade."""

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from cryptography.exceptions import InvalidTag
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from src.cloud_secure import encode, unwrap, wrap

NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def telegram_identity(init_data, credential, now=None):
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=30)
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError
    signature = fields.pop("hash", "")
    secret = hmac.digest(b"WebAppData", credential.encode(), "sha256")
    check = "\n".join(f"{name}={value}" for name, value in sorted(fields.items()))
    age = (int(time.time()) if now is None else now) - int(fields.get("auth_date", 0))
    if not 0 <= age <= 3600 or not hmac.compare_digest(hmac.digest(secret, check.encode(), "sha256").hex(), signature):
        raise ValueError
    user = json.loads(fields.get("user", "{}"))["id"]
    if type(user) is not int or user <= 0:
        raise ValueError
    return user, fields.get("start_param", "")


class MiniApp:
    def __init__(self, issuer, credential, slots, backends):
        self.issuer, self.credential, self.slots, self.backends = issuer, credential, slots, backends
        self.locks = {index: asyncio.Lock() for index in backends}

    def key(self, user, proof):
        if not isinstance(proof, str) or not re.fullmatch(r"[0-9a-f]{64}", proof):
            raise ValueError
        with self.slots.db(write=True) as db:
            slot = db.execute("SELECT * FROM cloud_slots WHERE user_id=?", (user,)).fetchone()
            if slot is None:
                raise ValueError
            context = f"mini/{slot['id']}/{user}"
            if slot["wrapped"]:
                return unwrap(proof, slot["wrapped"], context)
            key = secrets.token_bytes(32)
            db.execute("UPDATE cloud_slots SET wrapped=? WHERE user_id=?", (wrap(proof, key, context), user))
            return key

    async def call(self, slot, key, path, payload, headers=None):
        route = self.backends[slot]
        headers = {**(headers or {}), "Authorization": "Bearer " + route["bearer"], "X-Cloud-Session-Key": encode(key)}
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            return await client.post(route["url"].removesuffix("/v1/mcp") + path, json=payload, headers=headers)

    async def page(self, request):
        html = Path(__file__).with_name("cloud_mini.html").read_text(encoding="utf-8")
        script = html.partition("<script>")[2].partition("</script>")[0]
        script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        csp = f"default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; script-src https://telegram.org 'sha256-{script_hash}'; base-uri 'none'"
        return HTMLResponse(html, headers={**NO_STORE, "Content-Security-Policy": csp})

    async def api(self, request):
        action = request.path_params["action"]
        try:
            if request.headers.get("origin") != self.issuer:
                raise ValueError
            user, start_param = telegram_identity(request.headers.get("x-telegram-init-data", ""), self.credential)
            data = await request.json()
            slot = self.slots.lookup(user)["id"]
            async with self.locks[slot]:
                key = self.key(user, data["proof"])
                result = await self.action(slot, key, user, start_param, action, data.get("value"))
        except (ValueError, KeyError, TypeError, InvalidTag):
            return JSONResponse({"error": "Откройте Mini App из бота и проверьте приглашение."}, status_code=403, headers=NO_STORE)
        except httpx.HTTPError:
            return JSONResponse({"error": "Сервис временно недоступен. Повторите позже."}, status_code=502, headers=NO_STORE)
        return JSONResponse({**result, "mcp_url": self.issuer + "/mcp"}, headers=NO_STORE)

    async def action(self, slot, key, user, start_param, action, value):
        if action not in {"status", "phone", "code", "password", "chats", "edit", "delete"}:
            raise ValueError
        reply = await self.call(slot, key, "/setup", {"action": action, "user_id": user, "value": value})
        result = reply.json()
        if action == "delete" and result.get("step") == "deleted":
            with self.slots.db(write=True) as db:
                db.execute("UPDATE cloud_slots SET user_id=NULL, wrapped=NULL WHERE user_id=?", (user,))
        return result

    def routes(self):
        return [Route("/app", self.page), Route("/app/api/{action}", self.api, methods=["POST"])]
