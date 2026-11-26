"""Bounded per-process request limits for the cloud facade."""

import time

from starlette.responses import Response


class PilotLimits:
    def __init__(self, app):
        self.app = app
        self.window = -1
        self.counts = {}
        self.limits = {"/register": 15, "/authorize": 30, "/token": 60,
                       "/setup": 15, "/mcp": 120}

    async def __call__(self, scope, receive, send):
        limit = self.limits.get(scope.get("path"))
        if scope["type"] == "http" and limit:
            window = int(time.monotonic() // 60)
            if window != self.window:
                self.window = window
                self.counts.clear()
            key = (scope["path"], (scope.get("client") or ("unknown",))[0])
            if self.counts.get(key, 0) >= limit or (key not in self.counts and len(self.counts) >= 4096):
                return await Response(status_code=429, headers={"Retry-After": "60"})(scope, receive, send)
            self.counts[key] = self.counts.get(key, 0) + 1
        return await self.app(scope, receive, send)
