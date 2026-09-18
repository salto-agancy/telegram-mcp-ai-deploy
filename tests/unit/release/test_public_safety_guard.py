"""The guard must not publish the list it exists to enforce.

The production-name rule used to be a regular expression spelling out hosting
providers and account handles. The repository is public, so the check that kept
those words out of the tree was the one file that printed them. Digests catch
the same words and name none of them.
"""

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from check_public_safety import (  # noqa: E402
    PRODUCTION_NAME_DIGESTS,
    production_name_findings,
)


def digest(word: str) -> str:
    return hashlib.sha256(word.lower().encode()).hexdigest()[:32]


@pytest.fixture
def banned_word(monkeypatch):
    """A word nobody would use by accident, added to the list for this test."""
    word = "zzqqxwarehouse"
    monkeypatch.setattr(
        "check_public_safety.PRODUCTION_NAME_DIGESTS",
        PRODUCTION_NAME_DIGESTS | {digest(word)},
    )
    return word


def test_a_banned_word_is_found(banned_word):
    found = production_name_findings("file.txt", f"deploy on {banned_word} box")

    assert found == [("production-name", "file.txt:1", 1)]


def test_the_finding_never_quotes_the_word(banned_word):
    found = production_name_findings("file.txt", f"deploy on {banned_word} box")

    assert banned_word not in repr(found), "the report leaked the word it guards"


def test_case_does_not_help(banned_word):
    assert production_name_findings("f", banned_word.upper())


def test_ordinary_text_is_left_alone():
    assert production_name_findings("f", "a normal deploy script, nothing here") == []


def test_the_guard_file_does_not_spell_the_words_out():
    """Whatever else changes, this file must stay publishable."""
    text = (ROOT / "scripts" / "check_public_safety.py").read_text(encoding="utf-8")

    for line in text.splitlines():
        for token in line.replace('"', " ").replace("'", " ").split():
            word = token.strip(",;:()[]{}")
            if len(word) > 3 and digest(word) in PRODUCTION_NAME_DIGESTS:
                pytest.fail("the guard spells out a name it is meant to hide")


def test_a_private_denylist_extends_the_check(tmp_path, monkeypatch):
    extra = tmp_path / "denylist.txt"
    extra.write_text("# comment\nwarehousename\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_MCP_DENYLIST", str(extra))

    assert production_name_findings("f", "the warehousename box")


def test_a_missing_denylist_does_not_crash_the_scan(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MCP_DENYLIST", "/nonexistent/denylist.txt")

    assert production_name_findings("f", "ordinary text") == []
