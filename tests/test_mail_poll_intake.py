"""Tests for PRIORITIES.yaml newsletter senders read in mail_poll.

Verifies that ``intake.newsletter_<grain>.senders`` supersedes the
legacy inline ``sources.newsletter-<grain>.senders`` in sources.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.discover.strategies.mail_poll import MailPollStrategy


class _FakeVault:
    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.config = self


def _write_priorities(vault_root: Path, body: str) -> None:
    cfg = vault_root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "PRIORITIES.yaml").write_text(body, encoding="utf-8")


def _newsletter_spec() -> dict:
    return {
        "mail_provider": "gmail",
        "processed_label": "mem-processed",
        "lookback_days": 30,
        "senders": ["legacy@spec.com"],  # legacy inline senders
    }


def test_priorities_senders_supersede_inline(tmp_path: Path):
    _write_priorities(
        tmp_path,
        "intake:\n"
        "  newsletter_events:\n"
        "    senders: [new@priorities.com, second@p.com]\n",
    )
    vault = _FakeVault(tmp_path)
    strategy = MailPollStrategy()

    out = strategy.run(
        vault,
        project=None,
        config={"sources": {"newsletter-events": _newsletter_spec()}},
    )

    assert len(out) == 1
    plan = out[0]
    assert plan["kind"] == "mail_fetch_needed"
    assert plan["senders"] == ["new@priorities.com", "second@p.com"]
    # Composed Gmail query references the new senders
    assert "from:(new@priorities.com OR second@p.com)" in plan["effective_query"]


def test_legacy_senders_used_when_priorities_absent(tmp_path: Path):
    # No PRIORITIES.yaml → fall back to inline senders in the spec
    vault = _FakeVault(tmp_path)
    strategy = MailPollStrategy()

    out = strategy.run(
        vault,
        project=None,
        config={"sources": {"newsletter-events": _newsletter_spec()}},
    )

    plan = out[0]
    assert plan["senders"] == ["legacy@spec.com"]
    assert "from:(legacy@spec.com)" in plan["effective_query"]


def test_empty_priorities_senders_falls_through(tmp_path: Path):
    """An empty senders list in PRIORITIES yields fall-through to spec senders."""
    _write_priorities(
        tmp_path,
        "intake:\n"
        "  newsletter_events:\n"
        "    senders: []\n",
    )
    vault = _FakeVault(tmp_path)
    strategy = MailPollStrategy()

    out = strategy.run(
        vault,
        project=None,
        config={"sources": {"newsletter-events": _newsletter_spec()}},
    )

    plan = out[0]
    # Empty list in priorities → falls through to legacy spec senders
    assert plan["senders"] == ["legacy@spec.com"]


def test_empty_allowlist_returns_error_pointing_to_priorities(tmp_path: Path):
    """When both PRIORITIES and spec senders are empty, error hint
    points the user at PRIORITIES.yaml — not sources.yaml."""
    vault = _FakeVault(tmp_path)
    strategy = MailPollStrategy()
    empty_spec = dict(_newsletter_spec())
    empty_spec["senders"] = []
    empty_spec["mail_query"] = ""

    out = strategy.run(
        vault,
        project=None,
        config={"sources": {"newsletter-events": empty_spec}},
    )

    plan = out[0]
    assert plan["status"] == "error"
    assert plan["reason"] == "empty_allowlist"
    assert "PRIORITIES.yaml" in plan["hint"]
    assert "intake.newsletter_events.senders" in plan["hint"]
