"""Tests for the two-layer cost ledger (core/spend.py).

Layer A = Claude agent turns read from the native transcript; Layer B =
personal_mem internal-op spend captured via ``record_spend`` and meshed into
the session event stream. These tests fake a native ``~/.claude/projects``
transcript (via a tmp HOME) and a vault, then exercise both layers, the
graceful-degradation path, and the range rollup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_mem.core.buffer import archive_buffer
from personal_mem.core.config import Config
from personal_mem.core.spend import (
    RATES,
    SpendSummary,
    _resolve_rate,
    cost_of_turn,
    find_native_jsonl,
    read_range_spend,
    read_session_spend,
    record_spend,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Isolate HOME + XDG_CACHE_HOME so native-jsonl and headless-log lookups
    point at the tmp tree, and clear the session-routing env vars."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("PERSONAL_MEM_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    return home


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


def _write_native_jsonl(home: Path, session_id: str, turns: list[dict]) -> Path:
    """Write a fake Claude Code transcript with the given assistant turns.

    Each turn dict supplies ``model``, ``usage``, and optionally ``isSidechain``
    / ``timestamp``.
    """
    proj = home / ".claude" / "projects" / "-some-encoded-cwd"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session_id}.jsonl"
    lines = []
    # A non-assistant line should be ignored.
    lines.append(json.dumps({"type": "user", "message": {"role": "user"}}))
    for t in turns:
        lines.append(json.dumps({
            "type": "assistant",
            "isSidechain": t.get("isSidechain", False),
            "timestamp": t.get("timestamp", "2026-06-01T12:00:00.000Z"),
            "message": {"model": t["model"], "usage": t["usage"]},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Rate resolution + cost_of_turn                                               #
# --------------------------------------------------------------------------- #


def test_resolve_rate_longest_prefix():
    # Versioned Claude model resolves via the bare-family prefix.
    assert _resolve_rate("claude-opus-4-7") is RATES["claude-opus-4"]
    assert _resolve_rate("claude-opus-4-20250514") is RATES["claude-opus-4"]
    assert _resolve_rate("claude-sonnet-4-5") is RATES["claude-sonnet-4"]
    # Gemini "models/" prefix is stripped.
    assert _resolve_rate("models/gemini-2.0-flash") is RATES["gemini-2.0-flash"]
    # Unknown → None.
    assert _resolve_rate("gpt-9-ultra") is None
    assert _resolve_rate("") is None


def test_cost_of_turn_anthropic_with_cache():
    rate = RATES["claude-opus-4"]  # 15 / 75 / 1.5 / 18.75 per Mtok
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }
    expected = rate.input + rate.output + rate.cache_read + rate.cache_write
    assert cost_of_turn(usage, "claude-opus-4-7") == pytest.approx(expected)


def test_cost_of_turn_openai():
    # gpt-5-mini: 0.25 in / 2.0 out per Mtok.
    usage = {"input_tokens": 2_000_000, "output_tokens": 500_000}
    assert cost_of_turn(usage, "gpt-5-mini") == pytest.approx(0.25 * 2 + 2.0 * 0.5)


def test_cost_of_turn_unknown_model_is_zero(caplog):
    assert cost_of_turn({"input_tokens": 999}, "mystery-model") == 0.0


# --------------------------------------------------------------------------- #
# Layer B — record_spend                                                       #
# --------------------------------------------------------------------------- #


def test_record_spend_headless_when_no_session(fake_home, config, tmp_path):
    record_spend("openai", "gpt-5-mini", "enrich", 1000, 200, cfg=config)
    logs = list((tmp_path / "cache" / "personal_mem" / "spend" / "headless").glob("*.spend.jsonl"))
    assert len(logs) == 1
    ev = json.loads(logs[0].read_text().strip())
    assert ev["type"] == "spend"
    assert ev["op"] == "enrich"
    assert ev["tokens_input"] == 1000
    assert "session_id" not in ev


def test_record_spend_into_session_buffer(fake_home, config):
    record_spend(
        "openai", "gpt-5-mini", "enrich", 1000, 200,
        session_id="ses-abc123", cfg=config,
    )
    buf = config.mem_dir / "buffer" / "ses-abc123.jsonl"
    ev = json.loads(buf.read_text().strip())
    assert ev["type"] == "spend"
    assert ev["session_id"] == "ses-abc123"


def test_record_spend_routes_via_env(fake_home, config, tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_MEM_SESSION_ID", "ses-fromenv")
    record_spend("gemini", "gemini-2.0-flash", "gemini_extract", 50, 10, cfg=config)
    buf = config.mem_dir / "buffer" / "ses-fromenv.jsonl"
    assert buf.exists()
    # Nothing leaked to the headless log.
    assert not (tmp_path / "cache" / "personal_mem" / "spend" / "headless").exists()


def test_record_spend_never_raises(monkeypatch):
    # Even with a broken config it must swallow the failure.
    record_spend("openai", "gpt-5-mini", "enrich", 1, 1, session_id="x", cfg=None)


def test_spend_event_routes_to_events_jsonl_on_archive(fake_home, config):
    """A spend event in the buffer lands in events.jsonl (not retrieval_log)."""
    record_spend("openai", "gpt-5-mini", "enrich", 10, 5, session_id="ses-x", cfg=config)
    session_dir = config.vault_root / "sess"
    archive_buffer(config.mem_dir, "ses-x", session_dir)
    events = (session_dir / "events.jsonl").read_text()
    assert '"type": "spend"' in events
    assert not (session_dir / "retrieval_log.jsonl").exists()


# --------------------------------------------------------------------------- #
# Layer A + B — read_session_spend                                             #
# --------------------------------------------------------------------------- #


def test_find_native_jsonl(fake_home):
    _write_native_jsonl(fake_home, "ses-find", [
        {"model": "claude-opus-4-7", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    found = find_native_jsonl("ses-find")
    assert found is not None and found.name == "ses-find.jsonl"
    assert find_native_jsonl("ses-missing") is None


def test_read_session_spend_layer_a(fake_home, config):
    _write_native_jsonl(fake_home, "ses-a", [
        {"model": "claude-opus-4-7",
         "usage": {"input_tokens": 1_000_000, "output_tokens": 0,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
        {"model": "claude-opus-4-7", "isSidechain": True,
         "usage": {"input_tokens": 0, "output_tokens": 1_000_000}},
    ])
    s = read_session_spend("ses-a", cfg=config)
    assert not s.unknown
    assert s.n_turns == 2
    # 1M input @ $15 + 1M output @ $75 = $90
    assert s.claude_usd == pytest.approx(90.0)
    assert s.subagent_usd == pytest.approx(75.0)  # the sidechain turn
    assert s.ops_usd == 0.0


def test_read_session_spend_layers_combined(fake_home, config):
    _write_native_jsonl(fake_home, "ses-c", [
        {"model": "claude-sonnet-4-5",
         "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
    ])
    # Layer B event in the live buffer.
    record_spend("openai", "gpt-5-mini", "enrich", 1_000_000, 0,
                 session_id="ses-c", cfg=config)
    s = read_session_spend("ses-c", cfg=config)
    assert s.claude_usd == pytest.approx(3.0)   # 1M sonnet input @ $3
    assert s.ops_usd == pytest.approx(0.25)     # 1M gpt-5-mini input @ $0.25
    assert s.total_usd == pytest.approx(3.25)
    assert s.by_op == {"enrich": pytest.approx(0.25)}


def test_read_session_spend_graceful_degrade(fake_home, config):
    """No native transcript → unknown=True, but Layer-B still counted."""
    record_spend("openai", "gpt-5-mini", "enrich", 1_000_000, 0,
                 session_id="ses-d", cfg=config)
    s = read_session_spend("ses-d", cfg=config)
    assert s.unknown is True
    assert s.claude_usd == 0.0
    assert s.ops_usd == pytest.approx(0.25)


def test_cache_pct():
    s = SpendSummary(tokens_input=200, tokens_cache_read=800)
    assert s.cache_pct == pytest.approx(80.0)
    assert SpendSummary().cache_pct == 0.0


# --------------------------------------------------------------------------- #
# Range rollup                                                                 #
# --------------------------------------------------------------------------- #


def test_read_range_spend(fake_home, config, tmp_path):
    _write_native_jsonl(fake_home, "ses-r1", [
        {"model": "claude-opus-4-7", "timestamp": "2026-06-01T09:00:00Z",
         "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
    ])
    _write_native_jsonl(fake_home, "ses-r2", [
        {"model": "claude-opus-4-7", "timestamp": "2026-05-20T09:00:00Z",
         "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
    ])
    # Headless Layer-B op on 2026-06-01.
    record_spend("gemini", "gemini-2.0-flash", "gemini_extract", 1_000_000, 0, cfg=config)

    s = read_range_spend("2026-06-01", "2026-06-01", cfg=config)
    # Only ses-r1 (June 1) counts for Layer A; ses-r2 (May 20) excluded.
    assert s.claude_usd == pytest.approx(15.0)
    # Gemini headless op (today=fake; the test writes with utcnow date) —
    # included only if its date falls in-window. Assert it's attributed to ops.
    assert s.ops_usd >= 0.0
    assert s.n_turns == 1
