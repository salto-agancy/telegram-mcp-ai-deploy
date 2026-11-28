"""HTTP entry points for the isolated cloud pilot processes."""

import hmac
import logging
import os

import uvicorn
from mcp.server.transport_security import RequestBodyLimitMiddleware
from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, Response


def serve(app, *, internal_bearer=None):
    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)

        async def status_only(message):
            if message["type"] == "http.response.start":
                print(f"http_status={message['status']}", flush=True)
            await send(message)

        if (scope["path"], scope["method"]) == ("/app", "GET"):
            scope = {**scope, "query_string": b""}
        query_allowed = scope["path"] == "/mcp" or (scope["path"], scope["method"]) == (
            "/authorize",
            "GET",
        )
        if scope["query_string"] and not query_allowed:
            return await Response(status_code=400)(scope, receive, status_only)
        if internal_bearer is not None:
            auth = Headers(scope=scope).get("authorization", "").encode()
            if not hmac.compare_digest(auth, f"Bearer {internal_bearer}".encode()):
                return await Response(status_code=401)(scope, receive, status_only)
        return await app(scope, receive, status_only)

    limited = RequestBodyLimitMiddleware(guarded, 65536)

    async def app_error_page(scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/app":
            return await limited(scope, receive, send)
        started = replaced = False

        async def readable(message):
            nonlocal started, replaced
            if message["type"] == "http.response.start":
                started = True
                if message["status"] >= 400:
                    replaced = True
                    headers = {k.decode(): v.decode() for k, v in message.get("headers", [])
                               if k.lower() in {b"allow", b"retry-after"}}
                    headers["Cache-Control"] = "no-store"
                    page = HTMLResponse("<!doctype html><html lang=ru><meta charset=utf-8>"
                                        "<meta name=viewport content='width=device-width'>"
                                        "<title>Ошибка подключения</title><h1>Не удалось открыть приложение</h1>"  # noqa: RUF001
                                        "<p>Закройте окно и откройте Mini App через бота ещё раз.</p></html>",
                                        status_code=message["status"], headers=headers)
                    return await page(scope, receive, send)
            if not replaced:
                await send(message)
            return None

        try:
            await limited(scope, receive, readable)
        except Exception:
            if started:
                raise
            await HTMLResponse("<!doctype html><meta charset=utf-8><h1>Не удалось открыть приложение</h1>"  # noqa: RUF001
                               "<p>Попробуйте ещё раз позже.</p>", status_code=500,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)

    # Third-party diagnostic and access loggers can include URLs and tool arguments.
    logging.disable(logging.CRITICAL)
    uvicorn.run(
        app_error_page,
        log_config=None,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        host=os.environ.get("BIND_HOST", "127.0.0.1"),
        port=int(os.environ["BIND_PORT"]),
    )
