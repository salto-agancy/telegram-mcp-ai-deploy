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

from src.cloud_bot import telegram_api
from src.cloud_secure import digest, encode, unwrap, wrap

NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def verified_launch(init_data, credential, now=None):
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=30)
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError
    signature = fields.pop("hash", "")
    secret = hmac.digest(b"WebAppData", credential.encode(), "sha256")
    check = "\n".join(f"{name}={value}" for name, value in sorted(fields.items()))
    age = (int(time.time()) if now is None else now) - int(fields.get("auth_date", 0))
    if not 0 <= age <= 300 or not hmac.compare_digest(hmac.digest(secret, check.encode(), "sha256").hex(), signature):
        raise ValueError
    user = json.loads(fields.get("user", "{}"))["id"]
    if type(user) is not int or user <= 0:
        raise ValueError
    return user, fields.get("start_param", ""), signature, int(fields["auth_date"]) + 300


def telegram_identity(init_data, credential, now=None):
    return verified_launch(init_data, credential, now)[:2]


class MiniApp:
    def __init__(self, issuer, credential, slots, backends):
        self.issuer, self.credential, self.slots, self.backends = issuer, credential, slots, backends
        self.locks = {index: asyncio.Lock() for index in backends}
        self.consent = self.manage = None
        self.notified = set()

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

    def open_launch(self, request):
        now = int(time.time())
        user, start, signature, expires = verified_launch(
            request.headers.get("x-telegram-init-data", ""), self.credential, now
        )
        nonce = secrets.token_urlsafe(32)
        with self.slots.db(write=True) as db:
            db.execute("DELETE FROM cloud_launches WHERE expires < ?", (now,))
            created = db.execute(
                "INSERT OR IGNORE INTO cloud_launches SELECT ?,?,?,?,? "
                "WHERE EXISTS (SELECT 1 FROM cloud_slots WHERE user_id=?)",
                (digest(signature), digest(nonce), user, start, expires, user),
            )
            if created.rowcount != 1:
                raise ValueError
        return JSONResponse({"nonce": nonce}, headers=NO_STORE)

    async def api(self, request):
        action = request.path_params["action"]
        try:
            if request.headers.get("origin") != self.issuer:
                raise ValueError
            if action == "session":
                return self.open_launch(request)
            with self.slots.db() as db:
                launch = db.execute(
                    "SELECT user_id,start_param FROM cloud_launches WHERE nonce_hash=? AND expires>=?",
                    (digest(request.headers.get("x-cloud-nonce", "")), int(time.time())),
                ).fetchone()
            if launch is None:
                raise ValueError
            user, start_param = launch
            data = await request.json()
            slot = self.slots.lookup(user)["id"]
            async with self.locks[slot]:
                if self.slots.lookup(user)["id"] != slot:
                    raise ValueError
                key = self.key(user, data["proof"])
                result = await self.action(slot, key, user, start_param, action, data.get("value"))
        except (ValueError, KeyError, TypeError, InvalidTag):
            return JSONResponse({"error": "Откройте Mini App из бота и проверьте приглашение."}, status_code=403, headers=NO_STORE)
        except httpx.HTTPError:
            return JSONResponse({"error": "Сервис временно недоступен. Повторите позже."}, status_code=502, headers=NO_STORE)
        return JSONResponse({**result, "mcp_url": self.issuer + "/mcp"}, headers=NO_STORE)

    async def action(self, slot, key, user, start_param, action, value):
        management = {"api_key", "detach", "decision"}
        if action not in {"status", "phone", "code", "password", "chats", "edit", "delete"} | management:
            raise ValueError
        reply = await self.call(slot, key, "/setup", {"action": "status" if action in management else action, "user_id": user, "value": value})
        result = reply.json()
        if action == "delete" and result.get("step") == "deleted":
            if self.manage:
                self.manage(slot, user, key, action, value)
            else:
                with self.slots.db(write=True) as db:
                    db.execute("UPDATE cloud_slots SET user_id=NULL, wrapped=NULL WHERE user_id=?", (user,))
                    self.slots.clear_launches(db, user)
            self.notified.discard(user)
        if result.get("step") == "connected" and self.manage:
            if action == "api_key":
                value = "tgcloud_" + secrets.token_urlsafe(32)
                async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                    await telegram_api(client, self.credential, "sendMessage", {
                        "chat_id": user, "text": "Ваш личный API-ключ для MCP:\n" + value,
                    })
            if action == "decision":
                self.consent.decide(start_param, slot, user, key, value)
                result["decision_recorded"] = value
            result.update(self.manage(slot, user, key, action, value))
            result["oauth"] = self.consent.view(start_param)
        if action == "chats" and result.get("step") == "connected":
            self.notified.discard(user)
        return result

    async def notify_revoked(self, slot, reply):
        try:
            result = reply.json().get("result", {})
            if not result.get("isError") or "Сессия Telegram отозвана" not in str(result.get("content", [])):
                return
            with self.slots.db() as db:
                user = db.execute("SELECT user_id FROM cloud_slots WHERE id=?", (slot,)).fetchone()[0]
            if user and user not in self.notified:
                async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                    await telegram_api(client, self.credential, "sendMessage", {"chat_id": user, "text": "Подключите Telegram заново через Mini App."})
                self.notified.add(user)
        except (httpx.HTTPError, ValueError):
            pass

    def routes(self):
        return [Route("/app", self.page), Route("/app/api/{action}", self.api, methods=["POST"])]
