"""Ten-minute, browser-bound authorization requests confirmed in Telegram."""

import asyncio
import hmac
import secrets
from html import escape
from urllib.parse import urlsplit

from mcp.server.auth.provider import construct_redirect_uri
from starlette.responses import HTMLResponse, RedirectResponse

from src.cloud_mini import NO_STORE
from src.cloud_secure import digest


class Consent:
    cookie = "__Host-cloud-browser"

    def __init__(self, username, mini, grant):
        self.username, self.mini, self.grant = username, mini, grant
        self.requests = {}

    def start(self, browser, params):
        if len(self.requests) >= 1024:
            return self.error("Сервис занят. Повторите подключение позже.", 429)
        browser = browser or secrets.token_urlsafe(32)
        rid = secrets.token_urlsafe(24)
        data = {"browser": digest(browser), "params": params, "code": f"{secrets.randbelow(1000):03d}",
                "domain": urlsplit(params["redirect_uri"]).netloc}
        self.requests[rid] = data
        asyncio.get_running_loop().call_later(600, self.requests.pop, rid, None)
        result = self.waiting(rid, data)
        result.set_cookie(self.cookie, browser, secure=True, httponly=True, samesite="lax", path="/", max_age=600)
        return result

    def waiting(self, rid, data):
        return HTMLResponse(
            '<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">'
            f'<meta http-equiv=refresh content="2;url=/authorize/status/{rid}">'
            f'<h1>{escape(data["domain"])}</h1><p>Код подтверждения <strong>{data["code"]}</strong></p>'
            f'<a href="https://t.me/{escape(self.username)}?startapp={rid}">Подтвердить в Telegram</a>'
            '<p>Сверьте домен и код. После подтверждения это окно вернёт вас в приложение.</p>',
            headers={**NO_STORE, "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"},
        )

    @staticmethod
    def error(message, status):
        return HTMLResponse('<!doctype html><meta charset=utf-8><p>' + escape(message) + '</p>', status, headers=NO_STORE)

    def view(self, rid):
        data = self.requests.get(rid)
        if data and "decision" not in data:
            return {"domain": data["domain"], "code": data["code"]}
        return None

    def decide(self, rid, slot, user, key, allowed):
        data = self.requests.get(rid)
        member = self.mini.slots.lookup(user)
        if not self.view(rid) or member is None or member["id"] != slot or type(allowed) is not bool:
            raise ValueError
        data["decision"] = (slot, user, member["wrapped"], key) if allowed else None

    async def poll(self, request):
        rid = request.path_params["flow"]
        data = self.requests.get(rid)
        if not data or not hmac.compare_digest(data["browser"], digest(request.cookies.get(self.cookie, ""))):
            return self.error("Запрос недействителен. Начните подключение заново в том же окне.", 403)
        if "decision" not in data:
            return self.waiting(rid, data)
        self.requests.pop(rid)
        params, decision = data["params"], data["decision"]
        if decision:
            slot, user, wrapped, key = decision
            member = self.mini.slots.lookup(user)
            if member and member["id"] == slot and member["wrapped"] == wrapped:
                return self.grant(str(slot), params, None, key=key)
        return RedirectResponse(construct_redirect_uri(params["redirect_uri"], error="access_denied", state=params.get("state")), 303, headers=NO_STORE)
