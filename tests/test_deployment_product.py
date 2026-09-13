"""Static acceptance checks for the portable deployment product."""

import importlib.util
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_smoke_module() -> ModuleType:
    """Import scripts/mcp_smoke.py by path; it is a script, not a package module."""
    spec = importlib.util.spec_from_file_location(
        "mcp_smoke", ROOT / "scripts/mcp_smoke.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_no_service_publishes_host_ports() -> None:
    for name, service in compose()["services"].items():
        assert "ports" not in service, f"{name} must not publish a host port"


def test_only_required_services_have_egress() -> None:
    document = compose()
    assert document["networks"]["mcp-private"]["internal"] is True
    assert document["services"]["telegram-mcp"]["networks"] == [
        "mcp-private",
        "telegram-egress",
    ]
    assert document["services"]["oauth-proxy"]["networks"] == ["mcp-private"]
    assert document["services"]["setup"]["networks"] == [
        "mcp-private",
        "telegram-egress",
    ]
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
        "PROMPTS/REPORT_INSTALLATION_WITH_AI_RU.md",
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
        "scripts/configure_container_identity.py",
        "scripts/telegram-login-web.sh",
        "infra/docker/compose.setup.yml",
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
    assert "telegram-login-web.sh" in prompt
    assert "не проси пользователя открывать терминал" in " ".join(
        prompt.lower().split()
    )


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
    assert "UPDATE_CHANNEL:-stable" in script
    assert "refs/tags/v[0-9]*" in script
    assert 'git switch --detach "$previous"' in script
    assert "TELEGRAM_SMOKE" in script


def test_bootstrap_does_not_require_uncreated_runtime_configuration() -> None:
    script = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    assert '"$REPO_ROOT/scripts/preflight.sh" base' in script
    assert '"$REPO_ROOT/scripts/preflight.sh" config' not in script
    assert "scripts/init-secrets.sh" in script


def test_secret_permission_check_is_portable(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("synthetic\n", encoding="utf-8")
    secret.chmod(0o600)
    subprocess.run(
        [
            "bash",
            "-c",
            'source scripts/lib/common.sh; secret_file_ok "$1"',
            "secret-check",
            str(secret),
        ],
        cwd=ROOT,
        check=True,
    )


def test_container_identity_is_configurable_and_not_root() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ARG APP_UID=10001" in dockerfile
    assert "ARG APP_GID=10001" in dockerfile
    assert "APP_UID: ${APP_UID:-10001}" in compose_text
    assert 'user: "${APP_UID:-10001}:${APP_GID:-10001}"' in compose_text
    assert "USER ${APP_UID}:${APP_GID}" in dockerfile
    assert "adduser" not in dockerfile


def test_cli_setup_cannot_read_unrelated_runtime_secrets() -> None:
    volumes = compose()["services"]["setup"]["volumes"]
    assert volumes == [
        "telegram-sessions:/data/sessions",
        "./secrets/backend_bearer:/run/secrets/backend_bearer",
    ]


def test_setup_portal_is_loopback_only() -> None:
    override = yaml.safe_load(
        (ROOT / "infra/docker/compose.setup.yml").read_text(encoding="utf-8")
    )
    ports = override["services"]["telegram-mcp"]["ports"]
    assert ports == ["127.0.0.1:${TELEGRAM_SETUP_PORT:-8765}:8000"]
    service = override["services"]["telegram-mcp"]
    assert service["environment"]["SETUP_DESIRED_TOKEN_FILE"] == (
        "/run/secrets/backend_bearer"
    )
    assert service["volumes"] == [
        "./secrets/backend_bearer:/run/secrets/backend_bearer:ro"
    ]


def test_noninteractive_prompts_tolerate_pipe_eof() -> None:
    cloudflare = (ROOT / "scripts/cloudflare-provision.sh").read_text(encoding="utf-8")
    backup = (ROOT / "scripts/backup.sh").read_text(encoding="utf-8")
    assert "CLOUDFLARE_API_TOKEN || true" in cloudflare
    assert "BACKUP_PASSPHRASE || true" in backup
    assert "backup encryption passphrase is empty" in backup


def test_public_smoke_uses_explicit_user_agent() -> None:
    smoke = (ROOT / "scripts/mcp_smoke.py").read_text(encoding="utf-8")
    assert 'USER_AGENT = "telegram-mcp-healthcheck/1"' in smoke
    assert '"User-Agent": USER_AGENT' in smoke


def test_qr_expiry_regenerates_without_terminal() -> None:
    template = (ROOT / "src/templates/fragments/qr_expired.html").read_text(
        encoding="utf-8"
    )
    assert 'hx-trigger="load delay:1s"' in template


def test_public_safety_ignores_github_synthetic_merge_metadata_only() -> None:
    scanner = (ROOT / "scripts/check_public_safety.py").read_text(encoding="utf-8")
    assert '"--no-merges"' in scanner
    assert "for revision in git(\"rev-list\", \"--all\")" in scanner


def test_docker_validation_uses_public_example_env_files() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "APP_ENV_FILE=.env.example" in makefile
    assert "RUNTIME_ENV_FILE=.runtime.env.example" in makefile
    assert "${APP_ENV_FILE:-./.env}" in compose_text
    assert "${RUNTIME_ENV_FILE:-./.runtime.env}" in compose_text


def test_deploy_waits_for_tunnel_registration_before_public_probe() -> None:
    """The public probe must run only after the connector registered an edge connection."""
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    common = (ROOT / "scripts/lib/common.sh").read_text(encoding="utf-8")
    assert "wait_for_tunnel_registration" in common
    assert "Registered tunnel connection" in common
    wait_at = deploy.index("wait_for_tunnel_registration")
    probe_at = deploy.index("scripts/healthcheck.sh")
    assert wait_at < probe_at, (
        "tunnel readiness must be gated before the public healthcheck"
    )


def test_tunnel_wait_fails_closed_when_connector_never_registers(
    tmp_path: Path,
) -> None:
    """A connector that never registers must fail the deployment, not pass it silently."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    docker.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/lib/common.sh; TUNNEL_READY_TIMEOUT=1 wait_for_tunnel_registration",
        ],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "did not register a tunnel connection" in result.stderr


def test_public_probe_retries_transient_failures_then_succeeds() -> None:
    """One transient edge error must not fail an otherwise healthy deployment."""
    smoke = _load_smoke_module()
    attempts = {"n": 0}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _flaky(_request, timeout=0):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.URLError("transient edge failure")
        return _Response()

    with mock.patch.object(smoke.urllib.request, "urlopen", _flaky):
        status = smoke.open_with_retries(
            urllib.request.Request("https://mcp.example.com/.well-known/x"),
            timeout=1,
            sleep=lambda _seconds: None,
        )
    assert status == 200
    assert attempts["n"] == 3


def test_public_probe_gives_up_after_bounded_attempts() -> None:
    """Retries are bounded: a genuinely unreachable endpoint still fails the deployment."""
    smoke = _load_smoke_module()

    def _always_down(_request, timeout=0):
        raise urllib.error.URLError("edge down")

    with (
        mock.patch.object(smoke.urllib.request, "urlopen", _always_down),
        pytest.raises(RuntimeError, match="did not answer after"),
    ):
        smoke.open_with_retries(
            urllib.request.Request("https://mcp.example.com/.well-known/x"),
            timeout=1,
            attempts=3,
            sleep=lambda _seconds: None,
        )
