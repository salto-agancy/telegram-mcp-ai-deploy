import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from src.cloud_limits import PilotLimits


@pytest.mark.parametrize("cloudflare", [None, "192.0.2.2"])
def test_human_proxy_requires_distinct_cloudflare_proxy(monkeypatch, cloudflare):
    monkeypatch.setenv("CLOUD_HUMAN_PROXY_IP", "192.0.2.2")
    if cloudflare:
        monkeypatch.setenv("CLOUD_TRUSTED_PROXY_IP", cloudflare)
    else:
        monkeypatch.delenv("CLOUD_TRUSTED_PROXY_IP", raising=False)
    with pytest.raises(ValueError):
        PilotLimits(Starlette())


@pytest.mark.asyncio
async def test_human_proxy_is_scoped_and_cannot_forge_client_ip(monkeypatch):
    monkeypatch.setenv("CLOUD_TRUSTED_PROXY_IP", "192.0.2.1")
    monkeypatch.setenv("CLOUD_HUMAN_PROXY_IP", "192.0.2.2")
    app = PilotLimits(Starlette(routes=[
        Route("/authorize", lambda request: Response("ok")),
        Route("/token", lambda request: Response("ok")),
    ]))
    human = httpx.ASGITransport(app=app, client=("192.0.2.2", 1234))
    cloudflare = httpx.ASGITransport(app=app, client=("192.0.2.1", 1234))
    other = httpx.ASGITransport(app=app, client=("192.0.2.3", 1234))
    async with httpx.AsyncClient(transport=human, base_url="https://example.test") as client:
        for index in range(30):
            assert (await client.get("/authorize", headers={"cf-connecting-ip": f"198.51.100.{index + 1}"})).status_code == 200
        assert (await client.get("/authorize")).status_code == 429
        assert (await client.get("/token")).status_code == 403
        assert (await client.get("/revoke")).status_code == 403
        assert (await client.get("/mcp")).status_code == 403
    async with httpx.AsyncClient(transport=other, base_url="https://example.test") as client:
        assert (await client.get("/authorize", headers={"cf-connecting-ip": "198.51.100.1"})).status_code == 403
    async with httpx.AsyncClient(transport=cloudflare, base_url="https://example.test") as client:
        assert (await client.get("/authorize")).status_code == 403
        assert (await client.get("/authorize", headers={"cf-connecting-ip": "198.51.100.1"})).status_code == 200
