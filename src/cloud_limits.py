"""Bounded per-process request limits for the cloud facade."""

import os
import time
from ipaddress import ip_address

from starlette.datastructures import Headers
from starlette.responses import Response


class PilotLimits:
    def __init__(self, app):
        self.app = app
        trusted = os.environ.get("CLOUD_TRUSTED_PROXY_IP")
        self.proxy = ip_address(trusted) if trusted else None
        human = os.environ.get("CLOUD_HUMAN_PROXY_IP")
        self.human_proxy = ip_address(human) if human else None
        if self.human_proxy and (self.proxy is None or self.human_proxy == self.proxy):
            raise ValueError("The human proxy must have its own trusted address")
        self.window = -1
        self.counts = {}
        self.limits = {"/register": 15, "/authorize": 30, "/authorize/status": 90, "/token": 60,
                       "/setup": 15, "/mcp": 120}

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        peer = (scope.get("client") or ("unknown",))[0]
        if (scope["type"] == "http" and self.human_proxy and peer == str(self.human_proxy)
                and path not in {"/app", "/authorize"} and not path.startswith(("/authorize/status/", "/app/api/"))):
            return await Response(status_code=403)(scope, receive, send)
        if path.startswith("/app/api/"):
            path = "/setup"
        if path.startswith("/authorize/status/"):
            path = "/authorize/status"
        limit = self.limits.get(path)
        if scope["type"] == "http" and limit:
            window = int(time.monotonic() // 60)
            if window != self.window:
                self.window = window
                self.counts.clear()
            if self.proxy is not None:
                try:
                    remote = ip_address(peer)
                    if remote == self.human_proxy and path in {"/authorize", "/authorize/status", "/setup"}:
                        peer = "human-proxy"
                    elif remote == self.proxy:
                        peer = str(ip_address(Headers(scope=scope)["cf-connecting-ip"]))
                    else:
                        raise ValueError
                except (KeyError, ValueError):
                    return await Response(status_code=403)(scope, receive, send)
            key = (path, peer)
            if self.counts.get(key, 0) >= limit or (key not in self.counts and len(self.counts) >= 4096):
                return await Response(status_code=429, headers={"Retry-After": "60"})(scope, receive, send)
            self.counts[key] = self.counts.get(key, 0) + 1
        return await self.app(scope, receive, send)
