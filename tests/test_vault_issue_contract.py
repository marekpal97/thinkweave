"""Contract test for the vault ↔ issue division of labor (issue #63).

The issue-loop lands its outputs across four surfaces — the tracker, the PR,
the per-issue *trajectory* note, and the run's *session* note (`/wrap`). The
capture-parity contract (n-04674047) requires headless loop runs to feed the
vault identically to interactive work, and requires the boundary between those
surfaces to be a **written, tested contract** rather than tribal knowledge.

This test pins that boundary to executable artifacts:

* the contract doc (`docs/agents/vault-issue-contract.md`) is a *partition* —
  no field is claimed by two owners (the AC's "no field is written by two
  owners"), and `decisions` is owned by the session/`/wrap` row alone;
* `build_trajectory` (the loop's only vault-write payload) never mints a
  decision — its frontmatter carries no decision-note field and its `type`
  stays `note`;
* the loop command doc's §3 instructs exactly one `weave_create(type=note …)`
  and never a decision-mint or a `/wrap` — the loop never auto-mints decisions
  (they belong to the session-note owner);
* the contract is referenced from `issue-loop-memory.md`.

Expected owners/fields are hand-written from the issue's division-of-labor
table, and the decision-note field names from `core/schemas.py` +
`core/vault.py` (the real decision schema), so nothing is recomputed by the
code under test.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DOCS = _REPO / "docs" / "agents"
CONTRACT_DOC = _DOCS / "vault-issue-contract.md"
COMMAND_DOC = _DOCS / "issue-loop.command.md"
MEMORY_DOC = _DOCS / "issue-loop-memory.md"

# Load the deterministic loop rail the same way tests/test_issue_loop.py does.
_SPEC = importlib.util.spec_from_file_location(
    "issue_loop", _REPO / "scripts" / "issue_loop.py"
)
issue_loop = importlib.util.module_from_spec(_SPEC)
sys.modules["issue_loop"] = issue_loop
_SPEC.loader.exec_module(issue_loop)


# The four owners and the fields each owns — copied verbatim from the issue's
# division-of-labor table (the acceptance criteria). The contract doc must
# encode exactly this partition.
EXPECTED_OWNERS: dict[str, set[str]] = {
    "tracker": {"run history", "claims", "gate evidence"},
    "pr body": {"diff summary", "gate table", "smell report"},
    "trajectory note": {"how it went", "lessons"},
    "session note": {"cross-issue synthesis", "decisions", "insights"},
}

# Decision-note frontmatter markers — from core/vault.py (`status` default at
# creation) and synthesis/judge.py / lifecycle (`predicted_outcome`,
# `prediction_history`, `supersedes`, `superseded_by`, `file_paths`). A
# trajectory note that carried any of these would be minting a decision.
DECISION_FIELDS = {
    "status",
    "predicted_outcome",
    "prediction_history",
    "supersedes",
    "superseded_by",
    "file_paths",
}


def _section(doc: str, heading_num: str) -> str:
    """Return the text of a ``## <heading_num>.`` section up to the next ``## ``."""
    lines = doc.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if ln.startswith(f"## {heading_num}.")),
        None,
    )
    assert start is not None, f"section '## {heading_num}.' not found"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _parse_owner_table(doc: str) -> dict[str, set[str]]:
    """Parse the contract's ``| Surface | Owns |`` table into owner→fields.

    Keys are lower-cased and stripped of the ``comments``/``(/wrap)`` noise so
    they line up with EXPECTED_OWNERS; values split on commas.
    """
    owners: dict[str, set[str]] = {}
    for row in re.finditer(r"^\|(?P<a>[^|]+)\|(?P<b>[^|]+)\|\s*$", doc, re.MULTILINE):
        surface = row.group("a").strip().lower()
        owns = row.group("b").strip().lower()
        # skip header + separator rows
        if surface in ("surface", "") or set(surface) <= set("- :"):
            continue
        # normalize surface labels to the canonical owner keys
        surface = surface.replace("`", "").replace("(/wrap)", "")
        surface = surface.replace("comments", "").strip()
        fields = {f.strip() for f in owns.split(",") if f.strip()}
        owners[surface] = fields
    return owners


# ---------------------------------------------------------------------------
# The contract doc exists and encodes the four-owner partition


def test_contract_doc_exists():
    assert CONTRACT_DOC.exists(), (
        f"the division-of-labor contract must be documented at {CONTRACT_DOC}"
    )


def test_contract_doc_encodes_the_four_owners():
    owners = _parse_owner_table(CONTRACT_DOC.read_text(encoding="utf-8"))
    for name, fields in EXPECTED_OWNERS.items():
        assert name in owners, f"contract doc missing owner row: {name!r}"
        assert fields <= owners[name], (
            f"owner {name!r} must own {fields}, doc has {owners[name]}"
        )


def test_no_field_is_written_by_two_owners():
    """The AC's core invariant: the owner table is a partition."""
    owners = _parse_owner_table(CONTRACT_DOC.read_text(encoding="utf-8"))
    seen: dict[str, str] = {}
    for surface, fields in owners.items():
        for field in fields:
            assert field not in seen, (
                f"field {field!r} claimed by both {seen[field]!r} and {surface!r} "
                "— every field must have exactly one owner"
            )
            seen[field] = surface


def test_decisions_owned_by_session_not_trajectory():
    """A decision is never minted by both the loop and /wrap: the session/wrap
    row owns ``decisions``; the trajectory row does not."""
    owners = _parse_owner_table(CONTRACT_DOC.read_text(encoding="utf-8"))
    assert "decisions" in owners["session note"]
    assert "decisions" not in owners["trajectory note"]
    assert "lessons" in owners["trajectory note"]


def test_contract_doc_referenced_from_memory_doc():
    assert MEMORY_DOC.exists()
    assert "vault-issue-contract.md" in MEMORY_DOC.read_text(encoding="utf-8"), (
        "issue-loop-memory.md must reference the contract doc"
    )


# ---------------------------------------------------------------------------
# The loop's only vault-write payload never mints a decision


def _sample_trajectory() -> dict:
    issue = {
        "number": 63,
        "title": "Vault capture contract for headless loop sessions",
        "html_url": "https://github.com/x/y/issues/63",
        "labels": [{"name": "track:self-improvement"}],
    }
    return issue_loop.build_trajectory(
        issue,
        branch="loop/dag-54",
        commits=["a fix", "b test"],
        numstat="10\t2\tdocs/agents/vault-issue-contract.md\n",
        gates=[{"id": "tests", "kind": "command", "passed": True, "summary": "exit 0"}],
        fix_rounds=0,
        outcome="shipped",
        pr_url="https://github.com/x/y/pull/1",
        run_id="loop-20260718-dag54",
        primed=True,
        served=["n-prior1"],
    )


def test_trajectory_note_is_a_note_never_a_decision():
    payload = _sample_trajectory()
    assert payload["type"] == "note"


def test_trajectory_frontmatter_carries_no_decision_field():
    """The loop's §3 payload never contains decision-minting fields — its
    frontmatter and the real decision schema must be disjoint."""
    fm = _sample_trajectory()["frontmatter"]
    leaked = DECISION_FIELDS & set(fm)
    assert not leaked, f"trajectory frontmatter leaked decision fields: {leaked}"
    # ``outcome`` is an *observable* fact (shipped/routed-to-human), not the
    # decision lifecycle ``status`` — the two must not be conflated.
    assert "status" not in fm and "outcome" in fm


def test_trajectory_owns_how_it_went_and_lessons():
    """The trajectory body skeleton carries How it went + Lessons (its owned
    fields) and never a synthesis/decision heading (the session's)."""
    body = _sample_trajectory()["body_skeleton"]
    assert "## How it went" in body
    assert "## Lessons" in body
    assert "decision" not in body.lower()


# ---------------------------------------------------------------------------
# The command doc §3 stays inside the contract


def test_command_doc_section3_writes_one_note_never_a_decision():
    sec = _section(COMMAND_DOC.read_text(encoding="utf-8"), "3")
    assert "weave_create(type=note" in sec, (
        "§3 must instruct exactly one type=note trajectory write"
    )
    for mint in ("type=decision", "type: decision", "weave_create(type=decision"):
        assert mint not in sec, f"§3 must not instruct decision minting ({mint!r})"


def test_command_doc_section3_does_not_run_wrap():
    """The loop never runs /wrap or wrap-finalize itself — session synthesis
    (and decision promotion) belongs to the session-note owner, reached via the
    dream-wrap-worker catch-up (see the wrap-coverage section)."""
    sec = _section(COMMAND_DOC.read_text(encoding="utf-8"), "3").lower()
    assert "/wrap" not in sec
    assert "wrap-finalize" not in sec
