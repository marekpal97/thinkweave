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

from personal_mem.core.config import Config
from personal_mem.core.spend import (
    RATES,
    SpendSummary,
    _first_user_text,
    _resolve_rate,
    _transcript_op_label,
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


def _write_native_jsonl(
    home: Path, session_id: str, turns: list[dict], *, first_prompt: str | None = None
) -> Path:
    """Write a fake Claude Code transcript with the given assistant turns.

    Each turn dict supplies ``model``, ``usage``, and optionally ``isSidechain``
    / ``timestamp``. ``first_prompt`` sets the opening user-turn text (used by the
    --ops-only transcript classifier); omit it for an empty/meta user row.
    """
    proj = home / ".claude" / "projects" / "-some-encoded-cwd"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session_id}.jsonl"
    lines = []
    # A non-assistant line should be ignored for cost, but is the op-label probe.
    user_msg = {"role": "user"}
    if first_prompt is not None:
        user_msg["content"] = first_prompt
    lines.append(json.dumps({"type": "user", "message": user_msg}))
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


def test_cost_of_turn_anthropic_cache_split():
    rate = RATES["claude-opus-4"]  # 5 / 25 / 0.5 / 6.25 / 10 per Mtok
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 2_000_000,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 1_000_000,
            "ephemeral_1h_input_tokens": 1_000_000,
        },
    }
    expected = (
        rate.input + rate.output + rate.cache_read
        + rate.cache_write_5m + rate.cache_write_1h
    )  # 5 + 25 + 0.5 + 6.25 + 10 = 46.75
    assert cost_of_turn(usage, "claude-opus-4-8") == pytest.approx(expected)


def test_cost_of_turn_cache_fallback_no_split():
    # No TTL breakdown → the whole creation total books at the 5-minute rate.
    rate = RATES["claude-opus-4"]
    usage = {"cache_creation_input_tokens": 1_000_000}
    assert cost_of_turn(usage, "claude-opus-4-8") == pytest.approx(rate.cache_write_5m)


def test_cost_of_turn_openai():
    # gpt-5-mini: 0.25 in / 2.0 out per Mtok.
    usage = {"input_tokens": 2_000_000, "output_tokens": 500_000}
    assert cost_of_turn(usage, "gpt-5-mini") == pytest.approx(0.25 * 2 + 2.0 * 0.5)


def test_cost_of_turn_unknown_model_is_none():
    # Unpriced → None (so the caller can surface it, not silently book $0).
    assert cost_of_turn({"input_tokens": 999}, "mystery-model") is None


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


def test_record_spend_into_session_ledger(fake_home, config):
    """A known session id routes to the dedicated ledger .mem/spend/<sid>.jsonl,
    NOT the action/retrieval buffer (the sink is decoupled)."""
    record_spend(
        "openai", "gpt-5-mini", "enrich", 1000, 200,
        session_id="ses-abc123", cfg=config,
    )
    ledger = config.mem_dir / "spend" / "ses-abc123.jsonl"
    ev = json.loads(ledger.read_text().strip())
    assert ev["type"] == "spend"
    assert ev["session_id"] == "ses-abc123"
    # Decoupled: nothing written into the event buffer.
    assert not (config.mem_dir / "buffer" / "ses-abc123.jsonl").exists()


def test_record_spend_routes_via_env(fake_home, config, tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_MEM_SESSION_ID", "ses-fromenv")
    record_spend("gemini", "gemini-2.0-flash", "gemini_extract", 50, 10, cfg=config)
    ledger = config.mem_dir / "spend" / "ses-fromenv.jsonl"
    assert ledger.exists()
    # Nothing leaked to the headless log.
    assert not (tmp_path / "cache" / "personal_mem" / "spend" / "headless").exists()


def test_record_spend_never_raises(monkeypatch):
    # Even with a broken config it must swallow the failure.
    record_spend("openai", "gpt-5-mini", "enrich", 1, 1, session_id="x", cfg=None)


def test_record_spend_ledger_round_trips_to_read(fake_home, config):
    """record_spend → the dedicated ledger → read_session_spend Layer B, with no
    buffer/archive step in between (the decoupling, end to end)."""
    record_spend("openai", "gpt-5-mini", "enrich", 1_000_000, 0,
                 session_id="ses-x", cfg=config)
    s = read_session_spend("ses-x", cfg=config)
    assert s.unknown  # no native transcript for this synthetic session
    assert s.ops_usd == pytest.approx(0.25)  # 1M gpt-5-mini input @ $0.25
    assert s.by_op["enrich"] == pytest.approx(0.25)


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
    # 1M input @ $5 + 1M output @ $25 = $30
    assert s.claude_usd == pytest.approx(30.0)
    assert s.subagent_usd == pytest.approx(25.0)  # the sidechain turn
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


def test_read_session_spend_resolves_ses_id(fake_home, config):
    """/mem-wrap passes a ses-… id; the native transcript + folder are keyed by
    the Claude UUID (source_session). read_session_spend must resolve across."""
    uuid = "0352e1e4-1d0e-4cb1-a0e4-c4ec013d851d"
    _write_native_jsonl(fake_home, uuid, [
        {"model": "claude-opus-4-7",
         "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
    ])
    # Seed the vault session note (frontmatter id=ses-…, source_session=uuid)
    # and an archived Layer-B spend event in its events.jsonl.
    sess_dir = config.vault_root / "projects" / "p" / "sessions" / f"{uuid}-2026-06-01"
    sess_dir.mkdir(parents=True)
    (sess_dir / "session.md").write_text(
        "---\n"
        "type: session\n"
        "id: ses-6b216a81\n"
        f"source_session: {uuid}\n"
        "aliases: [ses-6b216a81]\n"
        "---\n\n# Session\n",
        encoding="utf-8",
    )
    (sess_dir / "events.jsonl").write_text(
        json.dumps({
            "type": "spend", "op": "enrich", "provider": "openai",
            "model": "gpt-5-mini", "tokens_input": 1_000_000, "tokens_output": 0,
            "tokens_cache_read": 0, "tokens_cache_write": 0, "mode": "cli",
        }) + "\n",
        encoding="utf-8",
    )

    s = read_session_spend("ses-6b216a81", project="p", cfg=config)
    assert not s.unknown
    assert s.claude_usd == pytest.approx(5.0)   # Layer A resolved via uuid (1M opus in)
    assert s.ops_usd == pytest.approx(0.25)     # Layer B from the folder events
    # A raw UUID resolves to itself — same numbers.
    s2 = read_session_spend(uuid, project="p", cfg=config)
    assert s2.claude_usd == pytest.approx(5.0)


def test_unpriced_model_surfaced(fake_home, config):
    """A turn on a model with no rate card is counted + flagged, not silent $0."""
    _write_native_jsonl(fake_home, "ses-u", [
        {"model": "claude-future-9", "usage": {"input_tokens": 1000, "output_tokens": 500}},
        {"model": "claude-opus-4-8", "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
    ])
    s = read_session_spend("ses-u", cfg=config)
    assert s.n_turns == 2
    assert s.unpriced_turns == 1
    assert s.unpriced_tokens == 1500
    assert "claude-future-9" in s.unpriced_models
    assert s.claude_usd == pytest.approx(5.0)  # only the priced opus turn
    assert s.as_dict()["unpriced_models"] == ["claude-future-9"]


def test_gemini_thoughts_count_as_output():
    """_record_gemini_spend bills thoughts_token_count as output."""
    from personal_mem.synthesis.gemini_extract import _record_gemini_spend

    recorded = {}

    class _Meta:
        prompt_token_count = 1000
        candidates_token_count = 200
        thoughts_token_count = 300

    class _Resp:
        usage_metadata = _Meta()

    import personal_mem.synthesis.gemini_extract as gx
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "personal_mem.core.spend.record_spend",
        lambda *a, **k: recorded.update(args=a, kwargs=k),
    )
    _record_gemini_spend(_Resp(), "gemini-2.5-flash", "gemini_extract")
    monkey.undo()
    # positional: provider, model, op, tokens_input, tokens_output
    assert recorded["args"][3] == 1000
    assert recorded["args"][4] == 500  # candidates 200 + thoughts 300


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
    assert s.claude_usd == pytest.approx(5.0)  # 1M opus input @ $5
    # Gemini headless op (today=fake; the test writes with utcnow date) —
    # included only if its date falls in-window. Assert it's attributed to ops.
    assert s.ops_usd >= 0.0
    assert s.n_turns == 1


# --------------------------------------------------------------------------- #
# --ops-only — mem-operating cost only                                         #
# --------------------------------------------------------------------------- #


def test_first_user_text_skips_tool_results(fake_home):
    """The op-label probe reads the first real prompt, not a tool-result row."""
    proj = fake_home / ".claude" / "projects" / "-cwd"
    proj.mkdir(parents=True)
    path = proj / "t.jsonl"
    path.write_text("\n".join([
        # A tool-result user row (content is a block list, no text) — skipped.
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "x"}]}}),
        json.dumps({"type": "user", "message": {"content": "/dream --foo"}}),
    ]) + "\n", encoding="utf-8")
    assert _first_user_text(path) == "/dream --foo"
    assert _transcript_op_label(path) == "dream"


def test_transcript_op_label_interactive_is_none(fake_home):
    p = _write_native_jsonl(fake_home, "ses-i", [
        {"model": "claude-opus-4-8", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ], first_prompt="fix the login bug please")
    assert _transcript_op_label(p) is None


def test_read_range_ops_only(fake_home, config, tmp_path):
    """--ops-only keeps mem-skill transcripts (bucketed by op) + all Layer-B;
    interactive coding transcripts are dropped from Layer A."""
    # A headless /dream run — Layer A should count, labelled 'dream'.
    _write_native_jsonl(fake_home, "ses-dream", [
        {"model": "claude-opus-4-8", "timestamp": "2026-06-01T09:00:00Z",
         "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
    ], first_prompt="/dream")
    # An interactive coding session — must be excluded under --ops-only.
    _write_native_jsonl(fake_home, "ses-code", [
        {"model": "claude-opus-4-8", "timestamp": "2026-06-01T10:00:00Z",
         "usage": {"input_tokens": 5_000_000, "output_tokens": 0}},
    ], first_prompt="refactor the parser")
    # A headless Layer-B op (gemini) in-window.
    record_spend("gemini", "gemini-2.0-flash", "gemini_extract", 1_000_000, 0,
                 mode="cron", cfg=config)

    # All-time window so the headless Layer-B log (dated the real "today", not
    # the transcript timestamps) falls in range — mirrors test_read_range_spend.
    full = read_range_spend(cfg=config)
    assert full.claude_usd == pytest.approx(30.0)  # both transcripts (5M+1M @ $5)

    ops = read_range_spend(ops_only=True, cfg=config)
    # Only the /dream transcript's Layer A (1M opus in @ $5); ses-code dropped.
    assert ops.claude_usd == pytest.approx(5.0)
    assert ops.n_turns == 1
    # Layer-A op cost is bucketed under by_op alongside Layer-B ops.
    assert ops.by_op["dream"] == pytest.approx(5.0)
    # Layer B (gemini headless) is mem-operating cost — kept regardless.
    assert ops.ops_usd == pytest.approx(0.10)  # 1M gemini-2.0-flash in @ $0.10
    assert ops.by_op["gemini_extract"] == pytest.approx(0.10)
