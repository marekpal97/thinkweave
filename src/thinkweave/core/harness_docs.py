"""Render harness-profile data for humans — degradation notes and, for
``docs/HARNESSES.md``, the generated capability matrix.

The epic's anti-goal (#103 / dec-5a076384) is a capability silently faked or
a hand-maintained capability table that rots (the vercel/memorix/hol-guard
pattern the issue names). So the prose people read is *rendered from* the same
``HarnessProfile`` rows the installer runs on — degradations included.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from thinkweave.core import fswrite
from thinkweave.core.harness import CANONICAL_EVENTS, PROFILES, HarnessProfile

MATRIX_START = "<!-- weave:harness-matrix:start — GENERATED from core/harness.py profiles; edit the profile, then `uv run python -m thinkweave.core.harness_docs --write` -->"
MATRIX_END = "<!-- weave:harness-matrix:end -->"


def render_degradations(profile: HarnessProfile) -> str:
    """The profile's degradations as a markdown bullet list, '' when none.

    Every row names the capability, whether it is documented-degraded or
    refused outright, the honest note, and the upstream evidence ref.
    """
    lines = []
    for d in profile.degradations:
        ref = f" ({d.upstream_ref})" if d.upstream_ref else ""
        lines.append(f"- **{d.capability}** — {d.mode}: {d.note}{ref}")
    return "\n".join(lines)


def _profiles() -> list[HarnessProfile]:
    # Aimed at a PurePosixPath home so every rendered path reads `~/…` with
    # forward slashes on any platform — committed docs must not vary by the
    # machine that regenerated them.
    return [factory(PurePosixPath("~")) for factory in PROFILES.values()]


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _event_cell(profile: HarnessProfile, event: str) -> str:
    # An event the profile has no key for is a different, worse state than an
    # explicit None mapping: the latter is an evidence-backed refusal, the
    # former means nobody has looked yet.
    if event not in profile.hook_events:
        return "unmapped — no verdict recorded"
    native = profile.hook_events[event]
    if native is None:
        return "— (no verified equivalent)"
    verified = profile.fires_verified.get(event)
    if verified:
        suffix = f"✓ {verified}" if native == event else f"`{native}` ✓ {verified}"
        return suffix
    if profile.hook_mechanism != "none":
        return "wired, unverified"
    return f"`{native}` (declared)"


def render_matrix() -> str:
    """The capability matrix + degradations, rendered from the profile rows."""
    ps = _profiles()
    head = " | ".join(p.display_name or p.id for p in ps)
    bar = "|" + "|".join(["---"] * (len(ps) + 1)) + "|"

    def row(label: str, cell) -> str:
        return f"| {label} | " + " | ".join(cell(p) for p in ps) + " |"

    lines = [
        "## Capability matrix",
        "",
        f"| | {head} |",
        bar,
        row("evidence", lambda p: p.evidence),
        row("eligibility (dec-5a076384 ladder)", lambda p: p.eligibility),
        row("detected by", lambda p: f"`{p.detect_dir}`"),
        row("lifecycle hooks", lambda p: p.hook_mechanism),
        row("subagent fan-out", lambda p: _yes(p.subagents)),
        row("headless slash skills", lambda p: _yes(p.headless_slash)),
        row(
            "native memory seam",
            lambda p: f"`{p.native_memory_artifact}`"
            if p.native_memory_artifact
            else "—",
        ),
        row("context channel", lambda p: f"`{p.context_channel}`"),
        row(
            "dispatch",
            lambda p: "`" + " ".join(p.headless_argv("<prompt>")) + "`",
        ),
        row(
            "transcripts",
            lambda p: f"`{p.transcript_glob}` ({p.transcript_format})",
        ),
        row("session ids", lambda p: f"`{p.session_id_scheme}`"),
        row(
            "MCP config",
            lambda p: f"`{p.mcp_config}` · key `{p.mcp_servers_key}`",
        ),
        row(
            "MCP native CLI",
            lambda p: f"`{p.mcp_via_cli}`" if p.mcp_via_cli else "—",
        ),
        row("instructions file", lambda p: f"`{p.instructions_file}`"),
        row("skills dir", lambda p: f"`{p.skills_dir}`"),
        "",
        "### Hook events (canonical → native, with observed-fire dates)",
        "",
        f"| canonical | {head} |",
        bar,
    ]
    for event in CANONICAL_EVENTS:
        lines.append(row(event, lambda p, e=event: _event_cell(p, e)))
    lines += [
        "",
        "### Documented degradations",
        "",
        "Nothing below is silently faked (#103 anti-goal): a listed capability",
        "degrades exactly as stated, and everything unlisted works as on",
        "Claude Code.",
    ]
    for p in ps:
        rendered = render_degradations(p)
        lines += ["", f"#### {p.display_name or p.id}", ""]
        lines.append(rendered if rendered else "None — the reference harness.")
    return "\n".join(lines)


def generated_block() -> str:
    return f"{MATRIX_START}\n\n{render_matrix()}\n\n{MATRIX_END}"


def splice(doc: str) -> str:
    """Replace the sentinel block in ``doc``, leaving every other byte alone.

    ``on_missing="error"`` is the generator-fingerprint gate: a generated
    block only ever overwrites a span its own sentinels mark."""
    return fswrite.replace_between(
        doc, MATRIX_START, MATRIX_END, generated_block(), on_missing="error"
    )


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Regenerate the HARNESSES.md capability matrix from the profiles."
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    # Repo-relative, like _canonical_hooks_path: every supported route is an
    # editable install from a checkout, so the docs tree is always present —
    # and when it is not, say so rather than compute a site-packages path.
    doc_path = Path(__file__).resolve().parents[3] / "docs" / "HARNESSES.md"
    if not doc_path.exists():
        sys.exit(
            f"error: {doc_path} not found — the generator must run from a "
            "thinkweave repo checkout (editable install)."
        )
    doc = doc_path.read_text(encoding="utf-8")
    try:
        updated = splice(doc)
    except ValueError:
        sys.exit(
            f"error: {doc_path} has lost its harness-matrix sentinel pair.\n"
            f"Restore both lines —\n  {MATRIX_START}\n  {MATRIX_END}\n"
            "— then re-run `uv run python -m thinkweave.core.harness_docs --write`."
        )
    if not args.write:
        print("stale" if updated != doc else "in sync")
        return
    fswrite.atomic_write_text(doc_path, updated)
    print(f"wrote {doc_path}")


if __name__ == "__main__":
    main()
