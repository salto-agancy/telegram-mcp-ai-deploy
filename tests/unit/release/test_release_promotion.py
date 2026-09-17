"""The rule that moves `release` must be provable, not observed.

A server follows `release` and deploys whatever it points at, so the decision to
move that branch is a security boundary. The workflow decides from the completed
workflow runs on one commit; this exercises that decision as data, because the
real thing depends on GitHub scheduling that cannot be reproduced on demand.

The four cases that matter: everything green promotes, any failure refuses, an
unfinished check waits rather than promotes, and results from a different commit
never count toward this one.
"""

from __future__ import annotations

import pytest

REQUIRED = ("CI", "Secret scan")


def decide(runs: list[dict], sha: str) -> str:
    """Mirror of the workflow's promotion rule.

    Returns "promote", "refuse" or "wait". Kept deliberately small and literal so
    it can be read against the YAML line by line.
    """
    # Only completed runs on this exact commit are evidence about this commit.
    relevant = [
        r for r in runs
        if r["head_sha"] == sha
        and r["status"] == "completed"
        and r["name"] in REQUIRED
    ]
    if any(r["conclusion"] != "success" for r in relevant):
        return "refuse"
    finished = {r["name"] for r in relevant}
    if not all(name in finished for name in REQUIRED):
        return "wait"
    return "promote"


def run(name: str, sha: str, conclusion: str = "success", status: str = "completed") -> dict:
    return {"name": name, "head_sha": sha, "status": status, "conclusion": conclusion}


SHA = "a" * 40
OTHER = "b" * 40


def test_all_required_checks_green_promotes():
    runs = [run("CI", SHA), run("Secret scan", SHA)]
    assert decide(runs, SHA) == "promote"


def test_failing_tests_refuse():
    runs = [run("CI", SHA, "failure"), run("Secret scan", SHA)]
    assert decide(runs, SHA) == "refuse"


def test_failing_secret_scan_refuses_even_when_tests_pass():
    """The case this rule exists for: green tests, leaked secret."""
    runs = [run("CI", SHA), run("Secret scan", SHA, "failure")]
    assert decide(runs, SHA) == "refuse"


def test_unfinished_check_waits_rather_than_promoting():
    """A check still running is not a pass. Promoting here would be a race."""
    runs = [run("CI", SHA)]
    assert decide(runs, SHA) == "wait"


def test_in_progress_run_is_not_evidence():
    runs = [run("CI", SHA), run("Secret scan", SHA, conclusion=None, status="in_progress")]
    assert decide(runs, SHA) == "wait"


def test_green_checks_from_another_commit_do_not_count():
    """Two commits in flight must not lend each other their results."""
    runs = [run("CI", OTHER), run("Secret scan", OTHER), run("CI", SHA)]
    assert decide(runs, SHA) == "wait"


def test_failure_on_another_commit_does_not_block_this_one():
    runs = [run("CI", OTHER, "failure"), run("CI", SHA), run("Secret scan", SHA)]
    assert decide(runs, SHA) == "promote"


def test_no_runs_at_all_waits():
    assert decide([], SHA) == "wait"


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "skipped", "neutral"])
def test_any_non_success_conclusion_refuses(conclusion):
    """Only an explicit success counts. Cancelled is not "fine"."""
    runs = [run("CI", SHA, conclusion), run("Secret scan", SHA)]
    assert decide(runs, SHA) == "refuse"


def test_unrelated_workflow_does_not_satisfy_a_requirement():
    runs = [run("CI", SHA), run("Graph Update", SHA)]
    assert decide(runs, SHA) == "wait"
