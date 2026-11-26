"""Single-resource OAuth facade. Each grant routes to one configured backend."""

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
from src.oauth_proxy import _secret

NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, secret TEXT, redirects TEXT);
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY, client TEXT NOT NULL, user_id TEXT NOT NULL,
    invite TEXT UNIQUE, resource TEXT, redirect TEXT, challenge TEXT,
    code TEXT UNIQUE, deadline INTEGER, revoked INTEGER DEFAULT 0);
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
                or not route["bearer"] or not url.hostname or url.scheme not in {"http", "https"}
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
        db.executescript(SCHEMA)

    metadata = build_metadata(AnyHttpUrl(issuer), None,
                              ClientRegistrationOptions(enabled=True, valid_scopes=["mcp"]),
                              RevocationOptions(enabled=True)).model_dump(mode="json", exclude_none=True)
    metadata.update(token_endpoint_auth_methods_supported=["client_secret_post"],
                    revocation_endpoint_auth_methods_supported=["client_secret_post"])

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
            row = db.execute("SELECT redirects FROM clients WHERE id=?", (client,)).fetchone()
            if not row or redirect not in callbacks or redirect not in json.loads(row["redirects"]):
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
            user = next((u for u, r in routes.items() if hmac.compare_digest(r["invite_sha256"], invite_hash)), None)
            if user is None:
                raise OAuthError("access_denied", 403)
            code = secrets.token_urlsafe(32)
            try:
                db.execute("""INSERT INTO grants(client,user_id,invite,resource,redirect,challenge,code,deadline)
                              VALUES (?,?,?,?,?,?,?,?)""",
                           (client, user, invite_hash, resource, redirect, challenge, digest(code), time.time() + 120))
            except sqlite3.IntegrityError:
                raise OAuthError("access_denied", 403) from None
        return RedirectResponse(construct_redirect_uri(redirect, code=code, state=params.get("state")),
                                status_code=303, headers=NO_STORE)

    def client_auth(db, data):
        client = data.get("client_id")
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
        if not route:
            return Response(status_code=401, headers={**NO_STORE, "WWW-Authenticate":
                f'Bearer resource_metadata="{issuer}/.well-known/oauth-protected-resource/mcp"'})
        if request.url.query:
            raise OAuthError()
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        headers = {k: request.headers[k] for k in ("accept", "content-type", "mcp-protocol-version") if k in request.headers}
        headers["Authorization"] = "Bearer " + route["bearer"]
        async with httpx.AsyncClient(trust_env=False, timeout=15) as session:
            try:
                response = await session.post(route["url"], headers=headers, content=body)
            except httpx.RequestError:
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
        Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]),
    ])
    app = Starlette(routes=endpoints, exception_handlers={OAuthError: error})
    app.router.redirect_slashes = False
    return app


if __name__ == "__main__":
    serve(build_cloud_app(os.environ["PUBLIC_BASE_URL"].rstrip("/"),
                          set(os.environ["MCP_OAUTH_REDIRECT_URIS"].split(",")),
                          json.loads(_secret("CLOUD_ROUTES")), os.environ["OAUTH_DB_PATH"]))
