"""Tests for the shared MCP call journal middleware."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.server_components.audit_middleware import (
    AuditMiddleware,
    _scrub,
    _session_fingerprint,
    input_hash,
)
from src.server_components.middleware_register import register_mcp_middleware


def _config(**overrides):
    base = {"prefix_mcp_tools_with_account": False, "audit_dsn": "", "audit_service": "x"}
    base.update(overrides)
    return SimpleNamespace(**base)


class TestArgumentHashing:
    def test_same_arguments_same_hash_regardless_of_key_order(self):
        assert input_hash({"a": 1, "b": 2}) == input_hash({"b": 2, "a": 1})

    def test_different_arguments_differ(self):
        assert input_hash({"chat_id": 1}) != input_hash({"chat_id": 2})

    def test_no_arguments_still_hashes(self):
        assert len(input_hash(None)) == 64

    def test_values_never_appear_in_the_hash(self):
        assert "secret-chat" not in input_hash({"query": "secret-chat"})

    def test_unserialisable_values_collapse_to_their_type(self):
        # An object's repr carries a memory address: hashing it would make every
        # call unique and the journal useless for spotting repeats.
        value = MagicMock()
        assert _scrub({"ctx": value}) == {"ctx": "<MagicMock>"}
        assert input_hash({"ctx": value}) == input_hash({"ctx": MagicMock()})


class TestSessionFingerprint:
    def test_token_is_never_stored_verbatim(self, monkeypatch):
        monkeypatch.setattr(
            "src.client.connection.get_request_token", lambda: "super-secret-token"
        )
        fingerprint = _session_fingerprint()
        assert fingerprint is not None
        assert "super-secret-token" not in fingerprint
        assert fingerprint.startswith("tok:")

    def test_no_session_yields_no_subject(self, monkeypatch):
        monkeypatch.setattr("src.client.connection.get_request_token", lambda: None)
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token", lambda: None
        )
        assert _session_fingerprint() is None


class TestCallRecording:
    @pytest.mark.asyncio
    async def test_successful_call_is_recorded_and_result_passed_through(self):
        middleware = AuditMiddleware("postgresql:///unused", "telegram-test")
        written: list = []

        async def record(row):
            written.append(row)

        middleware._write = record  # type: ignore[assignment]

        context = SimpleNamespace(
            message=SimpleNamespace(name="search_messages", arguments={"query": "hi"}),
            fastmcp_context=SimpleNamespace(request_id="req-1"),
        )

        async def call_next(_ctx):
            return "result"

        assert await middleware.on_call_tool(context, call_next) == "result"
        await asyncio.sleep(0)  # let the detached write task run
        (service, _subject, _scopes, tool, request_id, latency, ok, _rows, sha,
         note), = written
        assert (service, tool, request_id, ok, note) == (
            "telegram-test", "search_messages", "req-1", True, None)
        assert latency >= 0 and len(sha) == 64

    @pytest.mark.asyncio
    async def test_failing_call_records_the_error_class_and_still_raises(self):
        middleware = AuditMiddleware("postgresql:///unused", "telegram-test")
        written: list = []

        async def record(row):
            written.append(row)

        middleware._write = record  # type: ignore[assignment]

        context = SimpleNamespace(
            message=SimpleNamespace(name="send_message", arguments={"chat_id": 7}),
            fastmcp_context=SimpleNamespace(request_id="req-2"),
        )

        async def call_next(_ctx):
            raise ValueError("chat 7 not found")

        with pytest.raises(ValueError):
            await middleware.on_call_tool(context, call_next)
        await asyncio.sleep(0)  # let the detached write task run
        assert written[0][6] is False
        assert written[0][9] == "ValueError"
        # The message could quote a chat id or a phone number; only the class travels.
        assert "chat 7" not in str(written[0])

    @pytest.mark.asyncio
    async def test_a_broken_journal_never_breaks_the_tool(self):
        middleware = AuditMiddleware("postgresql://nobody@127.0.0.1:1/nothing", "t")
        context = SimpleNamespace(
            message=SimpleNamespace(name="get_messages", arguments=None),
            fastmcp_context=SimpleNamespace(request_id="req-3"),
        )

        async def call_next(_ctx):
            return "result"

        assert await middleware.on_call_tool(context, call_next) == "result"


class TestRegistration:
    def test_no_dsn_means_no_middleware(self):
        mcp = MagicMock()
        register_mcp_middleware(mcp, _config())
        mcp.add_middleware.assert_not_called()

    def test_dsn_attaches_the_middleware(self):
        mcp = MagicMock()
        register_mcp_middleware(mcp, _config(audit_dsn="postgresql:///journal"))
        assert mcp.add_middleware.call_count == 1
        assert isinstance(mcp.add_middleware.call_args[0][0], AuditMiddleware)

    def test_audit_is_attached_after_the_prefixing_middleware(self):
        mcp = MagicMock()
        register_mcp_middleware(
            mcp,
            _config(prefix_mcp_tools_with_account=True, audit_dsn="postgresql:///j"),
        )
        attached = [call[0][0] for call in mcp.add_middleware.call_args_list]
        assert isinstance(attached[-1], AuditMiddleware)
