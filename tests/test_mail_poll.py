"""Tests for the ``mail_poll`` discover strategy.

mail_poll is a planning strategy — it composes the effective Gmail query
from per-source-type config and emits a ``mail_fetch_needed`` descriptor.
The actual MCP-side fetch lives in the ``/newsletter`` skill, not here.
So tests cover query composition, allowlist enforcement, and
``_runtime.source_type`` filtering — no Gmail mocking needed.
"""

from __future__ import annotations

import pytest

from personal_mem.acquisition.discover.strategies.mail_poll import MailPollStrategy


def _cfg(**source_overrides):
    """Build a config with one newsletter-events source merged with overrides."""
    base = {
        "mail_connector": "gmail",
        "senders": ["news@stratechery.com", "matt@levine.com"],
        "mail_query": "is:unread",
        "processed_label": "mem-processed",
        "lookback_days": 30,
        "dedup_keys": ["message_id", "url"],
    }
    base.update(source_overrides)
    return {"sources": {"newsletter-events": base}}


def test_emits_mail_fetch_needed_for_gmail() -> None:
    out = MailPollStrategy().run(None, None, _cfg())
    assert len(out) == 1
    d = out[0]
    assert d["kind"] == "mail_fetch_needed"
    assert d["source_type"] == "newsletter-events"
    assert d["connector"] == "gmail"
    assert d["processed_label"] == "mem-processed"
    assert d["lookback_days"] == 30
    assert d["dedup_keys"] == ["message_id", "url"]


def test_composes_gmail_query_from_senders_and_extras() -> None:
    d = MailPollStrategy().run(None, None, _cfg())[0]
    q = d["effective_query"]
    assert "from:(news@stratechery.com OR matt@levine.com)" in q
    assert "is:unread" in q
    assert "-label:mem-processed" in q
    assert "newer_than:30d" in q


def test_composes_query_without_extras() -> None:
    d = MailPollStrategy().run(None, None, _cfg(mail_query=""))[0]
    q = d["effective_query"]
    assert "from:(news@stratechery.com OR matt@levine.com)" in q
    assert " is:unread" not in q
    assert "-label:mem-processed" in q
    assert "newer_than:30d" in q


def test_empty_senders_and_query_emits_error() -> None:
    out = MailPollStrategy().run(None, None, _cfg(senders=[], mail_query=""))
    assert len(out) == 1
    d = out[0]
    assert d["kind"] == "external"
    assert d["status"] == "error"
    assert d["reason"] == "empty_allowlist"
    assert "senders" in d["hint"]


def test_imap_connector_emits_not_implemented() -> None:
    out = MailPollStrategy().run(None, None, _cfg(mail_connector="imap"))
    assert len(out) == 1
    d = out[0]
    assert d["kind"] == "external"
    assert d["status"] == "error"
    assert d["reason"] == "connector_not_implemented"


def test_outlook_connector_emits_not_implemented() -> None:
    out = MailPollStrategy().run(None, None, _cfg(mail_connector="outlook"))
    assert out[0]["reason"] == "connector_not_implemented"


def test_mail_provider_alias_takes_precedence() -> None:
    """C21 rename: ``mail_provider`` is the new canonical name and
    overrides ``mail_connector`` when both are present. ``mail_connector``
    remains the back-compat fallback for vault configs predating the
    rename."""
    out = MailPollStrategy().run(
        None, None, _cfg(mail_provider="gmail", mail_connector="outlook")
    )
    # mail_provider wins → gmail path emits a fetch plan.
    assert out[0]["kind"] == "mail_fetch_needed"
    assert out[0]["connector"] == "gmail"


def test_back_compat_mail_connector_still_works() -> None:
    """Pre-C21 configs (``mail_connector:`` only, no ``mail_provider:``)
    continue to work without migration."""
    # _cfg already uses mail_connector — confirm the existing test
    # signal that pre-rename configs still produce fetch plans.
    out = MailPollStrategy().run(None, None, _cfg())
    assert out[0]["kind"] == "mail_fetch_needed"
    assert out[0]["connector"] == "gmail"


def test_skips_sources_without_mail_connector() -> None:
    """Non-mail sources (paper, repo, etc.) are skipped silently."""
    cfg = {
        "sources": {
            "paper": {"queue": "vault/.mem/queues/papers.jsonl"},
            "newsletter-events": {
                "mail_connector": "gmail",
                "senders": ["x@y.com"],
                "processed_label": "mem-processed",
                "lookback_days": 30,
            },
        }
    }
    out = MailPollStrategy().run(None, None, cfg)
    assert len(out) == 1
    assert out[0]["source_type"] == "newsletter-events"


def test_runtime_source_type_filter() -> None:
    cfg = {
        "_runtime": {"source_type": "newsletter-concepts"},
        "sources": {
            "newsletter-events": {
                "mail_connector": "gmail",
                "senders": ["x@y.com"],
                "processed_label": "mem-processed",
                "lookback_days": 30,
            },
            "newsletter-concepts": {
                "mail_connector": "gmail",
                "senders": ["z@w.com"],
                "processed_label": "mem-processed",
                "lookback_days": 90,
            },
        },
    }
    out = MailPollStrategy().run(None, None, cfg)
    assert len(out) == 1
    assert out[0]["source_type"] == "newsletter-concepts"


def test_lookback_zero_omits_newer_than_clause() -> None:
    """lookback_days: 0 means 'no lookback bound' — skip the newer_than clause."""
    d = MailPollStrategy().run(None, None, _cfg(lookback_days=0))[0]
    assert "newer_than" not in d["effective_query"]


def test_dedup_keys_default() -> None:
    """Missing dedup_keys defaults to [message_id, url]."""
    cfg = {
        "sources": {
            "newsletter-events": {
                "mail_connector": "gmail",
                "senders": ["x@y.com"],
                "processed_label": "mem-processed",
                "lookback_days": 30,
            }
        }
    }
    d = MailPollStrategy().run(None, None, cfg)[0]
    assert d["dedup_keys"] == ["message_id", "url"]
