#!/usr/bin/env python3
"""Render the deployment ACL as JSON (valid YAML) without exposing its principal."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path


def parse_chat(value: str) -> str | int:
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value


root = Path(__file__).resolve().parents[1]
secrets_dir = root / "secrets"
token = (secrets_dir / "backend_bearer").read_text(encoding="utf-8").strip()
if not token:
    raise SystemExit("backend bearer is empty")

mode = os.environ.get("TELEGRAM_ACCESS_MODE", "read-only").strip().lower()
if mode not in {"read-only", "write"}:
    raise SystemExit("TELEGRAM_ACCESS_MODE must be read-only or write")
chats = [
    parse_chat(item)
    for item in os.environ.get("TELEGRAM_ALLOWED_CHATS", "me").split(",")
    if item.strip()
]
if not chats:
    raise SystemExit("TELEGRAM_ALLOWED_CHATS must contain at least one chat")

document = {
    "blocked_peers": [777000, 93372553, 178220800, "@BotFather", "@SpamBot"],
    "principals": {
        token: {
            "chats": chats,
            "read_only": mode == "read-only",
            "allow_global_search": True,
            "allow_mtproto": False,
        }
    },
}
target = secrets_dir / "acl.yaml"
target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
target.chmod(stat.S_IRUSR | stat.S_IWUSR)
print(f"ACL rendered: mode={mode}, allowed_chats={len(chats)}, raw_mtproto=false")
