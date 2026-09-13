"""Tests for portable non-root container identity selection."""

from scripts import configure_container_identity as identity


def test_nonroot_deploy_uses_invoking_operator(monkeypatch):
    monkeypatch.setattr(identity.os, "geteuid", lambda: 501)
    monkeypatch.setattr(identity.os, "getuid", lambda: 501)
    monkeypatch.setattr(identity.os, "getgid", lambda: 20)

    assert identity._resolved_identity(["APP_UID=auto\n", "APP_GID=auto\n"]) == (
        501,
        20,
    )


def test_root_keeps_explicit_unassigned_identity(monkeypatch):
    monkeypatch.setattr(identity.os, "geteuid", lambda: 0)
    monkeypatch.setattr(identity, "_host_uid_exists", lambda _value: False)
    monkeypatch.setattr(identity, "_host_gid_exists", lambda _value: False)

    assert identity._resolved_identity(["APP_UID=15000\n", "APP_GID=15001\n"]) == (
        15000,
        15001,
    )


def test_root_avoids_identity_owned_by_host_account(monkeypatch):
    monkeypatch.setattr(identity.os, "geteuid", lambda: 0)
    monkeypatch.setattr(identity, "_host_uid_exists", lambda value: value == 1000)
    monkeypatch.setattr(identity, "_host_gid_exists", lambda value: value == 1000)

    assert identity._resolved_identity(["APP_UID=1000\n", "APP_GID=1000\n"]) == (
        10001,
        10001,
    )


def test_replace_values_preserves_other_configuration():
    result = identity._replace_values(
        ["MCP_HOSTNAME=mcp.example.com\n", "APP_UID=auto\n"], 12000, 12001
    )

    assert result == [
        "MCP_HOSTNAME=mcp.example.com\n",
        "APP_UID=12000\n",
        "APP_GID=12001\n",
    ]
