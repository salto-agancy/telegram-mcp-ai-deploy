import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ISSUER = "https://pilot.example"
CALLBACK = "https://client.example/oauth/callback"
ROOT = Path(__file__).resolve().parents[1]


def secret():
    return secrets.token_urlsafe(32)


@contextmanager
def service(module, config, output):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        **config,
        "BIND_HOST": "127.0.0.1",
        "BIND_PORT": str(port),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DO_NOT_TRACK": "1",
    }
    with output.open("ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "src." + module],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                assert proc.poll() is None, "Pilot service exited during startup"
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail("Pilot startup timed out")
            yield f"http://127.0.0.1:{port}"
        finally:
            proc.terminate()
            proc.wait(timeout=10)


@pytest.fixture(scope="module")
def backends(tmp_path_factory):
    root = tmp_path_factory.mktemp("fixture-processes")
    with ExitStack() as stack:
        routes, invites = {}, {}
        for user, chat in (("first", 101), ("second", 202)):
            invite, bearer = secret(), secret()
            private = stack.enter_context(
                service(
                    "cloud_backend",
                    {"FIXTURE_CHAT_ID": str(chat), "CLOUD_BACKEND_BEARER": bearer},
                    root / user,
                )
            )
            routes[user] = {
                "url": private + "/v1/mcp",
                "bearer": bearer,
                "invite_sha256": hashlib.sha256(invite.encode()).hexdigest(),
                "expires": int(time.time()) + 3600,
            }
            invites[user] = invite
        yield routes, invites, root


@pytest.fixture
def facade(backends, tmp_path):
    routes, invites, _ = backends
    config = tmp_path / "routes"
    config.write_text(json.dumps(routes))
    database = tmp_path / "oauth.db"
    log = tmp_path / "statuses"

    @contextmanager
    def start():
        with (
            service(
                "cloud_oauth",
                {
                    "PUBLIC_BASE_URL": ISSUER,
                    "MCP_OAUTH_REDIRECT_URIS": CALLBACK,
                    "CLOUD_ROUTES_FILE": str(config),
                    "OAUTH_DB_PATH": str(database),
                },
                log,
            ) as url,
            httpx.Client(base_url=url, trust_env=False) as http,
        ):
            yield http

    return start, invites, database, log


class Fields(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        item = dict(attrs)
        if tag == "input" and item.get("type") == "hidden":
            self.fields[item["name"]] = item["value"]


def register(http):
    meta = http.post(
        "/register",
        json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"},
    )
    assert meta.status_code == 201
    assert meta.headers["cache-control"] == "no-store"
    data = meta.json()
    assert data["token_endpoint_auth_method"] == "client_secret_post"
    assert data["client_secret"]
    return {k: data[k] for k in ("client_id", "client_secret")}


def auth_request(client_id, verifier):
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": "mcp",
        "resource": ISSUER + "/mcp",
        "state": secret(),
    }


def approval(http, credentials, invite, callback=CALLBACK):
    verifier = secret()
    params = auth_request(credentials["client_id"], verifier)
    params["redirect_uri"] = callback
    page = http.get("/authorize", params=params)
    assert page.status_code == 200
    form = Fields(page.text).fields
    assert form == params
    result = http.post(
        "/authorize", data={**form, "invite": invite}, headers={"Origin": ISSUER}
    )
    assert result.status_code == 303
    uri = urlsplit(result.headers["location"])
    assert uri._replace(query="").geturl() == callback
    returned = parse_qs(uri.query)
    assert set(returned) == {"code", "state"}
    assert returned["state"] == [params["state"]]
    token_form = {
        **credentials,
        "code": returned["code"][0],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": callback,
        "resource": ISSUER + "/mcp",
    }
    return token_form, [
        str(page.url),
        str(result.request.url),
        result.headers["location"],
    ]


def rpc(http, access, method, params):
    return http.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + access,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def tool(http, access, **arguments):
    resp = rpc(
        http, access, "tools/call", {"name": "get_messages", "arguments": arguments}
    )
    assert resp.status_code == 200
    return resp.json()["result"]


def test_discovery_and_validation(facade):
    start, invites, _, _ = facade
    with start() as http:
        unauthorized = http.post("/mcp")
        assert unauthorized.status_code == 401
        resource_url = unauthorized.headers["www-authenticate"].split('"')[1]
        metadata = http.get(urlsplit(resource_url).path).json()
        assert metadata["resource"] == ISSUER + "/mcp"
        discovery = http.get("/.well-known/oauth-authorization-server").json()
        assert discovery["issuer"] == metadata["authorization_servers"][0]
        assert discovery["registration_endpoint"] == ISSUER + "/register"
        assert discovery["revocation_endpoint"] == ISSUER + "/revoke"
        assert discovery["code_challenge_methods_supported"] == ["S256"]
        assert discovery["token_endpoint_auth_methods_supported"] == [
            "client_secret_post"
        ]
        assert (
            http.post("/register", json={"redirect_uris": [CALLBACK + "/"]}).status_code
            == 400
        )
        credentials = register(http)
        params = auth_request(credentials["client_id"], secret())
        for bad in (
            {"code_challenge": ""},
            {"code_challenge_method": "plain"},
            {"redirect_uri": CALLBACK + "/"},
            {"resource": ISSUER + "/other"},
            {"response_type": "token"},
            {"invite": "unexpected-query-parameter"},
        ):
            assert http.get("/authorize", params={**params, **bad}).status_code == 400
        assert (
            http.get(
                "/authorize",
                params={
                    k: v for k, v in params.items() if k != "code_challenge_method"
                },
            ).status_code
            == 400
        )
        assert (
            http.post(
                "/authorize", data={**params, "invite": invites["first"]}
            ).status_code
            == 403
        )
        assert (
            http.post(
                "/authorize",
                data={**params, "invite": secret()},
                headers={"Origin": ISSUER},
            ).status_code
            == 403
        )
        assert (
            http.post(
                "/token", data=credentials, params={"client_secret": secret()}
            ).status_code
            == 400
        )


def test_two_users_rotation_restart_and_revoke(facade, backends):
    start, invites, database, log = facade
    routes, _, backend_logs = backends
    urls = []
    hidden = [*invites.values(), *(r["bearer"] for r in routes.values())]
    with start() as http:
        credentials = register(http)
        other_client = register(http)
        hidden.extend([credentials["client_secret"], other_client["client_secret"]])
        exchange, first_urls = approval(http, credentials, invites["first"])
        urls += first_urls
        hidden.append(exchange["code_verifier"])
        for bad, status in (
            ({"code_verifier": secret()}, 400),
            ({"client_secret": ""}, 401),
            ({"resource": ISSUER + "/other"}, 400),
            ({"redirect_uri": CALLBACK + "/"}, 400),
            (other_client, 400),
        ):
            assert http.post("/token", data={**exchange, **bad}).status_code == status
        with ThreadPoolExecutor(2) as pool:
            results = list(
                pool.map(lambda _: http.post("/token", data=exchange), range(2))
            )
        assert sorted(r.status_code for r in results) == [200, 400]
        first = next(r.json() for r in results if r.status_code == 200)
        exchange2, second_urls = approval(http, credentials, invites["second"])
        urls += second_urls
        second = http.post("/token", data=exchange2).json()
        hidden.extend(
            (
                exchange2["code_verifier"],
                first["access_token"],
                first["refresh_token"],
                second["access_token"],
                second["refresh_token"],
            )
        )

        async def check_sdk():
            transport = StreamableHttpTransport(
                str(http.base_url.join("/mcp")),
                headers={"Authorization": "Bearer " + first["access_token"]},
            )
            async with Client(transport) as client:
                names = [item.name for item in await client.list_tools()]
                assert names == ["get_messages"]
                reply = await client.call_tool("get_messages", {"limit": 1})
                assert reply.structured_content["has_more"]

        asyncio.run(check_sdk())
        for access, own, foreign in (
            (first["access_token"], 101, 202),
            (second["access_token"], 202, 101),
        ):
            tools = rpc(http, access, "tools/list", {}).json()["result"]["tools"]
            assert [t["name"] for t in tools] == ["get_messages"]
            assert tools[0]["annotations"]["readOnlyHint"] is True
            page = tool(http, access, query="маяк", limit=2)["structuredContent"]
            assert page["has_more"] and len(page["messages"]) == 2
            assert all(str(own) in m["text"] for m in page["messages"])
            next_page = tool(http, access, chat_id=own, cursor=page["next_cursor"])[
                "structuredContent"
            ]
            assert max(m["id"] for m in next_page["messages"]) < min(
                m["id"] for m in page["messages"]
            )
            denied = tool(http, access, chat_id=foreign)
            assert denied["isError"] and denied == tool(http, access, chat_id=999)
            assert (
                tool(http, access, chat_id=own, query="no matches")[
                    "structuredContent"
                ]["messages"]
                == []
            )
        for route in routes.values():
            assert (
                http.post(
                    route["url"],
                    headers={"Authorization": "Bearer " + first["access_token"]},
                ).status_code
                == 401
            )
        assert http.post("/mcp?access_token=invalid-location").status_code == 401

    with start() as http:
        assert not tool(http, second["access_token"])["isError"]
        refresh = {
            **credentials,
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
        }
        assert http.post("/token", data={**refresh, **other_client}).status_code == 400
        with ThreadPoolExecutor(2) as pool:
            results = list(
                pool.map(lambda _: http.post("/token", data=refresh), range(2))
            )
        assert sorted(r.status_code for r in results) == [200, 400]
        rotated = next(r.json() for r in results if r.status_code == 200)
        hidden.extend([rotated["access_token"], rotated["refresh_token"]])
        assert rotated["refresh_token"] != first["refresh_token"]
        assert http.post("/token", data=refresh).status_code == 400
        assert rpc(http, first["access_token"], "tools/list", {}).status_code == 401
        assert not tool(http, rotated["access_token"], chat_id=101)["isError"]
        assert tool(http, rotated["access_token"], chat_id=202)["isError"]
        http.post(
            "/revoke", data={**other_client, "token": rotated["access_token"]}
        ).raise_for_status()
        assert not tool(http, rotated["access_token"])["isError"]
        http.post(
            "/revoke", data={**credentials, "token": first["refresh_token"]}
        ).raise_for_status()
        assert rpc(http, rotated["access_token"], "tools/list", {}).status_code == 401
        assert (
            http.post(
                "/token", data={**refresh, "refresh_token": rotated["refresh_token"]}
            ).status_code
            == 400
        )
        assert not tool(http, second["access_token"], chat_id=202)["isError"]
        http.post(
            "/revoke", data={**credentials, "token": second["access_token"]}
        ).raise_for_status()
        assert rpc(http, second["access_token"], "tools/list", {}).status_code == 401

    assert all(credential not in url for url in urls for credential in hidden)
    hidden.extend([exchange["code"], exchange2["code"]])
    with sqlite3.connect(database) as db:
        assert all(credential not in "\n".join(db.iterdump()) for credential in hidden)
    for path in (log, backend_logs / "first", backend_logs / "second"):
        lines = path.read_text().splitlines()
        assert lines and all(
            re.fullmatch(r"http_status=[1-5][0-9]{2}", line) for line in lines
        )


def test_expiry_and_revocation_survive_restart(facade):
    start, invites, database, _ = facade
    with start() as http:
        client = register(http)
        stale, _ = approval(http, client, invites["first"])
        with sqlite3.connect(database) as db:
            db.execute("UPDATE grants SET deadline=0")
        assert http.post("/token", data=stale).status_code == 400
        valid, _ = approval(http, client, invites["second"])
        tokens = http.post("/token", data=valid).json()
        with sqlite3.connect(database) as db:
            db.execute("UPDATE tokens SET expires=0 WHERE kind='access_token'")
        assert rpc(http, tokens["access_token"], "tools/list", {}).status_code == 401
        refresh = {
            **client,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        }
        rotated = http.post("/token", data=refresh).json()
        http.post(
            "/revoke", data={**client, "token": rotated["refresh_token"]}
        ).raise_for_status()
    with start() as http:
        assert rpc(http, rotated["access_token"], "tools/list", {}).status_code == 401
        assert (
            http.post(
                "/token", data={**refresh, "refresh_token": rotated["refresh_token"]}
            ).status_code
            == 400
        )


def test_invite_reuse_and_rate_limit(facade):
    start, invites, _, _ = facade
    with start() as http:
        client = register(http)
        exchange, _ = approval(http, client, invites["first"])
        old = http.post("/token", data=exchange).json()
        new_client = register(http)
        exchange, _ = approval(http, new_client, invites["first"])
        assert rpc(http, old["access_token"], "tools/list", {}).status_code == 401
        new = http.post("/token", data=exchange).json()
        assert not tool(http, new["access_token"])["isError"]
        assert (
            http.post(
                "/token",
                data={
                    **client,
                    "grant_type": "refresh_token",
                    "refresh_token": old["refresh_token"],
                },
            ).status_code
            == 400
        )
        for _ in range(13):
            register(http)
        assert (
            http.post("/register", json={"redirect_uris": [CALLBACK]}).status_code
            == 429
        )


def test_telegram_backend_contract(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from starlette.testclient import TestClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.tl.types import InputPeerUser

    from src.cloud_backend import fixture_backend
    from src.cloud_setup import TelegramChat

    peer = InputPeerUser(101, 99)
    calls = []

    class TelegramStub:
        session = SimpleNamespace(save=lambda: "fictional-session")

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def send_code_request(self, phone):
            assert phone.startswith("+")
            return SimpleNamespace(phone_code_hash="fictional")

        async def is_user_authorized(self):
            return False

        async def sign_in(self, **args):
            if "code" in args:
                raise SessionPasswordNeededError(request=None)
            assert "password" in args

        async def get_dialogs(self, limit):
            return [SimpleNamespace(id=101, input_entity=peer, name="Test")]

        async def get_messages(self, selected, **args):
            calls.append((selected, args))
            return [
                SimpleNamespace(id=3, message="Beacon"),
                SimpleNamespace(id=2, message="Signal"),
            ]

    state = tmp_path / "session.json"
    telegram = TelegramChat(state)
    monkeypatch.setattr(telegram, "client", lambda *args: TelegramStub())
    with (
        TestClient(fixture_backend(101)) as fixture,
        TestClient(fixture_backend(None, telegram)) as live,
    ):
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }

        def mcp(client, method, **params):
            resp = client.post(
                "/v1/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            return resp.json()["result"]

        assert mcp(fixture, "tools/list") == mcp(live, "tools/list")
        for action, value, step in (
            ("phone", "+" + "1" * 12, "code"),
            ("code", "1234", "password"),
            ("password", "fictional", "chat"),
            ("chat", "999", "chat"),
            ("chat", "101", "done"),
        ):
            assert (
                live.post(
                    "/setup", json={"action": action, "value": value, "flow": "test"}
                ).json()["step"]
                == step
            )
        args = {
            "name": "get_messages",
            "arguments": {"chat_id": 101, "query": "Beacon", "limit": 1, "cursor": 4},
        }
        result = mcp(live, "tools/call", **args)["structuredContent"]
        assert result == {
            "messages": [{"id": 3, "text": "Beacon"}],
            "has_more": True,
            "next_cursor": 3,
        }
        assert calls == [(peer, {"limit": 2, "offset_id": 4, "search": "Beacon"})]
        args["arguments"]["chat_id"] = 202
        assert mcp(live, "tools/call", **args)["isError"]
        assert len(calls) == 1
        assert live.post("/setup", json={"action": "disconnect"}).status_code == 200
        assert not state.exists()


def test_perplexity_public_client_without_dcr(backends, tmp_path):
    routes, invites, _ = backends
    config = tmp_path / "routes"
    config.write_text(json.dumps(routes))
    approved = "https://www.perplexity.ai/rest/connections/oauth_callback"
    enterprise = "https://enterprise.perplexity.ai/rest/connections/oauth_callback"
    other = "https://staging.perplexity.ai/rest/connections/oauth_callback"
    client = {"client_id": "https://www.perplexity.ai/.well-known/mcp-client.json"}
    config_env = {
        "PUBLIC_BASE_URL": ISSUER,
        "MCP_OAUTH_REDIRECT_URIS": ",".join((approved, enterprise, other)),
        "CLOUD_ROUTES_FILE": str(config),
        "OAUTH_DB_PATH": str(tmp_path / "oauth.db"),
    }
    with (
        service("cloud_oauth", config_env, tmp_path / "statuses") as url,
        httpx.Client(base_url=url, trust_env=False) as http,
    ):
        metadata = http.get("/.well-known/oauth-authorization-server").json()
        assert metadata["client_id_metadata_document_supported"] is True
        assert metadata["token_endpoint_auth_methods_supported"] == [
            "client_secret_post",
            "none",
        ]
        params = {
            **auth_request(client["client_id"], secret()),
            "redirect_uri": approved,
        }
        for change in (
            {"client_id": client["client_id"] + "/"},
            {"redirect_uri": approved + "/"},
            {"redirect_uri": other},
            {"code_challenge_method": "plain"},
        ):
            assert (
                http.get("/authorize", params={**params, **change}).status_code == 400
            )
        for callback, user in ((approved, "first"), (enterprise, "second")):
            exchange, _ = approval(http, client, invites[user], callback)
            for bad in (
                {"code_verifier": ""},
                {"code_verifier": secret()},
                {"redirect_uri": other},
            ):
                assert http.post("/token", data={**exchange, **bad}).status_code == 400
            tokens = http.post("/token", data=exchange)
            assert tokens.status_code == 200
            assert not tool(http, tokens.json()["access_token"])["isError"]
            refresh = http.post(
                "/token",
                data={
                    **client,
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.json()["refresh_token"],
                },
            )
            assert refresh.status_code == 200
            access = refresh.json()["access_token"]
            assert (
                http.post("/revoke", data={**client, "token": access}).status_code
                == 200
            )
            assert rpc(http, access, "tools/list", {}).status_code == 401
