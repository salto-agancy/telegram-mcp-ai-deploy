"""One fictional chat per process. No Telegram credentials or network client."""

import os
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from src.cloud_http import serve
from src.oauth_proxy import _secret


def fixture_backend(allowed_chat: int):
    server = FastMCP("Telegram cloud pilot", mask_error_details=True)
    messages = [
        {"id": n, "text": f"Маяк {allowed_chat}, сигнал {n}."} for n in range(6, 0, -1)
    ]

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def get_messages(
        chat_id: int | None = None,
        query: str = "",
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
        cursor: Annotated[int, Field(ge=1)] | None = None,
    ) -> dict:
        """Read or search the selected chat. Pass next_cursor to read the next page."""
        if chat_id is not None and chat_id != allowed_chat:
            raise ToolError("Chat unavailable")
        matches = [
            m
            for m in messages
            if (cursor is None or m["id"] < cursor)
            and query.casefold() in m["text"].casefold()
        ]
        page = matches[:limit]
        more = len(matches) > limit
        return {
            "messages": page,
            "has_more": more,
            "next_cursor": page[-1]["id"] if more else None,
        }

    return server.http_app(path="/v1/mcp", stateless_http=True, json_response=True)


if __name__ == "__main__":
    serve(
        fixture_backend(int(os.environ["FIXTURE_CHAT_ID"])),
        internal_bearer=_secret("CLOUD_BACKEND_BEARER"),
    )
