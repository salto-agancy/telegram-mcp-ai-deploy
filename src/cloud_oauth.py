"""Single-resource OAuth facade. Each grant routes to one configured backend."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
from mcp.server.auth.provider import construct_redirect_uri
from mcp.server.auth.routes import build_metadata, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from src.cloud_http import serve
from src.cloud_limits import PilotLimits
from src.oauth_proxy import _secret

PERPLEXITY_CLIENT_ID = "https://www.perplexity.ai/.well-known/mcp-client.json"
# Pin only the two production callbacks approved for the pilot.
PERPLEXITY_CALLBACKS = {
    "https://www.perplexity.ai/rest/connections/oauth_callback",
    "https://enterprise.perplexity.ai/rest/connections/oauth_callback",
}

NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, secret TEXT, redirects TEXT);
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY, client TEXT NOT NULL, user_id TEXT NOT NULL,
    invite TEXT, resource TEXT, redirect TEXT, challenge TEXT,
    code TEXT UNIQUE, deadline INTEGER, revoked INTEGER DEFAULT 0, manage TEXT);
CREATE TABLE IF NOT EXISTS tokens (
    digest TEXT PRIMARY KEY, grant_id INTEGER NOT NULL REFERENCES grants(id),
    kind TEXT, expires INTEGER);
"""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class OAuthError(Exception):
    def __init__(self, error="invalid_request", status=400):
        self.error, self.status = error, status


async def form(request):
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise OAuthError()
    pairs = parse_qsl((await request.body()).decode("latin-1"), keep_blank_values=True)
    if len(dict(pairs)) != len(pairs):
        raise OAuthError()
    return dict(pairs)


def build_cloud_app(issuer, callbacks, routes, db_path):
    origin = urlsplit(issuer)
    if (issuer != f"https://{origin.netloc}" or not origin.hostname
            or origin.username is not None):
        raise ValueError("Expected a public HTTPS origin")
    allowed = [urlsplit(uri) for uri in callbacks]
    if not allowed or any(u.scheme != "https" or not u.hostname or u.username is not None or u.fragment
                          for u in allowed):
        raise ValueError("Expected exact HTTPS callback URLs")
    for user, route in routes.items():
        url = urlsplit(route["url"])
        if (not user or not re.fullmatch(r"[0-9a-f]{64}", route["invite_sha256"])
                or type(route["expires"]) is not int or not route["bearer"] or not url.hostname or url.scheme not in {"http", "https"}
                or url.username is not None or url.query or url.fragment or url.path != "/v1/mcp"):
            raise ValueError("Invalid pilot route")
    for field in ("invite_sha256", "url", "bearer"):
        if len({r[field] for r in routes.values()}) != len(routes):
            raise ValueError("Each participant needs a distinct route and credentials")
    resource = issuer + "/mcp"

    @contextmanager
    def database(*, write=False):
        with closing(sqlite3.connect(db_path)) as db, db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    Path(db_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(db_path).touch(mode=0o600, exist_ok=True)
    with database() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(grants)")}
        if columns and "manage" not in columns:
            db.executescript(
                "BEGIN IMMEDIATE; ALTER TABLE grants RENAME TO old_grants;"
                + SCHEMA.split("CREATE TABLE IF NOT EXISTS tokens")[0]
                + "INSERT INTO grants SELECT *, NULL FROM old_grants; DROP TABLE old_grants;"
                  "ALTER TABLE tokens RENAME TO old_tokens;"
                + SCHEMA
                + "INSERT INTO tokens SELECT * FROM old_tokens; DROP TABLE old_tokens; COMMIT;"
            )
    with database() as db:
        db.executescript(SCHEMA)

    perplexity_callbacks = PERPLEXITY_CALLBACKS.intersection(callbacks)
    flows = {}
    locks = {user: asyncio.Lock() for user in routes}

    metadata = build_metadata(AnyHttpUrl(issuer), None,
                              ClientRegistrationOptions(enabled=True, valid_scopes=["mcp"]),
                              RevocationOptions(enabled=True)).model_dump(mode="json", exclude_none=True)
    auth_methods = ["client_secret_post"]
    if perplexity_callbacks:
        auth_methods.append("none")
        metadata["client_id_metadata_document_supported"] = True
    metadata.update(token_endpoint_auth_methods_supported=auth_methods,
                    revocation_endpoint_auth_methods_supported=auth_methods)

    async def discovery(request):
        return JSONResponse(metadata)

    grants_supported = ["authorization_code", "refresh_token"]

    async def register(request):
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise OAuthError("invalid_client_metadata") from None
        if not isinstance(data, dict):
            raise OAuthError("invalid_client_metadata")
        uris = data.get("redirect_uris")
        if not isinstance(uris, list) or not uris or any(not isinstance(u, str) or u not in callbacks for u in uris):
            raise OAuthError("invalid_redirect_uri")
        if data.get("token_endpoint_auth_method", "none") not in ("none", "client_secret_post"):
            raise OAuthError("invalid_client_metadata")
        grants = data.get("grant_types", ["authorization_code"])
        if (data.get("scope", "mcp") != "mcp" or data.get("response_types", ["code"]) != ["code"]
                or grants not in (["authorization_code"], grants_supported, grants_supported[::-1])):
            raise OAuthError("invalid_client_metadata")
        client, secret = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        with database(write=True) as db:
            db.execute("INSERT INTO clients VALUES (?,?,?)", (client, digest(secret), json.dumps(uris)))
        registration = {
            "client_id": client, "client_secret": secret, "redirect_uris": uris,
            "token_endpoint_auth_method": "client_secret_post", "scope": "mcp",
            "grant_types": grants_supported, "response_types": ["code"],
        }
        return JSONResponse(registration, status_code=201, headers=NO_STORE)

    async def authorize(request: Request):
        if request.method == "GET":
            params = dict(request.query_params)
            if len(params) != len(request.query_params.multi_items()):
                raise OAuthError()
        else:
            if request.headers.get("origin") != issuer or request.url.query:
                raise OAuthError("access_denied", 403)
            params = await form(request)
        invite = params.pop("invite", "") if request.method == "POST" else ""
        client, redirect = params.get("client_id"), params.get("redirect_uri")
        challenge = params.get("code_challenge", "")
        fields = {"response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method",
                  "resource", "scope", "state"}
        if (params.keys() - fields or params.get("response_type") != "code"
            or params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge)
            or params.get("resource", resource) != resource or params.get("scope", "mcp") != "mcp"):
            raise OAuthError()
        with database(write=request.method == "POST") as db:
            if client == PERPLEXITY_CLIENT_ID:
                valid_redirect = redirect in perplexity_callbacks
            else:
                row = db.execute("SELECT redirects FROM clients WHERE id=?", (client,)).fetchone()
                valid_redirect = row and redirect in callbacks and redirect in json.loads(row["redirects"])
            if not valid_redirect:
                raise OAuthError()
            if request.method == "GET":
                hidden = "".join(f'<input type="hidden" name="{k}" value="{escape(v)}">' for k, v in params.items())
                html = ('<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">'
                        '<h1>Доступ к тестовому чату</h1><form method=post action="/authorize">'
                        + hidden + '<label>Приглашение <input type=password name=invite required></label>'
                        '<button>Разрешить чтение</button></form>')
                return HTMLResponse(html, headers={
                    **NO_STORE, "Referrer-Policy": "same-origin",
                    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                })
            invite_hash = digest(invite)
            user = next((u for u, r in routes.items()
                         if r["expires"] > time.time() and hmac.compare_digest(r["invite_sha256"], invite_hash)), None)
            if user is None:
                raise OAuthError("access_denied", 403)
        async with locks[user]:
            if routes[user].get("telegram"):
                flow = secrets.token_urlsafe(32)
                flows[user] = (flow, params, invite_hash, time.monotonic() + 600)
                return setup_page(flow, {"step": "phone"})
            return grant_user(user, params, invite_hash)

    def grant_user(user, params, invite_hash, manage=None):
        code = secrets.token_urlsafe(32)
        with database(write=True) as db:
            db.execute("UPDATE grants SET revoked=1, manage=NULL WHERE user_id=?", (user,))
            db.execute("""INSERT INTO grants(client,user_id,invite,resource,redirect,challenge,code,deadline,manage)
                          VALUES (?,?,?,?,?,?,?,?,?)""",
                       (params["client_id"], user, invite_hash, resource, params["redirect_uri"],
                        params["code_challenge"], digest(code), time.time() + 120, digest(manage) if manage else None))
        result = RedirectResponse(construct_redirect_uri(params["redirect_uri"], code=code, state=params.get("state")),
                                  status_code=303, headers=NO_STORE)
        if manage:
            result.set_cookie("__Secure-pilot", manage, secure=True, httponly=True, samesite="strict", path="/setup")
        return result

    def setup_page(flow="", data=None):
        data = data or {}
        step = data.get("step", "disconnect")
        labels = {"phone": "Номер Telegram в формате +...", "code": "Код из приложения Telegram",
                  "password": "Пароль двухэтапной проверки", "chat": "Разрешить чтение одного тестового чата",
                  "disconnect": "Отключить Telegram и удалить сессию"}
        if step == "chat":
            options = "".join(f'<option value="{escape(key)}">{escape(title)}</option>'
                              for key, title in data.get("choices", []))
            field = f'<select name=value required><option value="">Выбери чат</option>{options}</select>'
        elif step == "disconnect":
            field = ""
        else:
            kind = {"phone": "tel", "code": "text", "password": "password"}[step]
            field = f'<input name=value type={kind} autocomplete=off required>'
        retry = "<p>Попробуй ещё раз.</p>" if data.get("retry") else ""
        button = "Отключить" if step == "disconnect" else "Продолжить"
        if step == "chat":
            button = "Разрешить чтение"
        return HTMLResponse(
            f'<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">'
            f'<h1>Telegram MCP · Подключение</h1>{retry}<form method=post action=/setup>'
            f'<input type=hidden name=flow value="{flow}"><input type=hidden name=action value="{step}">'
            f'<label>{labels[step]} {field}</label><button>{button}</button></form>',
            headers={**NO_STORE, "Referrer-Policy": "same-origin",
                     "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"},
        )

    async def setup(request):
        if request.method == "GET":
            return setup_page()
        if request.headers.get("origin") != issuer:
            raise OAuthError("access_denied", 403)
        data = await form(request)
        action = data.get("action")
        flow = data.get("flow", "")
        if action == "disconnect":
            manage = request.cookies.get("__Secure-pilot")
            with database() as db:
                record = db.execute("SELECT user_id FROM grants WHERE manage=?",
                                    (digest(manage or ""),)).fetchone()
            user = record["user_id"] if record else None
        else:
            user = next((u for u, f in flows.items() if f[0] == flow), None)
        if user not in locks:
            raise OAuthError("access_denied", 403)
        async with locks[user]:
            if action == "disconnect":
                with database(write=True) as db:
                    if not db.execute("SELECT 1 FROM grants WHERE user_id=? AND manage=?",
                                      (user, digest(manage))).fetchone():
                        raise OAuthError("access_denied", 403)
                    db.execute("UPDATE grants SET revoked=1 WHERE user_id=?", (user,))
                flows.pop(user, None)
            else:
                pending = flows.get(user)
                if (not pending or pending[0] != flow or pending[3] <= time.monotonic()
                        or routes[user]["expires"] <= time.time()
                        or action not in {"phone", "code", "password", "chat"}):
                    raise OAuthError("access_denied", 403)
            if action == "chat":
                with database(write=True) as db:
                    db.execute("UPDATE grants SET revoked=1 WHERE user_id=?", (user,))
            route = routes[user]
            async with httpx.AsyncClient(trust_env=False, timeout=30) as session:
                try:
                    reply = await session.post(route["url"].removesuffix("/v1/mcp") + "/setup",
                                               headers={"Authorization": "Bearer " + route["bearer"]},
                                               json={"action": action, "flow": flow, "value": data.get("value", "")})
                except httpx.RequestError:
                    return Response(status_code=502)
            if reply.status_code == 429:
                return Response(status_code=429, headers={"Retry-After": "60"})
            if reply.status_code != 200:
                return Response(status_code=502)
            if action == "disconnect":
                with database(write=True) as db:
                    db.execute("DELETE FROM tokens WHERE grant_id IN (SELECT id FROM grants WHERE user_id=?)", (user,))
                    db.execute("DELETE FROM grants WHERE user_id=?", (user,))
                result = HTMLResponse("<p>Telegram отключён.</p>", headers=NO_STORE)
                result.delete_cookie("__Secure-pilot", path="/setup", secure=True)
                return result
            if reply.json().get("step") == "done":
                flows.pop(user)
                return grant_user(user, pending[1], pending[2], secrets.token_urlsafe(32))
            return setup_page(flow, reply.json())

    def client_auth(db, data):
        client = data.get("client_id")
        if client == PERPLEXITY_CLIENT_ID and perplexity_callbacks and not data.get("client_secret"):
            return client
        row = db.execute("SELECT secret FROM clients WHERE id=?", (client,)).fetchone()
        if not row or not hmac.compare_digest(row["secret"], digest(data.get("client_secret", ""))):
            raise OAuthError("invalid_client", 401)
        return client

    def token_grant(db, raw, kind):
        return db.execute("""SELECT g.* FROM tokens t JOIN grants g ON g.id=t.grant_id
                             WHERE t.digest=? AND t.kind=? AND t.expires>? AND g.revoked=0 AND g.resource=?""",
                          (digest(raw), kind, time.time(), resource)).fetchone()

    async def token(request):
        data = await form(request)
        with database(write=True) as db:
            client = client_auth(db, data)
            if data.get("resource", resource) != resource:
                raise OAuthError("invalid_target")
            if data.get("scope", "mcp") != "mcp":
                raise OAuthError("invalid_scope")
            if data.get("grant_type") == "authorization_code":
                grant = db.execute("""SELECT * FROM grants WHERE code=? AND client=? AND resource=?
                                      AND deadline>? AND revoked=0""",
                                   (digest(data.get("code", "")), client, resource, time.time())).fetchone()
                verifier = data.get("code_verifier", "")
                if (not grant or data.get("redirect_uri") != grant["redirect"]
                        or (client == PERPLEXITY_CLIENT_ID and grant["redirect"] not in perplexity_callbacks)
                        or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)):
                    raise OAuthError("invalid_grant")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if not hmac.compare_digest(challenge, grant["challenge"]):
                    raise OAuthError("invalid_grant")
                db.execute("UPDATE grants SET code=NULL WHERE id=?", (grant["id"],))
            elif data.get("grant_type") == "refresh_token":
                grant = token_grant(db, data.get("refresh_token", ""), "refresh_token")
                if not grant or grant["client"] != client:
                    raise OAuthError("invalid_grant")
                db.execute("UPDATE tokens SET expires=0 WHERE grant_id=? AND expires>0", (grant["id"],))
            else:
                raise OAuthError("unsupported_grant_type")
            result = {"token_type": "Bearer", "scope": "mcp", "expires_in": 3600}
            for kind, ttl in (("access_token", 3600), ("refresh_token", 30 * 86400)):
                raw = secrets.token_urlsafe(32)
                db.execute("INSERT INTO tokens VALUES (?,?,?,?)", (digest(raw), grant["id"], kind, time.time() + ttl))
                result[kind] = raw
        return JSONResponse(result, headers=NO_STORE)

    async def revoke(request):
        data = await form(request)
        with database(write=True) as db:
            client = client_auth(db, data)
            db.execute("""UPDATE grants SET revoked=1 WHERE client=?
                          AND id=(SELECT grant_id FROM tokens WHERE digest=?)""",
                       (client, digest(data.get("token", ""))))
        return Response(status_code=200, headers=NO_STORE)

    async def mcp(request):
        body = await request.body()
        scheme, _, raw = request.headers.get("authorization", "").partition(" ")
        with database() as db:
            grant = token_grant(db, raw, "access_token") if scheme.lower() == "bearer" else None
        route = routes.get(grant["user_id"]) if grant else None
        unauthorized = {**NO_STORE, "WWW-Authenticate":
                        f'Bearer resource_metadata="{issuer}/.well-known/oauth-protected-resource/mcp"'}
        if not route:
            return Response(status_code=401, headers=unauthorized)
        if request.url.query:
            raise OAuthError()
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        async with locks[grant["user_id"]]:
            with database() as db:
                if not token_grant(db, raw, "access_token"):
                    return Response(status_code=401, headers=unauthorized)
            headers = {k: request.headers[k] for k in ("accept", "content-type", "mcp-protocol-version") if k in request.headers}
            headers["Authorization"] = "Bearer " + route["bearer"]
            async with httpx.AsyncClient(trust_env=False, timeout=15) as session:
                try:
                    response = await session.post(route["url"], headers=headers, content=body)
                except httpx.RequestError:
                    return Response(status_code=502)
        if response.status_code in (401, 403):
            return Response(status_code=502)
        if response.status_code >= 300:
            return Response(status_code=response.status_code if response.is_client_error else 502)
        return Response(response.content, status_code=response.status_code,
                        headers={**NO_STORE, "Content-Type": response.headers.get("content-type", "application/json")})

    async def error(request, exc):
        return JSONResponse({"error": exc.error}, status_code=exc.status, headers=NO_STORE)

    endpoints = create_protected_resource_routes(AnyHttpUrl(resource), [AnyHttpUrl(issuer)], ["mcp"])
    endpoints.extend([
        Route("/.well-known/oauth-authorization-server", discovery),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token, methods=["POST"]),
        Route("/revoke", revoke, methods=["POST"]),
        Route("/setup", setup, methods=["GET", "POST"]),
        Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]),
    ])
    app = Starlette(routes=endpoints, exception_handlers={OAuthError: error})
    app.router.redirect_slashes = False
    return PilotLimits(app)


if __name__ == "__main__":
    serve(build_cloud_app(os.environ["PUBLIC_BASE_URL"].rstrip("/"),
                          set(os.environ["MCP_OAUTH_REDIRECT_URIS"].split(",")),
                          json.loads(_secret("CLOUD_ROUTES")), os.environ["OAUTH_DB_PATH"]))
