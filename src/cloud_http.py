"""HTTP entry points for the isolated cloud pilot processes."""

import hmac
import logging
import os

import uvicorn
from mcp.server.transport_security import RequestBodyLimitMiddleware
from starlette.datastructures import Headers
from starlette.responses import Response


def serve(app, *, internal_bearer=None):
    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)

        async def status_only(message):
            if message["type"] == "http.response.start":
                print(f"http_status={message['status']}", flush=True)
            await send(message)

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

    # Third-party diagnostic and access loggers can include URLs and tool arguments.
    logging.disable(logging.CRITICAL)
    uvicorn.run(
        RequestBodyLimitMiddleware(guarded, 65536),
        log_config=None,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        host=os.environ.get("BIND_HOST", "127.0.0.1"),
        port=int(os.environ["BIND_PORT"]),
    )
