"""Cross-harness contract for the first-run onboarding skill (issue #134)."""

from pathlib import Path


ONBOARD = Path("commands/onboard.md")


def test_onboard_routes_history_to_the_running_harness():
    text = ONBOARD.read_text(encoding="utf-8")
    assert "Codex" in text
    assert "weave import codex" in text
    assert "weave import claude-code" in text
    assert "Do not merge the two history lanes" in text
    assert ".weave/onboarding/claude_code.json" in text
    assert ".weave/onboarding/codex.json" in text


def test_onboard_routes_hook_install_to_codex():
    text = ONBOARD.read_text(encoding="utf-8")
    assert "weave hooks install --scope user --harness codex" in text
    assert "trust" in text.lower()


def test_onboard_smoke_test_requires_semantic_retrieval():
    text = ONBOARD.read_text(encoding="utf-8")
    assert "weave search --mode similar" in text
    assert "weave search --mode hybrid" in text
    assert "FTS-only" in text
