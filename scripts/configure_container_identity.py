#!/usr/bin/env python3
"""Resolve a non-root container UID/GID without claiming an unrelated host user."""

from __future__ import annotations

import grp
import os
import pwd
import re
import stat
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
ID_MIN = 10001
ID_MAX = 19999


def _lookup(mapping: str) -> int | None:
    raw = mapping.strip().lower()
    if not raw or raw == "auto":
        return None
    if not re.fullmatch(r"[0-9]+", raw):
        raise SystemExit("APP_UID and APP_GID must be numeric or 'auto'")
    value = int(raw)
    if value <= 0 or value > 2_147_483_647:
        raise SystemExit("APP_UID and APP_GID must be positive non-root IDs")
    return value


def _host_uid_exists(value: int) -> bool:
    try:
        pwd.getpwuid(value)
    except KeyError:
        return False
    return True


def _host_gid_exists(value: int) -> bool:
    try:
        grp.getgrgid(value)
    except KeyError:
        return False
    return True


def _free_pair(start: int = ID_MIN) -> tuple[int, int]:
    for value in range(max(start, ID_MIN), ID_MAX + 1):
        if not _host_uid_exists(value) and not _host_gid_exists(value):
            return value, value
    raise SystemExit(f"no free container UID/GID in {ID_MIN}-{ID_MAX}")


def _read_values(lines: list[str]) -> tuple[int | None, int | None]:
    values: dict[str, str] = {}
    for line in lines:
        if line.startswith(("APP_UID=", "APP_GID=")):
            key, value = line.rstrip("\n").split("=", 1)
            values[key] = value
    return _lookup(values.get("APP_UID", "")), _lookup(values.get("APP_GID", ""))


def _resolved_identity(lines: list[str]) -> tuple[int, int]:
    configured_uid, configured_gid = _read_values(lines)
    if os.geteuid() != 0:
        return os.getuid(), os.getgid()
    if (
        configured_uid is not None
        and configured_gid is not None
        and not _host_uid_exists(configured_uid)
        and not _host_gid_exists(configured_gid)
    ):
        return configured_uid, configured_gid
    return _free_pair(configured_uid or ID_MIN)


def _replace_values(lines: list[str], uid: int, gid: int) -> list[str]:
    replacements = {"APP_UID": str(uid), "APP_GID": str(gid)}
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        replaced = False
        for key, value in replacements.items():
            if line.startswith(f"{key}="):
                result.append(f"{key}={value}\n")
                seen.add(key)
                replaced = True
                break
        if not replaced:
            result.append(line)
    for key, value in replacements.items():
        if key not in seen:
            result.append(f"{key}={value}\n")
    return result


def main() -> None:
    if not ENV_PATH.is_file():
        raise SystemExit("missing .env; run scripts/init-secrets.sh first")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    uid, gid = _resolved_identity(lines)
    updated = "".join(_replace_values(lines, uid, gid))
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=ENV_PATH.parent, delete=False
    ) as handle:
        handle.write(updated)
        temp_path = Path(handle.name)
    temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    temp_path.replace(ENV_PATH)
    print(f"Container identity configured: uid={uid}, gid={gid}")


if __name__ == "__main__":
    main()
