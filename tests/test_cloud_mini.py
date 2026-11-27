import hmac
import json
import secrets
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from src.cloud_mini import telegram_identity
from src.cloud_slot_backend import SlotTelegram


def signed(credential, timestamp):
    fields = {"auth_date": str(timestamp), "user": json.dumps({"id": 123})}
    secret = hmac.digest(b"WebAppData", credential.encode(), "sha256")
    data = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return urlencode({**fields, "hash": hmac.digest(secret, data.encode(), "sha256").hex()})


def test_bad_and_expired_init_data():
    credential = secrets.token_urlsafe(32)
    valid = signed(credential, 5000)
    assert telegram_identity(valid, credential, 5100) == (123, "")
    with pytest.raises(ValueError):
        telegram_identity(valid.replace("123", "456"), credential, 5100)
    with pytest.raises(ValueError):
        telegram_identity(valid, credential, 9000)


@pytest.mark.asyncio
async def test_telegram_account_must_match(tmp_path, monkeypatch):
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

    client = FakeClient()
    telegram.pending = {"user": 123, "client": client, "step": "code", "phone": "fixture", "phone_hash": "fixture"}
    result = await telegram.configure(secrets.token_bytes(32), 123, "code", "12345")
    assert result["step"] == "phone"
    assert calls == ["logout"]
    assert telegram.pending is None
    assert not telegram.encrypted.path.exists()


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
