import hmac
import json
import secrets
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from src.cloud_bot import message
from src.cloud_mini import telegram_identity
from src.cloud_slot_backend import SlotTelegram
from src.cloud_slots import Slots


def signed(credential, timestamp):
    fields = {"auth_date": str(timestamp), "user": json.dumps({"id": 123})}
    secret = hmac.digest(b"WebAppData", credential.encode(), "sha256")
    data = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return urlencode({**fields, "hash": hmac.digest(secret, data.encode(), "sha256").hex()})


def test_bad_and_expired_init_data():
    credential = secrets.token_urlsafe(32)
    valid = signed(credential, 5000)
    assert telegram_identity(valid, credential, 5300) == (123, "")
    with pytest.raises(ValueError):
        telegram_identity(valid.replace("123", "456"), credential, 5100)
    with pytest.raises(ValueError):
        telegram_identity(valid, credential, 5301)
    with pytest.raises(ValueError):
        telegram_identity(valid, credential, 4999)


@pytest.mark.asyncio
async def test_bot_sends_machine_url_and_human_button(tmp_path):
    slots = Slots(tmp_path / "slots.sqlite3", 1)
    with slots.db(write=True) as db:
        assert slots.claim(db, 123) == 1
    sent = []

    async def bot(method, payload):
        sent.append((method, payload))

    await message(bot, slots, 999, "test_bot", "https://login.example",
                  "https://mcp.example/mcp", {"message": {
                      "chat": {"type": "private", "id": 123},
                      "from": {"id": 123}, "text": "/start",
                  }})
    method, payload = sent.pop()
    assert method == "sendMessage"
    assert "https://mcp.example/mcp" in payload["text"]
    button = payload["reply_markup"]["inline_keyboard"][0][0]
    assert button["web_app"]["url"] == "https://login.example/app"


@pytest.mark.asyncio
async def test_login_resumes_without_writing_credentials(tmp_path):
    telegram = SlotTelegram(tmp_path / "session.aes", 1)
    key = secrets.token_bytes(32)
    phone = "+" + "1" * 12
    client = SimpleNamespace(
        connect=lambda: None,
        disconnect=lambda: None,
    )

    async def connect():
        pass

    async def disconnect():
        pass

    async def send_code_request(phone):
        assert phone == "+" + "1" * 12
        return SimpleNamespace(phone_code_hash="fixture")

    client.connect = connect
    client.disconnect = disconnect
    client.send_code_request = send_code_request
    telegram.client = lambda *args: client
    assert await telegram.configure(key, 123, "status", None) == {"step": "phone"}
    assert await telegram.configure(key, 123, "phone", phone) == {"step": "code"}
    assert await telegram.configure(key, 123, "status", None) == {"step": "code"}
    assert not telegram.encrypted.path.exists()
    telegram.expire(telegram.pending)
    assert telegram.pending is None


@pytest.mark.asyncio
async def test_telegram_account_must_match(tmp_path):
    telegram = SlotTelegram(tmp_path / "session.aes", 1)
    calls = []

    class FakeClient:
        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def sign_in(self, **data):
            pass

        async def get_me(self):
            return SimpleNamespace(id=456)

        async def log_out(self):
            calls.append("logout")

    telegram.pending = {"user": 123, "client": FakeClient(), "step": "code",
                        "phone": "fixture", "phone_hash": "fixture"}
    result = await telegram.configure(secrets.token_bytes(32), 123, "code", "12345")
    assert result["step"] == "phone"
    assert calls == ["logout"]
    assert telegram.pending is None
    assert not telegram.encrypted.path.exists()


@pytest.mark.asyncio
async def test_password_failure_and_expired_login_are_distinct(tmp_path, monkeypatch):
    from starlette.requests import Request
    from telethon.errors import PasswordHashInvalidError

    from src.cloud_secure import encode

    telegram = SlotTelegram(tmp_path / "session.aes", 1)
    key = secrets.token_bytes(32)
    expired = await telegram.configure(key, 123, "password", "unused")
    assert expired["step"] == "phone"
    assert "истекло" in expired["error"]

    async def bad_password(*args):
        raise PasswordHashInvalidError(request=None)

    monkeypatch.setattr(telegram, "configure", bad_password)
    body = json.dumps({"action": "password", "user_id": 123, "value": "fictional"}).encode()

    async def receive():
        return {"type": "http.request", "body": body}

    request = Request({"type": "http", "method": "POST", "path": "/setup",
                       "headers": [(b"x-cloud-session-key", encode(key).encode())]}, receive)
    response = await telegram.configuration(request)
    assert response.status_code == 400
    assert "Telegram не принял пароль" in response.body.decode()
    assert b"fictional" not in response.body


@pytest.mark.asyncio
async def test_delete_logs_out_and_erases_session(tmp_path, monkeypatch):
    telegram = SlotTelegram(tmp_path / "session.aes", 1)
    key = secrets.token_bytes(32)
    telegram.encrypted.write(key, json.dumps({"user": 123, "session": "fixture", "peers": {}}).encode())
    calls = []

    class FakeClient:
        async def log_out(self):
            calls.append("logout")
            return True

    async def network(state, operation):
        return await operation(FakeClient())

    monkeypatch.setattr(telegram, "run", network)
    assert await telegram.configure(key, 123, "delete", None) == {"step": "deleted"}
    assert calls == ["logout"]
    assert not telegram.encrypted.path.exists()
