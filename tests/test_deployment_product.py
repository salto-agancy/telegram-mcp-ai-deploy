"""Static acceptance checks for the portable deployment product."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_no_service_publishes_host_ports() -> None:
    for name, service in compose()["services"].items():
        assert "ports" not in service, f"{name} must not publish a host port"


def test_tunnel_is_only_egress_bridge() -> None:
    document = compose()
    assert document["networks"]["mcp-private"]["internal"] is True
    assert document["services"]["telegram-mcp"]["networks"] == ["mcp-private"]
    assert document["services"]["oauth-proxy"]["networks"] == ["mcp-private"]
    assert document["services"]["cloudflared"]["networks"] == [
        "mcp-private",
        "tunnel-egress",
    ]


def test_fail_closed_compose_defaults() -> None:
    environment = compose()["services"]["telegram-mcp"]["environment"]
    assert environment["ACL_ENABLED"] == "true"
    assert environment["ACL_DENY_UNLISTED_PRINCIPALS"] == "true"
    assert environment["DO_NOT_TRACK"] == "1"
    assert "false" in str(environment["ENABLE_RAW_MTPROTO"])


def test_installer_deliverables_exist() -> None:
    required = [
        "AGENTS.md",
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "INSTALL_WITH_AI.md",
        "Makefile",
        "SECURITY.md",
        "acl.example.yaml",
        "cloudflare.example.yaml",
        "PROMPTS/CONTRIBUTE_WITH_AI.md",
        "docs/FUTURE_ARCHITECTURE.md",
        "docs/MAINTAINER_WORKFLOW.md",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/ISSUE_TEMPLATE/feature.yml",
        ".github/ISSUE_TEMPLATE/deployment.yml",
        ".github/ISSUE_TEMPLATE/client.yml",
        ".github/workflows/release.yml",
        "scripts/preflight.sh",
        "scripts/bootstrap.sh",
        "scripts/deploy.sh",
        "scripts/update.sh",
        "scripts/healthcheck.sh",
        "scripts/backup.sh",
        "scripts/cloudflare-provision.sh",
        "templates/claude-code/.mcp.json.example",
        "templates/codex/config.toml.example",
    ]
    assert all((ROOT / item).is_file() for item in required)


def test_master_prompt_requires_verified_and_read_only_smoke() -> None:
    prompt = (ROOT / "INSTALL_WITH_AI.md").read_text(encoding="utf-8")
    assert "DEPLOYMENT PASSED" in prompt
    assert "TELEGRAM_SMOKE=true" in prompt
    assert "никогда не отправляй, не редактируй и не удаляй" in prompt.lower()
    assert "https://github.com/salto-agancy/telegram-mcp-ai-deploy" in prompt
    assert "публичного репозитория" in prompt


def test_public_examples_are_fail_closed() -> None:
    acl = yaml.safe_load((ROOT / "acl.example.yaml").read_text(encoding="utf-8"))
    principal = acl["principals"]["REPLACE_WITH_BACKEND_BEARER"]
    assert principal["chats"] == ["me"]
    assert principal["read_only"] is True
    assert principal["allow_mtproto"] is False


def test_runtime_state_is_gitignored() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in [
        ".env.*",
        ".runtime.env.*",
        "secrets/",
        "backups/",
        "*.session",
        "acl.production.yaml",
        "tunnel-credentials.json",
        "*.tfstate",
    ]:
        assert entry in ignore


def test_update_defaults_to_stable_tags_and_can_rollback() -> None:
    script = (ROOT / "scripts/update.sh").read_text(encoding="utf-8")
    assert 'UPDATE_CHANNEL:-stable' in script
    assert "refs/tags/v[0-9]*" in script
    assert "git switch --detach \"$previous\"" in script
    assert "TELEGRAM_SMOKE" in script


def test_bootstrap_does_not_require_uncreated_runtime_configuration() -> None:
    script = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    assert '"$REPO_ROOT/scripts/preflight.sh" base' in script
    assert '"$REPO_ROOT/scripts/preflight.sh" config' not in script
    assert "scripts/init-secrets.sh" in script
