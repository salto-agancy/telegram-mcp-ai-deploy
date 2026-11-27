import json
import secrets
from base64 import b64encode

import pytest
from cryptography.exceptions import InvalidTag
from fastmcp.exceptions import ToolError
from telethon.tl.types import InputPeerChat

from src.cloud_secure import SessionFile, digest, unwrap, wrap
from src.cloud_slot_backend import SlotTelegram
from src.cloud_slots import Slots


def test_copy_and_restart(tmp_path):
    key, credential = secrets.token_bytes(32), secrets.token_urlsafe(32)
    session = b"fictional session and ACL"
    path = tmp_path / "account.aes"
    encrypted = SessionFile(path, 1)
    encrypted.write(key, session)
    wrapped = wrap(credential, key, "slot/1")
    assert session not in path.read_bytes()
    assert key not in path.read_bytes() + wrapped
    with pytest.raises(InvalidTag):
        unwrap(digest(credential), wrapped, "slot/1")
    with pytest.raises(InvalidTag):
        SessionFile(path, 2).read(key)
    reopened = SessionFile(path, 1)
    assert reopened.read(unwrap(credential, wrapped, "slot/1")) == session


@pytest.mark.asyncio
async def test_chat_acl_between_slots(tmp_path, monkeypatch):
    slots = Slots(tmp_path / "slots.sqlite3", 2)
    with slots.db(write=True) as db:
        assert slots.claim(db, 123) == 1
        assert slots.claim(db, 456) == 2
        assert slots.claim(db, 789) is None
    assert slots.lookup(123)["id"] == 1
    one, two = (SlotTelegram(tmp_path / f"{n}.aes", n) for n in (1, 2))
    for backend, own, other in [(one, 1, 2), (two, 2, 1)]:
        key = secrets.token_bytes(32)
        encoded_peer = b64encode(bytes(InputPeerChat(own))).decode()
        backend.encrypted.write(key, json.dumps({"session": "fixture", "peers": {str(own): encoded_peer}}).encode())

        async def fake_run(state, call, own=own):
            class FakeClient:
                async def get_messages(self, peer, **kwargs):
                    assert peer.chat_id == own
                    return []

            return await call(FakeClient())

        monkeypatch.setattr(backend, "run", fake_run)
        assert (await backend.messages(key, own, "", 1, None))["messages"] == []
        with pytest.raises(ToolError):
            await backend.messages(key, other, "", 1, None)
