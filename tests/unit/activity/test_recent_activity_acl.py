"""ACL regression for recent_activity.

A batch snapshot must not become a way around the per-chat lane. Every rule that
applies when chats are opened one by one applies here too, and the response says
how many chats the lane removed rather than hiding the gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.client.connection import set_request_token
from src.config.server_config import ServerConfig, ServerMode, set_config
from src.server_components.session_acl import (
    check_pre_tool_access,
    clear_acl_cache,
    filter_tool_result,
)


@pytest.fixture(autouse=True)
def _reset_acl():
    clear_acl_cache()
    set_request_token(None)
    yield
    clear_acl_cache()
    set_request_token(None)


@pytest.fixture
def acl_config(tmp_path: Path):
    acl_file = tmp_path / "acl.yaml"
    acl_file.write_text(
        """
principals:
  token-team:
    chats:
      - -1001234567890
      - "@workgroup"
    read_only: true
    allow_global_search: false
""",
        encoding="utf-8",
    )
    config = ServerConfig(_cli_parse_args=[])
    config.server_mode = ServerMode.HTTP_AUTH
    config.acl_enabled = True
    config.acl_config_path = str(acl_file)
    set_config(config)
    return acl_file


def _snapshot() -> dict:
    return {
        "chats": [
            {"id": -1001234567890, "title": "Work", "messages": [{"id": 1}]},
            {"id": -1000000, "title": "Out of lane", "messages": [{"id": 2}]},
            {"id": 555, "username": "workgroup", "title": "By username",
             "messages": [{"id": 3}]},
        ],
        "coverage": {"chats_returned": 3},
    }


def test_out_of_lane_chats_are_removed_from_the_snapshot(acl_config):
    set_request_token("token-team")
    filtered = filter_tool_result("recent_activity", _snapshot())
    ids = [c["id"] for c in filtered["chats"]]
    assert -1000000 not in ids
    assert set(ids) == {-1001234567890, 555}


def test_removal_is_reported_rather_than_silent(acl_config):
    set_request_token("token-team")
    filtered = filter_tool_result("recent_activity", _snapshot())
    assert filtered["coverage"]["chats_hidden_by_acl"] == 1
    assert filtered["coverage"]["chats_returned"] == 2


@pytest.fixture
def empty_lane_config(tmp_path: Path):
    acl_file = tmp_path / "acl.yaml"
    acl_file.write_text(
        "principals:\n  empty-lane:\n    chats: []\n    read_only: false\n",
        encoding="utf-8",
    )
    config = ServerConfig(_cli_parse_args=[])
    config.server_mode = ServerMode.HTTP_AUTH
    config.acl_enabled = True
    config.acl_config_path = str(acl_file)
    set_config(config)
    return acl_file


def test_empty_lane_is_denied_outright(empty_lane_config):
    """An empty lane must fail closed, exactly as it does for find_chats."""
    set_request_token("empty-lane")

    pre = check_pre_tool_access("recent_activity", {"since": "24h"})
    assert pre is not None
    assert pre["ok"] is False
    assert "empty chat lane" in pre["error"].lower()

    denial = filter_tool_result("recent_activity", _snapshot())
    assert denial.get("ok") is False
    assert not denial.get("chats")


def test_read_only_principal_may_still_read_the_snapshot(acl_config):
    set_request_token("token-team")
    assert check_pre_tool_access("recent_activity", {"since": "24h"}) is None


def test_unlisted_principal_keeps_current_behaviour(acl_config):
    set_request_token("unlisted")
    filtered = filter_tool_result("recent_activity", _snapshot())
    assert len(filtered["chats"]) == 3
