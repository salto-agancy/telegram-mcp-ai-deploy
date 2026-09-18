"""Updating the updater must not lose the deploy it was in the middle of.

Observed in production on 18.09: a commit that touched the updater itself made
it hand over to its new version, which started from the top, saw the checkout
already at the target commit, and exited through the ordinary "nothing to do"
path. The run reported success, the disk was updated, and the containers kept
serving the code they already had in memory. Nothing said so.

The script is exercised as a whole with git and docker faked, because the bug
lived in exactly the interaction between them.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UPDATER = ROOT / "scripts" / "auto_update.sh"


def _modern_bash() -> str | None:
    """A bash that has associative arrays, which the updater has always needed.

    macOS ships bash 3.2 as /bin/bash for licensing reasons; the deployment runs
    bash 5. Skipping beats pretending the script is broken.
    """
    for candidate in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", "bash"):
        try:
            out = subprocess.run(
                [candidate, "-c", "echo ${BASH_VERSINFO[0]}"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        version = out.stdout.strip()
        if out.returncode == 0 and version.isdigit() and int(version) >= 4:
            return candidate
    return None


BASH = _modern_bash()
pytestmark = pytest.mark.skipif(
    BASH is None, reason="needs bash 4+ for associative arrays (macOS ships 3.2)"
)


@pytest.fixture
def deployment(tmp_path):
    """A checkout, a fake git and a fake docker that record what was asked."""
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    # The real script, not a placeholder: the handover re-executes this path, and
    # a stub there would make the test pass while proving nothing.
    deployed = app / "scripts" / "auto_update.sh"
    deployed.write_text(UPDATER.read_text(encoding="utf-8"), encoding="utf-8")
    deployed.chmod(0o755)  # the handover execs this path
    # The script reads the version from here when it decides to rebuild.
    (app / "pyproject.toml").write_text('version = "0.0.0"\n', encoding="utf-8")
    log = tmp_path / "calls.log"
    # Real compose directories, so `cd` succeeds and the restart actually happens.
    service = tmp_path / "service"
    (service / "personal").mkdir(parents=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text(
        "#!/bin/bash\n"
        'echo "git $*" >> "$CALL_LOG"\n'
        'case "$1 $2" in\n'
        '  "rev-parse HEAD") echo "${FAKE_CURRENT}" ;;\n'
        '  "rev-parse origin/release") echo "${FAKE_TARGET}" ;;\n'
        '  "log --oneline") echo "${FAKE_TARGET:0:7} a commit" ;;\n'
        '  "diff --quiet")\n'
        "    # Only the updater is reported as changed; anything else would send\n"
        "    # the run into an image rebuild that does not exist here.\n"
        '    case "$*" in\n'
        '      *auto_update.sh*) exit "${FAKE_UPDATER_CHANGED:-1}" ;;\n'
        "      *) exit 0 ;;\n"
        "    esac ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").write_text(
        '#!/bin/bash\necho "docker $*" >> "$CALL_LOG"\nexit 0\n', encoding="utf-8"
    )
    # Health is decided by the HTTP code AND the body, so the fake must write the
    # body where -o points. Without it the check fails and the script spends
    # twenty five-second retries per account before giving up — which is exactly
    # how this test first timed out in CI while passing on a machine where a
    # leftover health file from a real run happened to say "healthy".
    (bin_dir / "curl").write_text(
        "#!/bin/bash\n"
        'echo "curl $*" >> "$CALL_LOG"\n'
        "out=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        '  [ "$1" = "-o" ] && { out="$2"; shift; }\n'
        "  shift\n"
        "done\n"
        '[ -n "$out" ] && printf \'{"status":"healthy"}\' > "$out"\n'
        "echo 200\n",
        encoding="utf-8",
    )
    for name in ("git", "docker", "curl"):
        (bin_dir / name).chmod(0o755)

    def run(**extra_env):
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "APP_DIR": str(app),
            "SERVICE_DIR": str(service),
            "CONTAINER_PREFIX": "test-",
            "DEPLOY_ACCOUNTS": "personal",
            "DEPLOY_PORTS": "personal=8820",
            "DEPLOY_CONFIG": str(tmp_path / "absent.env"),
            "FAKE_CURRENT": "a" * 40,
            "FAKE_TARGET": "b" * 40,
            **extra_env,
        }
        proc = subprocess.run(
            [BASH, str(UPDATER)], env=env, capture_output=True, text=True, timeout=60
        )
        calls = log.read_text(encoding="utf-8") if log.exists() else ""
        return proc, calls

    return run


def test_resumed_run_still_restarts_the_containers(deployment):
    """The handover says where it came from, so the deploy finishes."""
    proc, calls = deployment(
        TG_UPDATER_RELOADED="1",
        TG_UPDATER_FROM="a" * 40,
        FAKE_CURRENT="b" * 40,  # checkout already at target, as after the handover
    )

    assert "docker" in calls, f"nothing was restarted; output:\n{proc.stdout}{proc.stderr}"
    assert "resumed after updating myself" in proc.stdout


def test_a_genuinely_current_deployment_still_does_nothing(deployment):
    proc, calls = deployment(FAKE_CURRENT="b" * 40)

    assert proc.returncode == 0
    assert "docker" not in calls, "an up-to-date deployment was restarted for no reason"


def test_an_ordinary_update_restarts_without_any_handover(deployment):
    """The updater did not change, so there is nothing to hand over."""
    proc, calls = deployment(FAKE_UPDATER_CHANGED="0")

    assert "docker" in calls
    assert "resumed after updating myself" not in proc.stdout


def test_a_changed_updater_hands_over_and_the_deploy_completes(deployment):
    """The whole path end to end: hand over, resume, restart."""
    proc, calls = deployment(FAKE_UPDATER_CHANGED="1")

    assert "the updater itself changed" in proc.stdout
    assert "resumed after updating myself" in proc.stdout
    assert "docker" in calls, "the handover lost the restart"
