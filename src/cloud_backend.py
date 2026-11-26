"""One fictional chat per process. No Telegram credentials or network client."""

import os
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.routing import Route
from telethon.errors import RPCError

from src.cloud_http import serve
from src.cloud_setup import TelegramChat
from src.oauth_proxy import _secret


def fixture_backend(allowed_chat: int | None, telegram=None):
    server = FastMCP("Telegram cloud pilot", mask_error_details=True)
    messages = [
        {"id": n, "text": f"Маяк {allowed_chat}, сигнал {n}."} for n in range(6, 0, -1)
    ]

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def get_messages(
        chat_id: int | None = None,
        query: str = "",
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
        cursor: Annotated[int, Field(ge=1)] | None = None,
    ) -> dict:
        """Read or search the selected chat. Pass next_cursor to read the next page."""
        if telegram is not None:
            try:
                matches = await telegram.read(chat_id, query, limit + 1, cursor)
            except (RPCError, OSError, ValueError):
                raise ToolError("Chat unavailable") from None
            if matches is None:
                raise ToolError("Chat unavailable")
        else:
            if chat_id is not None and chat_id != allowed_chat:
                raise ToolError("Chat unavailable")
            matches = [
                m for m in messages if (cursor is None or m["id"] < cursor)
                and query.casefold() in m["text"].casefold()
            ]
        page = matches[:limit]
        more = len(matches) > limit
        return {
            "messages": page,
            "has_more": more,
            "next_cursor": page[-1]["id"] if more else None,
        }

    app = server.http_app(path="/v1/mcp", stateless_http=True, json_response=True)
    if telegram is not None:
        app.router.routes.append(Route("/setup", telegram.setup, methods=["POST"]))
    return app


if __name__ == "__main__":
    path = os.environ.get("CLOUD_SESSION_PATH")
    telegram = TelegramChat(path) if path else None
    serve(
        fixture_backend(None if telegram else int(os.environ["FIXTURE_CHAT_ID"]), telegram),
        internal_bearer=_secret("CLOUD_BACKEND_BEARER"),
    )
