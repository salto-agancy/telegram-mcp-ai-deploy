import hmac
import json
import secrets
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
    assert telegram_identity(valid, credential, 5300) == (123, "")
    with pytest.raises(ValueError):
        telegram_identity(valid.replace("123", "456"), credential, 5100)
    with pytest.raises(ValueError):
        telegram_identity(valid, credential, 5301)
    with pytest.raises(ValueError):
        telegram_identity(valid, credential, 4999)


@pytest.mark.asyncio
async def test_slot_has_no_credential_entry(tmp_path):
    telegram = SlotTelegram(tmp_path / "session.aes", 1)
    key = secrets.token_bytes(32)
    assert await telegram.configure(key, 123, "status", None) == {"step": "login_closed"}
    for action in ("phone", "code", "password"):
        with pytest.raises(ValueError):
            await telegram.configure(key, 123, action, None)
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
