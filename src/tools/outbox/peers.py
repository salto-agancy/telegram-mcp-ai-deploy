"""Typed recipients only. A bare number is refused.

Found while designing this: find_chats returns group ids without the -100 prefix,
and raw MTProto accepted both <id> and -<id> for the same group without an error.
A bare id can therefore address a different peer than intended; a typed reference
cannot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Telegram service notifications/login codes, BotFather, SpamBot.
BLOCKED_PEER_IDS = {777000, 93372553, 178220800}
BLOCKED_USERNAMES = {"botfather", "spambot", "telegram"}

_TYPED = re.compile(r"^(user|chat|channel):(\d{1,16})$")
_USERNAME = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_TME = re.compile(r"^(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?$")

HELP = (
    "recipient must be typed: 'me', 'user:<id>', 'chat:<id>' (basic group), 'channel:<id>' "
    "(supergroup or channel), '@username' or https://t.me/username; a bare number is refused"
)


class PeerError(ValueError):
    pass


@dataclass(frozen=True)
class PeerRef:
    kind: str  # me | user | chat | channel | username
    value: str

    @property
    def text(self) -> str:
        return (
            "me"
            if self.kind == "me"
            else (
                f"@{self.value}"
                if self.kind == "username"
                else f"{self.kind}:{self.value}"
            )
        )

    def telethon_target(self):
        """What TelegramClient.get_input_entity expects: marked ids, usernames, 'me'."""
        if self.kind == "me":
            return "me"
        if self.kind == "username":
            return self.value
        n = int(self.value)
        return {"user": n, "chat": -n, "channel": int(f"-100{n}")}[self.kind]


def parse_peer(ref: str) -> PeerRef:
    s = (ref or "").strip()
    if not s:
        raise PeerError(HELP)
    if s.lower() in ("me", "self"):
        return PeerRef("me", "me")
    m = _TYPED.match(s)
    if m:
        kind, n = m.group(1), int(m.group(2))
        if n in BLOCKED_PEER_IDS:
            raise PeerError("writing to Telegram service chats is not allowed")
        return PeerRef(kind, str(n))
    m = _USERNAME.match(s) or _TME.match(s)
    if m:
        if m.group(1).lower() in BLOCKED_USERNAMES:
            raise PeerError("writing to Telegram service chats is not allowed")
        return PeerRef("username", m.group(1))
    raise PeerError(HELP)


def check_resolved_id(entity_id: int) -> None:
    """A username can resolve to a blocked service account; refuse after resolution too."""
    if abs(int(entity_id)) in BLOCKED_PEER_IDS:
        raise PeerError("writing to Telegram service chats is not allowed")
