"""Tests for the Haiku-based news triage helper.

Covers the deterministic surfaces (catalog extraction, prompt
assembly, response parsing, CLI plumbing). The Anthropic call itself
is stubbed — we only verify the request shape and the parser's
robustness to malformed responses, not the LLM's verdicts.

**Provider-swap pause (2026-05-21).** ``operations/news_triage`` is
temporarily on OpenAI ``gpt-5-mini`` (see its module docstring), so
the four tests that lock in the Anthropic ``system: [...]`` +
``cache_control: ephemeral`` request shape are marked ``xfail`` with
``strict=False`` — they'll auto-pass once ``ANTHROPIC_API_KEY`` is in
the env and the provider reverts to the long-term Haiku shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


# The triage helper is on a temporary provider swap (Anthropic → OpenAI);
# tests asserting the Anthropic prompt shape will pass again on revert.
_PROVIDER_SWAP_XFAIL = pytest.mark.xfail(
    reason=(
        "news_triage is temporarily on OpenAI gpt-5-mini per its module "
        "docstring; Anthropic shape (system blocks + cache_control) "
        "regression checks pause until ANTHROPIC_API_KEY is available."
    ),
    strict=False,
)

from personal_mem.operations.news_triage import (
    ALLOWED_VERDICTS,
    CATALOG_HEADING,
    DEFAULT_MODEL,
    VERDICT_DROP,
    VERDICT_KEEP,
    VERDICT_UNFILED,
    build_triage_messages,
    extract_catalog_section,
    parse_triage_response,
)


# ---------------------------------------------------------------------------
# extract_catalog_section
# ---------------------------------------------------------------------------


class TestExtractCatalog:
    def test_slices_catalog_to_next_h2(self):
        md = (
            "# Themes\n\n"
            "## Active (2)\n\n| Theme | ... |\n\n"
            f"{CATALOG_HEADING}\n\n"
            "### Theme A\n- **id:** `thm-a`\n\n> essence A\n\n"
            "### Theme B\n- **id:** `thm-b`\n\n> essence B\n\n"
            "## Dormant (1)\n\n| Theme | ... |\n"
        )
        section = extract_catalog_section(md)
        assert section.startswith(CATALOG_HEADING)
        assert "Theme A" in section
        assert "Theme B" in section
        # Stops before the next H2.
        assert "Dormant" not in section

    def test_returns_empty_when_section_absent(self):
        md = "# Themes\n\n## Active (0)\n\nNo themes recorded yet.\n"
        assert extract_catalog_section(md) == ""

    def test_takes_to_eof_when_no_later_h2(self):
        md = (
            f"{CATALOG_HEADING}\n\n"
            "### Solo theme\n\n> essence\n"
        )
        section = extract_catalog_section(md)
        assert "Solo theme" in section
        assert "essence" in section


# ---------------------------------------------------------------------------
# build_triage_messages
# ---------------------------------------------------------------------------


class TestBuildTriageMessages:
    @_PROVIDER_SWAP_XFAIL
    def test_request_shape(self):
        catalog = f"{CATALOG_HEADING}\n\n### Theme A\n> essence"
        items = [
            {"id": "q-1", "title": "Memory chip rally", "outlet": "cnbc", "tier": 1},
            {"id": "q-2", "title": "Sports gossip", "outlet": "marketwatch", "tier": 1},
        ]
        req = build_triage_messages(catalog, items)

        assert req["model"] == DEFAULT_MODEL
        # Two system blocks: instructions + cached catalog.
        assert isinstance(req["system"], list)
        assert len(req["system"]) == 2
        assert req["system"][0]["type"] == "text"
        # The cached block carries the catalog and the cache_control marker.
        cached = req["system"][1]
        assert cached.get("cache_control") == {"type": "ephemeral"}
        assert "Theme A" in cached["text"]
        # User message lists items by index.
        user = req["messages"][0]["content"]
        assert "1. [outlet=cnbc, tier=1] Memory chip rally" in user
        assert "2. [outlet=marketwatch, tier=1] Sports gossip" in user

    @_PROVIDER_SWAP_XFAIL
    def test_empty_catalog_uses_placeholder(self):
        """When the vault has no active themes, the catalog block falls
        back to a placeholder so the LLM's instructions still parse —
        the bias toward keep_unfiled in the system prompt then kicks in."""
        items = [{"id": "q-1", "title": "X", "outlet": "y", "tier": 1}]
        req = build_triage_messages("", items)
        cached_text = req["system"][1]["text"]
        assert "no active themes" in cached_text
        assert CATALOG_HEADING in cached_text


# ---------------------------------------------------------------------------
# parse_triage_response
# ---------------------------------------------------------------------------


def _items(*ids: str) -> list[dict]:
    return [{"id": i, "title": "t", "outlet": "o", "tier": 1} for i in ids]


class TestParseTriageResponse:
    def test_well_formed_json_parses(self):
        raw = json.dumps(
            {
                "1": {
                    "verdict": "keep",
                    "theme_id": "thm-aaaa1111",
                    "reason": "fits AI capex theme",
                },
                "2": {
                    "verdict": "drop",
                    "theme_id": None,
                    "reason": "sports",
                },
            }
        )
        out = parse_triage_response(raw, _items("q-1", "q-2"))
        assert out[0]["id"] == "q-1"
        assert out[0]["verdict"] == "keep"
        assert out[0]["theme_id"] == "thm-aaaa1111"
        assert out[1]["verdict"] == "drop"
        assert out[1]["theme_id"] is None

    def test_code_fenced_json_unwrapped(self):
        raw = (
            "```json\n"
            + json.dumps({"1": {"verdict": "keep_unfiled", "theme_id": None, "reason": "x"}})
            + "\n```"
        )
        out = parse_triage_response(raw, _items("q-1"))
        assert out[0]["verdict"] == "keep_unfiled"

    def test_invalid_json_drops_all_with_reason(self):
        out = parse_triage_response("garbage not json", _items("q-1", "q-2"))
        assert all(o["verdict"] == "drop" for o in out)
        assert all("invalid JSON" in o["reason"] for o in out)

    def test_missing_item_defaults_to_drop(self):
        # Response only has key for item 1; item 2 is missing.
        raw = json.dumps(
            {"1": {"verdict": "keep", "theme_id": "thm-x", "reason": "ok"}}
        )
        out = parse_triage_response(raw, _items("q-1", "q-2"))
        assert out[0]["verdict"] == "keep"
        assert out[1]["verdict"] == "drop"
        assert "no verdict" in out[1]["reason"]

    def test_unknown_verdict_collapses_to_drop(self):
        raw = json.dumps(
            {"1": {"verdict": "maybe", "theme_id": None, "reason": "unsure"}}
        )
        out = parse_triage_response(raw, _items("q-1"))
        assert out[0]["verdict"] == "drop"
        assert "unknown verdict" in out[0]["reason"]

    def test_invalid_theme_id_dropped_to_null(self):
        """An LLM that emits a non-thm- string for theme_id gets it
        scrubbed to null — we don't trust unverified ids into the
        graph."""
        raw = json.dumps(
            {
                "1": {
                    "verdict": "keep",
                    "theme_id": "ai-capex-2026",  # missing thm- prefix
                    "reason": "x",
                }
            }
        )
        out = parse_triage_response(raw, _items("q-1"))
        assert out[0]["theme_id"] is None

    def test_reason_truncated_at_200_chars(self):
        raw = json.dumps(
            {
                "1": {
                    "verdict": "drop",
                    "theme_id": None,
                    "reason": "x" * 500,
                }
            }
        )
        out = parse_triage_response(raw, _items("q-1"))
        assert len(out[0]["reason"]) == 200


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestCLI:
    @_PROVIDER_SWAP_XFAIL
    def test_dry_run_emits_drops_and_logs_request_to_stderr(
        self, tmp_path: Path
    ):
        themes = tmp_path / "THEMES.md"
        themes.write_text(
            f"# Themes\n\n{CATALOG_HEADING}\n\n### Theme A\n\n> essence\n",
            encoding="utf-8",
        )
        items = [
            {"id": "q-1", "title": "x", "outlet": "y", "tier": 1},
            {"id": "q-2", "title": "z", "outlet": "y", "tier": 1},
        ]
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "personal_mem.operations.news_triage",
                "--themes",
                str(themes),
                "--dry-run",
            ],
            input=json.dumps(items),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        # stderr carries the prepared request (for cache-key inspection).
        assert "messages" in proc.stderr
        assert "ephemeral" in proc.stderr
        # stdout carries the verdict list — all drops with reason "dry-run".
        out = json.loads(proc.stdout)
        assert len(out) == 2
        assert all(v["verdict"] == "drop" for v in out)
        assert all(v["reason"] == "dry-run" for v in out)

    def test_missing_themes_file_returns_nonzero(self, tmp_path: Path):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "personal_mem.operations.news_triage",
                "--themes",
                str(tmp_path / "does_not_exist.md"),
                "--dry-run",
            ],
            input="[]",
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 1
        assert "not found" in proc.stderr


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestVerdictConstants:
    def test_allowed_verdicts_set(self):
        assert ALLOWED_VERDICTS == {VERDICT_KEEP, VERDICT_UNFILED, VERDICT_DROP}

    @_PROVIDER_SWAP_XFAIL
    def test_default_model_is_haiku(self):
        # The triage layer should be cheap by default. If someone bumps
        # this to sonnet, tests should fail loudly so the change is
        # deliberate.
        assert DEFAULT_MODEL.startswith("claude-haiku-")
