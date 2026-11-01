"""Hook behaviour as knobs (dec-4f158b58 layer 1, dec-6d5f1913) and the
serving-chain instrumentation (#353): ``[hooks]`` budgets and cap bounded by
the profile literals, the Stop nudge seam (#345), the effective values in
the round manifest, ``bytes``/``cap`` on the serving rows, the PreCompact
``compact`` row, the rail-disabled early exit, and the mid-sentence
``/wrap`` Skill row (round 2, found-at-wrap item 11)."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from tests.tasks.conftest import run_hook
from thinkweave.core import manifest
from thinkweave.core.buffer import buffer_path
from thinkweave.core.config import Config, Hooks, _apply_knobs, _check_rail_keys
from thinkweave.core.harness import CANONICAL_EVENTS, lookup
from thinkweave.core.task_contract import Round
from thinkweave.operations import tasks
from thinkweave.surfaces.hooks import handler

SESSION = "22222222-3333-4444-8555-666677778888"


def _rows(cfg: Config) -> list[dict]:
    path = buffer_path(cfg.weave_dir, SESSION)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def _log(cfg: Config) -> str:
    path = cfg.weave_dir / "hooks.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _prompt(monkeypatch, cwd: Path, text: str, *, harness: str = "") -> dict:
    payload = {"session_id": SESSION, "cwd": str(cwd), "hook_event_name": "UserPromptSubmit",
               "prompt": text}
    reply = run_hook(monkeypatch, "user_prompt_submit", payload, harness=harness)
    monkeypatch.setattr("sys.stdout", sys.__stdout__)
    return reply


# --------------------------------------------------------------------------- #
# [hooks] knobs
# --------------------------------------------------------------------------- #


class TestHooksSection:
    def test_unset_is_the_profile_literal(self):
        eff = Config().hooks.effective({"Stop": 30_000}, 10_000)
        assert (eff.budget_ms, eff.cap_chars, eff.nudge, eff.refused) == (
            {"Stop": 30_000}, 10_000, False, ()
        )

    def test_a_knob_lowers_and_never_raises(self):
        hooks = Hooks(budget_ms={"UserPromptSubmit": 50, "Stop": 40_000},
                      additional_context_cap_chars=20_000)
        eff = hooks.effective({"UserPromptSubmit": 30_000, "Stop": 30_000}, 10_000)
        assert eff.budget_ms == {"UserPromptSubmit": 50, "Stop": 30_000}
        assert eff.cap_chars == 10_000
        assert len(eff.refused) == 2
        assert "Stop = 40000" in eff.refused[0]

    def test_a_knob_without_a_literal_stands_alone(self):
        eff = Hooks(budget_ms={"PostToolUse": 900}).effective({}, None)
        assert eff.budget_ms == {"PostToolUse": 900}

    def test_the_toml_shape_reads(self):
        data = tomllib.loads(
            "[hooks]\nadditional_context_cap_chars = 4000\n"
            "[hooks.budget_ms]\nUserPromptSubmit = 50\nPreCompact = 1000\n"
            "[hooks.stop]\nunverified_claim_nudge = true\n"
        )
        cfg = Config()
        _check_rail_keys(data, Path("rail.toml"))
        _apply_knobs(cfg, data)
        assert cfg.hooks.budget_ms == {"UserPromptSubmit": 50, "PreCompact": 1000}
        assert cfg.hooks.additional_context_cap_chars == 4000
        assert cfg.hooks.stop.unverified_claim_nudge is True

    def test_an_unknown_event_is_refused_by_name(self):
        data = tomllib.loads("[hooks.budget_ms]\nUserPrompt = 50\n")
        with pytest.raises(ValueError, match="unknown event 'UserPrompt'"):
            _apply_knobs(Config(), data, Path("config.toml"))
        with pytest.raises(ValueError, match="hooks.budget_ms.UserPrompt"):
            _check_rail_keys(data, Path("rail.toml"))

    def test_the_inventory_renders_every_hooks_key(self):
        from thinkweave.surfaces.cli.policy import render_toml

        text = render_toml()
        for line in ("# [hooks]", "# [hooks.budget_ms]", "# [hooks.stop]"):
            assert line in text
        assert "# additional_context_cap_chars = <unset>" in text
        assert "# unverified_claim_nudge = false" in text
        for event in CANONICAL_EVENTS:
            assert f"# {event} = <unset>" in text


# --------------------------------------------------------------------------- #
# Budgets in the handler, and the prompt-time rows
# --------------------------------------------------------------------------- #


class TestBudget:
    def test_a_lowered_budget_skips_the_prompt_time_arm_with_a_row(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        """Demo criterion 2: UserPromptSubmit = 50 leaves no time for the
        similarity arm; the miss row says ``skipped: "budget"``."""
        cfg.hooks.budget_ms = {"UserPromptSubmit": 50}
        _prompt(monkeypatch, tmp_path, "how does the drift detector rank pairs")
        (row,) = [r for r in _rows(cfg) if r.get("type") == "prompt_time_miss"]
        assert row["skipped"] == "budget"
        assert row["bytes"] == 0
        assert row["cap"] == cfg.retrieval_prompt_time.max_injected_chars_per_turn

    def test_a_budget_above_the_literal_is_refused_once(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        cfg.hooks.budget_ms = {"UserPromptSubmit": 40_000}
        monkeypatch.setattr(
            "thinkweave.operations.prompt_time_retrieval.build_enrichment",
            lambda *a, **kw: (None, [], False),
        )
        _prompt(monkeypatch, tmp_path, "a prompt long enough to enrich")
        refusals = [ln for ln in _log(cfg).splitlines() if "above the harness literal" in ln]
        assert len(refusals) == 1
        assert "UserPromptSubmit = 40000" in refusals[0]
        assert "30000" in refusals[0]

    def test_a_served_block_row_carries_bytes_and_cap(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        block = "## Related\n- (`n-aaaa1111`) a note\n"
        monkeypatch.setattr(
            "thinkweave.operations.prompt_time_retrieval.build_enrichment",
            lambda *a, **kw: (block, ["n-aaaa1111"], False),
        )
        reply = _prompt(monkeypatch, tmp_path, "a prompt long enough to enrich")
        assert reply["hookSpecificOutput"]["additionalContext"] == block
        (row,) = [r for r in _rows(cfg) if r.get("type") == "retrieval"]
        assert row["bytes"] == len(block)
        assert row["cap"] == cfg.retrieval_prompt_time.max_injected_chars_per_turn


class TestStartupRow:
    def _start(self, monkeypatch, tmp_path: Path, payload: str) -> dict:
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context", lambda *a, **kw: payload
        )
        return run_hook(monkeypatch, "session_start", {
            "session_id": SESSION, "cwd": str(tmp_path), "source": "startup",
            "hook_event_name": "SessionStart",
        })

    def test_the_startup_row_carries_bytes_written_and_the_cap(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        reply = self._start(monkeypatch, tmp_path, "## Recent Decisions\n- (`dec-aaaa1111`) one\n")
        served = reply["hookSpecificOutput"]["additionalContext"]
        (row,) = [r for r in _rows(cfg) if r.get("type") == "startup"]
        assert row["bytes"] == len(served)
        assert row["cap"] > 0

    def test_a_lowered_cap_shrinks_the_budget_in_the_row(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        from thinkweave.retrieval.context import serving_plan

        cfg.hooks.additional_context_cap_chars = 3_000
        self._start(monkeypatch, tmp_path, "## Recent Decisions\n- (`dec-aaaa1111`) one\n")
        (row,) = [r for r in _rows(cfg) if r.get("type") == "startup"]
        assert row["cap"] == serving_plan(cfg).startup_budget_chars(3_000)

    def test_an_empty_payload_writes_zero_bytes(self, cfg: Config, monkeypatch, tmp_path: Path):
        self._start(monkeypatch, tmp_path, "")
        (row,) = [r for r in _rows(cfg) if r.get("type") == "startup"]
        assert row["bytes"] == 0


# --------------------------------------------------------------------------- #
# The manifest's effective values
# --------------------------------------------------------------------------- #


class TestManifestEffective:
    def test_effective_sits_beside_the_literals(self, tmp_path: Path):
        cfg = Config(vault_root=tmp_path / "vault")
        cfg.hooks.budget_ms = {"UserPromptSubmit": 50, "Stop": 40_000}
        found = manifest.collect(cfg, "claude-code")
        assert found["literals"]["hook_budget_ms"]["UserPromptSubmit"] == 30_000
        eff = found["effective"]
        assert eff["hook_budget_ms"]["UserPromptSubmit"] == 50
        assert eff["hook_budget_ms"]["Stop"] == 30_000
        assert eff["additional_context_cap_chars"] == 10_000
        assert eff["nudge"] is False
        assert Round.from_dict({"manifest": found}, grain="work").manifest == found

    def test_each_harness_names_its_own_budgets(self, tmp_path: Path):
        """Demo criterion 5: Pi's 2500 ms, not Claude Code's 30000."""
        cfg = Config(vault_root=tmp_path / "vault")
        cfg.hooks.stop.unverified_claim_nudge = True
        pi = manifest.collect(cfg, "pi")["effective"]
        assert pi["hook_budget_ms"] == lookup("pi").hook_budget_ms
        assert "additional_context_cap_chars" not in pi
        assert pi["nudge"] is True


# --------------------------------------------------------------------------- #
# The Stop nudge (#345)
# --------------------------------------------------------------------------- #


def _transcript(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "transcript.jsonl"
    rows = [
        {"type": "user", "message": {"content": "do it"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


class TestStopNudge:
    def _turn(self, cfg: Config, monkeypatch, tmp_path: Path, *, test_ran: bool = False):
        _prompt(monkeypatch, tmp_path, "fix the parser")
        run_hook(monkeypatch, "post_tool_use", {
            "session_id": SESSION, "cwd": str(tmp_path), "tool_name": "Edit",
            "tool_input": {"file_path": "src/parser.py", "old_string": "a", "new_string": "b"},
        })
        if test_ran:
            run_hook(monkeypatch, "post_tool_use", {
                "session_id": SESSION, "cwd": str(tmp_path), "tool_name": "Bash",
                "tool_input": {"command": "uv run pytest -q tests/test_parser.py"},
                "tool_response": {"stdout": "3 passed in 0.1s"},
            })

    def _stop(self, monkeypatch, tmp_path: Path, text: str, **extra) -> dict:
        return run_hook(monkeypatch, "stop", {
            "session_id": SESSION, "cwd": str(tmp_path), "hook_event_name": "Stop",
            "transcript_path": str(_transcript(tmp_path, text)), **extra,
        })

    def test_a_claim_with_no_test_run_blocks_once(self, cfg: Config, monkeypatch, tmp_path: Path):
        cfg.hooks.stop.unverified_claim_nudge = True
        self._turn(cfg, monkeypatch, tmp_path)
        reply = self._stop(monkeypatch, tmp_path, "Fixed the parser. All tests pass.")
        assert reply["decision"] == "block"
        assert "all tests pass" in reply["reason"]
        (row,) = [r for r in _rows(cfg) if r.get("type") == "nudge"]
        assert row["kind"] == "unverified_claim"
        assert row["phrase"] == "all tests pass"
        assert row["ts"]

    def test_the_loop_guard_lets_the_next_stop_pass(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        cfg.hooks.stop.unverified_claim_nudge = True
        self._turn(cfg, monkeypatch, tmp_path)
        self._stop(monkeypatch, tmp_path, "All tests pass.")
        reply = self._stop(monkeypatch, tmp_path, "All tests pass.", stop_hook_active=True)
        assert "decision" not in reply
        assert len([r for r in _rows(cfg) if r.get("type") == "nudge"]) <= 1

    def test_knob_off_reads_nothing_and_writes_no_row(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        self._turn(cfg, monkeypatch, tmp_path)
        monkeypatch.setattr(
            handler, "_last_assistant_text",
            lambda path: pytest.fail("the transcript was read with the knob off"),
        )
        reply = self._stop(monkeypatch, tmp_path, "All tests pass.")
        assert "decision" not in reply
        assert not any(r.get("type") == "nudge" for r in _rows(cfg))

    def test_a_test_run_this_turn_passes(self, cfg: Config, monkeypatch, tmp_path: Path):
        cfg.hooks.stop.unverified_claim_nudge = True
        self._turn(cfg, monkeypatch, tmp_path, test_ran=True)
        assert "decision" not in self._stop(monkeypatch, tmp_path, "Tests pass.")

    def test_unverified_is_not_verified(self, cfg: Config, monkeypatch, tmp_path: Path):
        cfg.hooks.stop.unverified_claim_nudge = True
        self._turn(cfg, monkeypatch, tmp_path)
        assert "decision" not in self._stop(monkeypatch, tmp_path, "The fix is unverified.")

    def test_no_transcript_passes_and_says_so(self, cfg: Config, monkeypatch, tmp_path: Path):
        cfg.hooks.stop.unverified_claim_nudge = True
        self._turn(cfg, monkeypatch, tmp_path)
        reply = run_hook(monkeypatch, "stop", {
            "session_id": SESSION, "cwd": str(tmp_path), "hook_event_name": "Stop",
        }, harness="pi")
        assert "decision" not in reply
        assert "sends no transcript_path" in _log(cfg)


# --------------------------------------------------------------------------- #
# PreCompact and the rail-disabled exit
# --------------------------------------------------------------------------- #


class TestPreCompact:
    def test_claude_code_writes_one_compact_row(self, cfg: Config, monkeypatch, tmp_path: Path):
        reply = run_hook(monkeypatch, "pre_compact", {
            "session_id": SESSION, "cwd": str(tmp_path), "hook_event_name": "PreCompact",
            "trigger": "auto",
        })
        assert reply == {}
        (row,) = [r for r in _rows(cfg) if r.get("type") == "compact"]
        assert row["trigger"] == "auto"

    @pytest.mark.parametrize("harness", ["codex", "pi", "opencode"])
    def test_an_unmapped_harness_reports_and_writes_nothing(
        self, cfg: Config, monkeypatch, tmp_path: Path, harness: str
    ):
        reply = run_hook(monkeypatch, "pre_compact", {
            "session_id": SESSION, "cwd": str(tmp_path), "hook_event_name": "PreCompact",
        }, harness=harness)
        assert reply == {}
        assert not _rows(cfg)
        assert f"PreCompact is unmapped on {harness}" in _log(cfg)


class TestRailDisabledEvent:
    def test_a_disabled_event_does_no_work(self, cfg: Config, monkeypatch, tmp_path: Path):
        """R2-D's plugin-route follow-up: the plugin registers every event,
        so the handler drops the one hooks.toml disables."""
        rail = tmp_path / "rail"
        (rail / "hooks").mkdir(parents=True)
        (rail / "hooks" / "hooks.toml").write_text(
            "[UserPromptSubmit]\nenabled = false\n", encoding="utf-8"
        )
        cfg.rsi_rail = rail
        reply = _prompt(monkeypatch, tmp_path, "a prompt long enough to enrich")
        assert reply == {}
        assert not _rows(cfg)

    def test_a_rail_file_that_does_not_read_is_reported_and_the_hook_runs(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        rail = tmp_path / "rail"
        (rail / "hooks").mkdir(parents=True)
        (rail / "hooks" / "hooks.toml").write_text("[Nope]\nenabled = false\n", encoding="utf-8")
        cfg.rsi_rail = rail
        reply = run_hook(monkeypatch, "pre_compact", {
            "session_id": SESSION, "cwd": str(tmp_path), "hook_event_name": "PreCompact",
        })
        assert "unknown event 'Nope'" in reply["systemMessage"]
        assert [r["type"] for r in _rows(cfg)] == ["compact"]


# --------------------------------------------------------------------------- #
# Item 11: /wrap typed mid-sentence
# --------------------------------------------------------------------------- #


class TestMidSentenceSkill:
    def test_the_tool_row_is_the_witness_and_the_round_counts_one(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        """Claude Code does not expand a slash command typed mid-sentence: it
        attaches a ``skill_mention`` and the model calls the Skill tool, so
        the prompt path writes no Skill row and the PostToolUse ``Skill``
        row (``declared: tool``) is the one witness. Payloads as Claude Code
        sends them; the round counts ``Skill`` once."""
        monkeypatch.setattr(
            "thinkweave.operations.prompt_time_retrieval.build_enrichment",
            lambda *a, **kw: (None, [], False),
        )
        _prompt(monkeypatch, tmp_path,
                "this sounds good, /wrap and we'll pick it up next session")
        assert not [r for r in _rows(cfg) if r.get("tool") == "Skill"]
        run_hook(monkeypatch, "post_tool_use", {
            "session_id": SESSION, "cwd": str(tmp_path), "hook_event_name": "PostToolUse",
            "tool_name": "Skill", "tool_input": {"skill": "wrap", "args": ""},
            "tool_response": {"success": True, "commandName": "wrap"},
            "tool_use_id": "toolu_01SxoM2obsw7Djnfu3cyWGHv",
        })
        (row,) = [r for r in _rows(cfg) if r.get("tool") == "Skill"]
        assert (row["skill"], row["declared"]) == ("wrap", "tool")

        result = tasks.apply_declaration(
            cfg, {"declared": [{"title": "mid-sentence wrap", "round": {}}]},
            session_key=SESSION, project="t",
            streams=[buffer_path(cfg.weave_dir, SESSION)], folders=[],
        )
        assert result.errors == []
        (note,) = [p for p in cfg.vault_root.rglob("tsk-*.md")]
        from thinkweave.core.vault import parse_frontmatter

        (entry,) = parse_frontmatter(note.read_text(encoding="utf-8"))[0]["rounds"]
        assert entry["tools"] == {"Skill": 1}

    def test_the_hooks_json_matcher_reaches_the_skill_tool(self):
        import re

        doc = json.loads(
            (Path(__file__).resolve().parents[2] / "hooks" / "hooks.json").read_text("utf-8")
        )
        matchers = [e["matcher"] for e in doc["hooks"]["PostToolUse"]]
        assert any(re.fullmatch(m, "Skill") for m in matchers)
