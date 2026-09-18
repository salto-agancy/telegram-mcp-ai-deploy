"""A connector should be able to say what its account actually holds.

Two connectors, one named "work" and one named "personal", are told apart by name
alone. The names misled: the work account had ten active chats over a month while
the personal one had a hundred and fifty nine, most of the actual work among
them. The model asked the emptier one first and reported near-nothing. Nothing in
the tool set said otherwise.
"""

import importlib

import pytest


@pytest.fixture
def tools_register(monkeypatch):
    def _apply(note=None):
        if note is None:
            monkeypatch.delenv("ACCOUNT_NOTE", raising=False)
        else:
            monkeypatch.setenv("ACCOUNT_NOTE", note)
        module = importlib.import_module("src.server_components.tools_register")
        return importlib.reload(module)

    return _apply


def test_the_note_reaches_the_tool_description(tools_register):
    mod = tools_register("main account: most work lives here, not in the other one")

    assert "most work lives here" in mod._DESC_SEARCH_GLOBAL
    assert "About this account:" in mod._DESC_RECENT_ACTIVITY


def test_without_a_note_descriptions_are_unchanged(tools_register):
    mod = tools_register(None)

    assert "About this account:" not in mod._DESC_SEARCH_GLOBAL


def test_whitespace_only_is_treated_as_no_note(tools_register):
    mod = tools_register("   ")

    assert "About this account:" not in mod._DESC_SEARCH_GLOBAL


def test_the_documentation_link_still_closes_every_description(tools_register):
    mod = tools_register("second account, small volume")

    assert mod._DESC_SEARCH_GLOBAL.rstrip().endswith(mod.TOOLS_REFERENCE_DOC_URL)
