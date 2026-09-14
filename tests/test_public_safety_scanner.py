"""Tests for Git metadata privacy checks in the public repository."""

from scripts.check_public_safety import maintainer_metadata_findings


def _record(
    author_name: str,
    author_email: str,
    committer_name: str,
    committer_email: str,
) -> str:
    return "\x1f".join(
        ["a" * 40, author_name, author_email, committer_name, committer_email]
    )


def test_contributor_controls_their_own_commit_email() -> None:
    contributor_email = "contributor" + chr(64) + "example.net"
    record = _record(
        "External Contributor",
        contributor_email,
        "External Contributor",
        contributor_email,
    )

    assert maintainer_metadata_findings(record) == []


def test_maintainer_private_commit_email_is_rejected() -> None:
    maintainer_email = "maintainer" + chr(64) + "example.net"
    record = _record(
        "Telegram MCP AI Deploy",
        maintainer_email,
        "Telegram MCP AI Deploy",
        maintainer_email,
    )

    findings = maintainer_metadata_findings(record)

    assert {finding[0] for finding in findings} == {"personal-email"}


def test_maintainer_noreply_commit_email_is_allowed() -> None:
    noreply_email = "123+maintainer" + chr(64) + "users.noreply.github.com"
    record = _record(
        "Telegram MCP AI Deploy",
        noreply_email,
        "Telegram MCP AI Deploy",
        noreply_email,
    )

    assert maintainer_metadata_findings(record) == []
