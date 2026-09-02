"""The Python half of the shims/shim-core cross-language seam (#194).

Vocabulary parity: ``shims/shim-core/canonical-events.json`` is the shared
fixture both suites pin. Translator rule (dec-5a076384, spike on #194):
deny-list grep over ``shims/``.
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
        """Same names, same order — the TS types mirror this exact tuple.

        Deliberately order-sensitive over JSON object keys (both json and JS
        preserve insertion order): a formatter that sorts keys would red this
        with no semantic drift behind it — reorder the fixture back, don't
        weaken the assert.
        """
        from thinkweave.core.harness import CANONICAL_EVENTS

        assert list(_fixture()) == list(CANONICAL_EVENTS)

    def test_ts_source_pins_the_fixture(self):
        """The TS half of the parity gate, enforced from pytest (which is
        what CI runs — there is no node job). Extracts the CANONICAL_EVENTS
        and EVENT_PHASES literals statically from src/index.ts and asserts
        them against the shared fixture, so a fixture+Python edit with a
        stale TS side fails here rather than drifting silently.

        ponytail: static regex extraction, not compilation — it pins the
        literals, not what tsc would emit. The package's own node:test suite
        pins the compiled exports; a node job in CI is the upgrade path
        (human-owned, .github/workflows is off-limits to the loop).
        """
        src = (SHIMS_DIR / "shim-core" / "src" / "index.ts").read_text(
            encoding="utf-8"
        )
        events_lit = re.search(
            r"export const CANONICAL_EVENTS = \[(.*?)\] as const", src, re.DOTALL
        )
        assert events_lit, "CANONICAL_EVENTS literal not found in src/index.ts"
        assert re.findall(r'"([A-Za-z]+)"', events_lit.group(1)) == list(_fixture())

        phases_lit = re.search(
            r"EVENT_PHASES: Record<CanonicalEvent, string> = \{(.*?)\};",
            src,
            re.DOTALL,
        )
        assert phases_lit, "EVENT_PHASES literal not found in src/index.ts"
        assert dict(
            re.findall(r'(\w+): "([a-z_]+)"', phases_lit.group(1))
        ) == _fixture()

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
# this test as its floor. Two deliberate reach choices: comments inside code
# files ARE matched (naming a writer tool in a comment usually accompanies
# calling it), while .md prose is exempt (documentation of the rule
# legitimately names the denied vocabulary).
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

# Inverted filter: scan EVERYTHING except an explicit skip list, so a future
# .py/.sh/.toml helper under shims/ is in reach without anyone remembering to
# extend a suffix set. Generated/vendored trees are not ours to police; .md
# is exempt per the ponytail above.
_SKIP_DIRS = {"node_modules", "dist"}
_SKIP_FILES = {"package-lock.json"}  # registry URLs, not our code


class TestTranslatorRule:
    def test_shims_carry_no_vault_semantics(self):
        scanned = 0
        for path in sorted(SHIMS_DIR.rglob("*")):
            if not path.is_file() or path.suffix == ".md":
                continue
            if _SKIP_DIRS & set(path.relative_to(SHIMS_DIR).parts):
                continue
            if path.name in _SKIP_FILES:
                continue
            scanned += 1
            # errors="replace": the deny tokens are ASCII, so any file — even
            # a stray binary — decodes well enough to match on them.
            hit = _DENY_RE.search(path.read_text(encoding="utf-8", errors="replace"))
            assert hit is None, (
                f"{path.relative_to(REPO_ROOT)} contains vault-semantic "
                f"token {hit.group(0)!r} — shims translate protocol only "
                "(dec-5a076384)"
            )
        assert scanned > 0, "shims/ scan found no source files — wrong path?"
