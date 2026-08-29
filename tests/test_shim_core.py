"""The Python half of the shims/shim-core cross-language seam (#194).

Two contracts live here, both cheap and both aimed at drift that would
otherwise be silent:

1. **Vocabulary parity.** ``shims/shim-core/canonical-events.json`` is the one
   shared fixture both suites pin against: this file checks it equals
   ``core.harness.CANONICAL_EVENTS`` (names, order) and that each event's
   phase token is exactly the argv the authored ``hooks/hooks.json`` commands
   pass to the handler. The package's node:test suite pins its TypeScript
   exports against the same fixture, so neither side can drift alone.

2. **The translator rule** (dec-5a076384): shims may adapt protocol — event
   synthesis, dedup, debounce, timeouts — but never carry vault semantics.
   Enforced as an enumerated deny-list grep over ``shims/``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SHIMS_DIR = REPO_ROOT / "shims"
FIXTURE = SHIMS_DIR / "shim-core" / "canonical-events.json"


def _fixture() -> dict[str, str]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestVocabularyParity:
    def test_fixture_events_equal_canonical_events(self):
        """Same names, same order — the TS types mirror this exact tuple."""
        from thinkweave.core.harness import CANONICAL_EVENTS

        assert list(_fixture()) == list(CANONICAL_EVENTS)

    def test_fixture_phases_are_the_authored_hook_argv(self):
        """Each phase token is what hooks/hooks.json actually passes.

        The launch command's last argv word is the handler phase
        (``… weave-hook-launch" session_start``). Pinning the fixture to the
        authored commands — not to a copy of the mapping — means a phase
        rename in hooks.json breaks this test, not the shims at runtime.
        """
        authored = json.loads(
            (REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )["hooks"]
        for event, phase in _fixture().items():
            commands = {
                h["command"].rsplit(" ", 1)[-1]
                for entry in authored[event]
                for h in entry["hooks"]
            }
            assert commands == {phase}, (
                f"{event}: fixture says {phase!r}, hooks.json says {commands}"
            )


# The translator-rule deny-list: identifiers that only appear in code doing
# vault work — MCP writer tool names, the CLI's write subcommand, index/DB
# access, ontology vocabulary, and the vault's own paths. Word-boundary
# matched so e.g. `ontology` can't hide inside an identifier.
#
# ponytail: this is a substring gate, not a semantic one. It catches the
# honest-mistake failure mode (someone imports vault logic into a shim
# because it was convenient) and anything named after what it does. It cannot
# catch vault semantics smuggled through an innocently-named subprocess or a
# re-exported helper — that remains the review rule the issue states, with
# this test as its floor.
_DENY = (
    "weave_create",
    "weave_extract",
    "weave_update",
    "weave_link",
    "weave_unlink",
    "weave add",
    "sqlite",
    "ontology",
    "frontmatter",
    "proposed_concepts",
    ".weave/",
    "vault/",
)
_DENY_RE = re.compile(
    "|".join(rf"(?<![A-Za-z0-9_]){re.escape(t)}" for t in _DENY), re.IGNORECASE
)

# Source and config only; generated/vendored trees are not ours to police.
_SCANNED_SUFFIXES = {".ts", ".js", ".mjs", ".cjs", ".json", ".md"}
_SKIP_DIRS = {"node_modules", "dist"}


class TestTranslatorRule:
    def test_shims_carry_no_vault_semantics(self):
        scanned = 0
        for path in sorted(SHIMS_DIR.rglob("*")):
            if not path.is_file() or path.suffix not in _SCANNED_SUFFIXES:
                continue
            if _SKIP_DIRS & set(path.relative_to(SHIMS_DIR).parts):
                continue
            if path.name == "package-lock.json":  # registry URLs, not our code
                continue
            scanned += 1
            hit = _DENY_RE.search(path.read_text(encoding="utf-8"))
            assert hit is None, (
                f"{path.relative_to(REPO_ROOT)} contains vault-semantic "
                f"token {hit.group(0)!r} — shims translate protocol only "
                "(dec-5a076384)"
            )
        assert scanned > 0, "shims/ scan found no source files — wrong path?"
