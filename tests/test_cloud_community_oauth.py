import asyncio
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from src import cloud_cimd
from src.cloud_mini import MiniApp
from src.cloud_oauth import build_cloud_app
from src.cloud_secure import decode, encode
from src.cloud_slots import Slots

ISSUER = "https://pilot.example"
CALLBACK = "https://client.example/callback"


class StubMini(MiniApp):
    async def call(self, slot, key, path, payload, headers=None):
        assert slot == 1 and key == self.expected_key and path == "/setup"
        step = "deleted" if payload["action"] == "delete" else "connected"
        return httpx.Response(200, json={"step": step})


@pytest.fixture
def community(tmp_path, monkeypatch):
    credential, proof = secrets.token_urlsafe(32), secrets.token_hex(32)
    path = tmp_path / "oauth.sqlite3"
    routes = {"1": {"url": "http://cloud-slot-1:8000/v1/mcp", "bearer": secrets.token_urlsafe(32)}}
    slots = Slots(path, 1)
    with slots.db(write=True) as db:
        slots.claim(db, 123)
    original_client = httpx.AsyncClient

    def create_app():
        mini = StubMini(ISSUER, credential, slots, {1: routes["1"]})
        mini.expected_key = mini.key(123, proof)

        def backend(request):
            assert decode(request.headers["x-cloud-session-key"]) == mini.expected_key
            return httpx.Response(200, json={"result": {}})

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(backend)))
        return build_cloud_app(ISSUER, set(), routes, path, mini=mini, bot_username="test_bot")

    def signed(start=""):
        data = {"auth_date": str(int(time.time())), "user": '{"id":123}', "start_param": start}
        key = hmac.digest(b"WebAppData", credential.encode(), "sha256")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        data["hash"] = hmac.digest(key, check.encode(), "sha256").hex()
        return {"origin": ISSUER, "x-telegram-init-data": urlencode(data)}

    def mini_call(http, action, value=None, start=""):
        launches = getattr(http, "launches", {})
        if start not in launches:
            headers = signed(start)
            first = http.post("/app/api/session", headers=headers)
            assert first.status_code == 200
            assert http.post("/app/api/session", headers=headers).status_code == 403
            launches[start] = first.json()["nonce"]
            http.launches = launches
        return http.post(f"/app/api/{action}", headers={"origin": ISSUER, "x-cloud-nonce": launches[start]},
                         json={"proof": proof, "value": value})

    return create_app, mini_call, slots


def register(http, method):
    result = http.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": method})
    assert result.status_code == 201
    registered = result.json()
    return {k: registered[k] for k in ("client_id", "client_secret") if k in registered}


def oauth_request(client):
    verifier = secrets.token_urlsafe(32)
    return {"client_id": client["client_id"], "redirect_uri": CALLBACK, "response_type": "code",
            "code_challenge": encode(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}, verifier


def exchange(http, other, mini_call, client):
    params, verifier = oauth_request(client)
    params["resource"] = ISSUER + "/mcp/"
    assert http.get("/authorize", params={**params, "resource": ISSUER + "/mcp//"}).status_code == 400
    response = http.get("/authorize", params=params)
    assert response.status_code == 200
    flow = re.search(r"startapp=([A-Za-z0-9_-]+)", response.text)[1]
    poll = f"/authorize/status/{flow}"
    assert other.get(poll).status_code == 403
    approval = mini_call(http, "status", start=flow).json()["oauth"]
    assert approval["domain"] == "client.example"
    assert approval["code"] in response.text
    assert mini_call(http, "decision", True, flow).status_code == 200
    result = http.get(poll)
    assert result.status_code == 303
    code = parse_qs(urlsplit(result.headers["location"]).query)["code"][0]
    assert http.get(poll).status_code == 403
    data = {**client, "grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": CALLBACK}
    data["resource"] = params["resource"]
    assert http.post("/token", data={**data, "resource": ISSUER + "/mcp//"}).status_code == 400
    if "client_secret" in client:
        assert http.post("/token", data={**data, "client_secret": "incorrect"}).status_code == 401
    result = http.post("/token", data=data)
    assert result.status_code == 200
    return result.json()


@pytest.mark.parametrize("method", ["none", "client_secret_post"])
def test_dcr_browser_binding_and_refresh_restart(community, method):
    create_app, mini_call, _ = community
    app = create_app()
    with TestClient(app, base_url=ISSUER, follow_redirects=False) as http, TestClient(app, base_url=ISSUER) as other:
        root = http.get("/.well-known/oauth-protected-resource")
        full = http.get("/.well-known/oauth-protected-resource/mcp")
        assert root.status_code == full.status_code == 200
        assert root.json() == full.json()
        client = register(http, method)
        assert ("client_secret" in client) == (method == "client_secret_post")
        issued = exchange(http, other, mini_call, client)
        refresh = {**client, "grant_type": "refresh_token", "refresh_token": issued["refresh_token"]}
        assert http.post("/token", data=refresh).status_code == 200
    with TestClient(create_app(), base_url=ISSUER) as restarted:
        retried = restarted.post("/token", data=refresh)
        assert retried.status_code == 200
        assert restarted.post("/mcp", headers={"Authorization": "Bearer " + retried.json()["access_token"]}).status_code == 200
        assert restarted.post("/token", data=refresh).status_code == 400


def test_api_key_and_delete_everything(community, monkeypatch):
    create_app, mini_call, slots = community
    with TestClient(create_app(), base_url=ISSUER) as http:
        delivered = []

        async def deliver(client, credential, method, payload):
            assert method == "sendMessage" and payload["chat_id"] == 123
            delivered.append(payload["text"].splitlines()[-1])

        async def unavailable(*args):
            raise httpx.ConnectError("unavailable")

        monkeypatch.setattr("src.cloud_mini.telegram_api", unavailable)
        assert mini_call(http, "api_key").status_code == 502
        with slots.db() as db:
            assert db.execute("SELECT count(*) FROM tokens").fetchone()[0] == 0

        monkeypatch.setattr("src.cloud_mini.telegram_api", deliver)
        response = mini_call(http, "api_key")
        key, = delivered
        assert response.json()["key_sent"] and key not in response.text
        assert "api_key" not in mini_call(http, "status").json()
        auth = {"Authorization": "Bearer " + key}
        assert http.post("/mcp", headers=auth).status_code == 200
        assert mini_call(http, "delete").json()["step"] == "deleted"
        assert http.post("/mcp", headers=auth).status_code == 401
        assert mini_call(http, "status").status_code == 403
    assert slots.lookup(123) is None
    with slots.db() as db:
        assert db.execute("SELECT count(*) FROM tokens").fetchone()[0] == 0


def test_cimd(community, monkeypatch):
    async def document(client):
        return {"client_id": client, "redirect_uris": [CALLBACK]}

    monkeypatch.setattr(cloud_cimd, "fetch_document", document)
    create_app, _, _ = community
    with TestClient(create_app(), base_url=ISSUER) as http:
        params, _ = oauth_request({"client_id": "https://client.example/metadata.json"})
        assert http.get("/authorize", params=params).status_code == 200
        params["redirect_uri"] = "https://other.example/callback"
        assert http.get("/authorize", params=params).status_code == 400


@pytest.mark.asyncio
async def test_cimd_internal_addresses_are_not_dialled(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(0, 0, 0, "", ("127.0.0.1", 443))]

    async def dial(*args, **kwargs):
        pytest.fail("Internal address was dialled")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(asyncio, "open_connection", dial)
    with pytest.raises(ValueError, match="Non-public"):
        await cloud_cimd.fetch_document("https://client.example/metadata.json")
