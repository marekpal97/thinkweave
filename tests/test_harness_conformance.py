"""Conformance suite over every registered ``HarnessProfile`` (issue #191).

The dispatch-contract re-scope (dec-5a076384, amended by dec-2fa074a0): a
profile is pure data, interpreted by one installer + one envelope normaliser +
one write-safety module, and every profile — including a tier-0-only row with
no hooks and no transcript importer — must pass the same parametrised checks.
Expected values for the Pi and OpenCode rows are written out by hand from the
evidence blueprints (n-a1d3beba, n-767d66b4), never recomputed from the
factories under test.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from thinkweave.core import harness, harness_docs, mcp_config
from thinkweave.operations import hook_events

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "harness_transcripts"

ALL_IDS = ("claude-code", "codex", "pi", "opencode")

#: The canonical lifecycle vocabulary is authored once, in ``hooks/hooks.json``.
CANONICAL_HOOKS = json.loads(
    (REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
)["hooks"]


def _build(profile_id: str, home: Path) -> harness.HarnessProfile:
    # Every factory is dual-use: no-arg derives the real home, an explicit
    # ``home`` sandboxes it — which is what makes the rows conformance-testable.
    return harness.PROFILES[profile_id](home)


@pytest.fixture(params=ALL_IDS)
def profile(request, tmp_path: Path) -> harness.HarnessProfile:
    return _build(request.param, tmp_path / "home")


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_all_four_profiles_registered(self):
        assert set(harness.PROFILES) == set(ALL_IDS)

    def test_canonical_vocabulary_matches_the_authored_hooks_file(self):
        # ``hooks/hooks.json`` is where the lifecycle vocabulary is authored;
        # the normaliser's constant must never lag it.
        assert set(CANONICAL_HOOKS) == set(hook_events.CANONICAL_EVENTS)


# --------------------------------------------------------------------------- #
# schema invariants — every profile, one parametrisation
# --------------------------------------------------------------------------- #


class TestSchemaInvariants:
    def test_eligibility_is_a_ladder_tier(self, profile):
        assert profile.eligibility in ("E0", "E1", "E2", "E3")

    def test_hook_mechanism_enum_and_hooks_flag_agree(self, profile):
        assert profile.hook_mechanism in ("file", "plugin", "none")
        assert profile.hooks == (profile.hook_mechanism != "none")

    def test_native_memory_flag_and_artifact_agree(self, profile):
        # dec-5a076384: the seam gates on a *detected artifact*, so a profile
        # claiming native memory must say where that artifact lives.
        assert profile.native_memory == (
            profile.native_memory_artifact is not None
        )

    def test_hook_event_keys_are_canonical(self, profile):
        assert set(profile.hook_events) <= set(hook_events.CANONICAL_EVENTS)

    def test_identity_fields_are_filled(self, profile):
        assert profile.display_name
        assert profile.detect_dir is not None
        assert profile.transcript_format
        assert profile.session_id_scheme
        assert profile.context_channel

    def test_harness_flag_is_empty_or_names_this_harness(self, profile):
        # Claude Code is the authored canonical shape and stays unstamped;
        # every other harness's flag must name itself, never another harness.
        assert profile.harness_flag in ("", f"--harness {profile.id}")
        if profile.id != "claude-code":
            assert profile.harness_flag

    def test_all_home_scoped_paths_stay_under_the_home(
        self, profile, tmp_path: Path
    ):
        home = tmp_path / "home"
        for f in dataclasses.fields(profile):
            value = getattr(profile, f.name)
            if isinstance(value, Path) and value.is_absolute():
                assert value.is_relative_to(home), (
                    f"{profile.id}.{f.name} = {value} escapes the home "
                    f"{home} — a sandboxed caller would write into the real one"
                )
        assert profile.transcript_glob.startswith(str(home))

    def test_mcp_servers_key_declared_and_format_consistent(self, profile):
        assert profile.mcp_servers_key
        # For the harnesses whose key follows the format convention, the
        # declared key and the writer's suffix-derived default must agree —
        # OpenCode is the deliberate exception (JSON file, ``mcp`` key).
        if profile.id in ("claude-code", "codex", "pi"):
            assert profile.mcp_servers_key == mcp_config._servers_key(
                profile.mcp_config
            )


# --------------------------------------------------------------------------- #
# hand-written expected rows for the two blueprint harnesses
# --------------------------------------------------------------------------- #


class TestPiRow:
    """Facts from blueprint n-a1d3beba, declared-not-verified where marked."""

    def test_install_topology(self, tmp_path: Path):
        p = _build("pi", tmp_path)
        agent = tmp_path / ".pi" / "agent"
        assert p.skills_dir == agent / "skills"
        assert p.mcp_config == agent / "settings.json"
        assert p.instructions_file == agent / "AGENTS.md"
        assert p.project_mcp_config_relpath == Path(".pi") / "settings.json"

    def test_tier_zero_row_is_accepted_without_hooks_or_importer(
        self, tmp_path: Path
    ):
        # dec-2fa074a0: E0 is an OFFICIAL tier — skills dir + instructions
        # file + rendered degradations, no hooks, no transcript importer.
        p = _build("pi", tmp_path)
        assert p.eligibility == "E0"
        assert p.hook_mechanism == "none" and not p.hooks
        assert p.fires_verified == {}
        assert not p.subagents and not p.headless_slash

    def test_dispatch_shape(self, tmp_path: Path):
        p = _build("pi", tmp_path)
        assert p.headless_argv("hello") == ["pi", "-p", "hello"]

    def test_blueprint_event_map_is_declared(self, tmp_path: Path):
        p = _build("pi", tmp_path)
        assert p.hook_events == {
            "SessionStart": "session_start",
            "UserPromptSubmit": "before_agent_start",
            "PostToolUse": "tool_result",
            "Stop": "agent_end",
        }


class TestOpenCodeRow:
    """Facts from blueprint n-767d66b4, declared-not-verified where marked."""

    def test_install_topology(self, tmp_path: Path):
        p = _build("opencode", tmp_path)
        cfg = tmp_path / ".config" / "opencode"
        assert p.mcp_config == cfg / "opencode.json"
        assert p.mcp_servers_key == "mcp"
        assert p.plugins_root == cfg / "plugins"
        assert p.instructions_file == cfg / "AGENTS.md"

    def test_state_facts(self, tmp_path: Path):
        p = _build("opencode", tmp_path)
        data = tmp_path / ".local" / "share" / "opencode"
        assert p.transcript_glob == str(data / "storage" / "session" / "*" / "*.json")
        assert p.transcript_format == "json-records"
        assert p.eligibility == "E0"

    def test_dispatch_shape(self, tmp_path: Path):
        p = _build("opencode", tmp_path)
        assert p.headless_argv("hello", bypass=True) == [
            "opencode",
            "run",
            "hello",
            "--auto",
        ]

    def test_stop_has_no_verified_native_event(self, tmp_path: Path):
        # claude-mem #2462: their plugin subscribed to bus events that never
        # fire and capture was silently dead. The profile therefore refuses to
        # name a Stop mapping until one is proven, and carries the honesty as
        # a degradation instead.
        p = _build("opencode", tmp_path)
        assert p.hook_events["Stop"] is None


# --------------------------------------------------------------------------- #
# events-actually-fire probe (in-repo consistency)
# --------------------------------------------------------------------------- #


class TestEventsFireProbe:
    # ponytail: this probe is an in-repo consistency check — a profile cannot
    # claim more than its own hook wiring supports. The ceiling is that no
    # live harness is launched; the upgrade path is a real fires-probe run
    # per harness recorded into fires_verified by hand, dated.

    def test_fires_verified_only_claims_mapped_events(self, profile):
        mapped = {e for e, native in profile.hook_events.items() if native}
        assert set(profile.fires_verified) <= mapped

    def test_fires_verified_requires_a_hook_mechanism(self, profile):
        if profile.fires_verified:
            assert profile.hook_mechanism != "none"

    def test_hooked_harnesses_map_every_installed_event(self, profile):
        # `weave hooks install` writes every canonical event's entry; a
        # harness that cannot fire one of them must not get that config.
        if profile.hook_mechanism != "none":
            for event in CANONICAL_HOOKS:
                assert profile.hook_events.get(event), (
                    f"{profile.id} installs a {event} hook but declares no "
                    "native event for it — config that parses and never fires"
                )

    def test_shipped_harnesses_carry_dated_verification(self, tmp_path: Path):
        cc = _build("claude-code", tmp_path)
        assert set(cc.fires_verified) == set(hook_events.CANONICAL_EVENTS)
        codex = _build("codex", tmp_path)
        # The 2026-08-02 spike observed SessionStart and UserPromptSubmit
        # pre-auth; Stop and PostToolUse remain unobserved on a live Codex
        # (docs/HARNESSES.md §Spike answers) and must not be claimed.
        assert set(codex.fires_verified) == {"SessionStart", "UserPromptSubmit"}
        for date in {**cc.fires_verified, **codex.fires_verified}.values():
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)


# --------------------------------------------------------------------------- #
# degradations — no capability silently faked
# --------------------------------------------------------------------------- #


class TestDegradations:
    def test_rows_are_well_formed(self, profile):
        for d in profile.degradations:
            assert d.capability and d.note
            assert d.mode in ("documented", "refuse")

    def test_hookless_harness_documents_the_capture_degradation(self, profile):
        if profile.hook_mechanism == "none":
            assert any(
                "hook" in d.capability.lower() for d in profile.degradations
            ), f"{profile.id} has no hooks and no documented degradation for it"

    def test_unmapped_canonical_events_are_documented(self, profile):
        for event, native in profile.hook_events.items():
            if native is None:
                assert any(
                    event.lower() in d.capability.lower()
                    for d in profile.degradations
                ), f"{profile.id} maps {event} to nothing without documenting it"

    def test_rendering_carries_every_note(self, profile):
        rendered = harness_docs.render_degradations(profile)
        if not profile.degradations:
            assert rendered == ""
        for d in profile.degradations:
            assert d.note in rendered
            if d.upstream_ref:
                assert d.upstream_ref in rendered


# --------------------------------------------------------------------------- #
# MCP config round-trip, in every profile's declared format + key
# --------------------------------------------------------------------------- #


class TestMcpConfigRoundTrip:
    ENTRY = {"command": "uv", "args": ["run", "weave-mcp"], "env": {}}

    def test_write_then_read_round_trips_under_the_declared_key(
        self, profile, tmp_path: Path
    ):
        target = tmp_path / ("cfg" + profile.mcp_config.suffix)
        entry = mcp_config.canonical(target, self.ENTRY)
        mcp_config.write_entry(
            target, "thinkweave", entry, servers_key=profile.mcp_servers_key
        )
        assert (
            mcp_config.read_entry(
                target, "thinkweave", servers_key=profile.mcp_servers_key
            )
            == entry
        )

    def test_json_formats_nest_under_the_declared_key(
        self, profile, tmp_path: Path
    ):
        if profile.mcp_config.suffix != ".json":
            pytest.skip("TOML nesting is pinned byte-level in test_codex_install")
        target = tmp_path / "cfg.json"
        # A foreign top-level key must survive the splice untouched.
        target.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        mcp_config.write_entry(
            target, "thinkweave", self.ENTRY, servers_key=profile.mcp_servers_key
        )
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["theme"] == "dark"
        assert doc[profile.mcp_servers_key]["thinkweave"] == self.ENTRY


# --------------------------------------------------------------------------- #
# zero per-harness forks outside the interpreter (grep-enforced, AC3)
# --------------------------------------------------------------------------- #


class TestNoHarnessForks:
    def test_no_id_comparisons_against_harness_literals(self):
        """Consumers branch on capability *data*, never on which harness it
        is — that is the whole contract ("new harness = one profile row").
        The one sanctioned home for id-keyed knowledge is ``core/harness.py``
        itself, where the rows are authored."""
        fork = re.compile(r"\.id\s*[!=]=\s*[\"']")
        offenders = []
        for py in (REPO_ROOT / "src" / "thinkweave").rglob("*.py"):
            if py.name == "harness.py" and py.parent.name == "core":
                continue
            for lineno, line in enumerate(
                py.read_text(encoding="utf-8").splitlines(), 1
            ):
                if fork.search(line):
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        assert not offenders, (
            "per-harness fork(s) outside the profile interpreter — express "
            "the fact as profile data instead:\n" + "\n".join(offenders)
        )


# --------------------------------------------------------------------------- #
# hook-envelope round-trip per (harness × event)
# --------------------------------------------------------------------------- #


class TestEnvelopeRoundTrip:
    def test_canonical_envelope_survives_the_native_round_trip(self, profile):
        for event, native in profile.hook_events.items():
            if native is None:
                continue
            envelope = {
                "hook_event_name": event,
                "session_id": "s-1",
                "cwd": "/x",
            }
            wire = hook_events.to_native(profile, envelope)
            assert wire["hook_event_name"] == native
            back = hook_events.to_canonical(profile, wire)
            assert back == envelope

    def test_unknown_native_event_is_refused_not_guessed(self, tmp_path: Path):
        p = _build("pi", tmp_path)
        with pytest.raises(hook_events.UnknownHookEvent):
            hook_events.to_canonical(
                p, {"hook_event_name": "before_provider_headers"}
            )


# --------------------------------------------------------------------------- #
# docs/HARNESSES.md carries the GENERATED capability matrix (AC4)
# --------------------------------------------------------------------------- #


class TestGeneratedHarnessesDoc:
    def test_committed_matrix_matches_the_profiles(self):
        """The capability matrix people read is rendered from the same rows
        the installer runs on — a hand-maintained table is the rot pattern
        the issue names (vercel/memorix/hol-guard)."""
        doc = (REPO_ROOT / "docs" / "HARNESSES.md").read_text(encoding="utf-8")
        start = doc.find(harness_docs.MATRIX_START)
        end = doc.find(harness_docs.MATRIX_END)
        assert start != -1 and end != -1, "generated-matrix sentinels missing"
        committed = doc[start : end + len(harness_docs.MATRIX_END)]
        assert committed == harness_docs.generated_block(), (
            "docs/HARNESSES.md is stale — regenerate with "
            "`uv run python -m thinkweave.core.harness_docs --write`"
        )

    def test_matrix_names_every_profile_and_degradation(self):
        block = harness_docs.generated_block()
        for pid in ALL_IDS:
            p = _build(pid, Path("/h"))
            assert (p.display_name or pid) in block
            for d in p.degradations:
                assert d.note in block

    def test_matrix_carries_no_machine_paths(self):
        assert str(Path.home()) not in harness_docs.generated_block()


# --------------------------------------------------------------------------- #
# transcript-parse fixture, per declared format
# --------------------------------------------------------------------------- #


class TestTranscriptFormats:
    def test_format_has_a_parser_or_a_documented_degradation(self, profile):
        parser = profile.load_transcript_parser()
        if parser is None:
            assert any(
                "import" in d.capability.lower()
                or "transcript" in d.capability.lower()
                for d in profile.degradations
            ), f"{profile.id}: no parser for {profile.transcript_format!r} and no degradation"

    def test_parser_reads_the_fixture(self, profile, tmp_path: Path):
        parser = profile.load_transcript_parser()
        if parser is None:
            pytest.skip("format has no parser yet (documented degradation)")
        fixtures = sorted(FIXTURES.glob(f"{profile.id}/*"))
        assert fixtures, f"no transcript fixture for {profile.id}"
        session = parser(fixtures[0])
        assert session is not None and session.turn_count >= 2
