"""`weave session-id` — the harness-neutral session-id resolver (#103 wrap fix).

The bug it closes: `/wrap` resolved the current session only through
`$CLAUDE_SESSION_ID`, which Claude Code sets and Pi/Codex do not. On a
non-Claude harness the wrap read an empty value and minted a detached slug,
so `weave_extract` auto-created a *second* session note while the real
hook-created one kept none (Pi 2026-09-08 → ses-e5a02d7a vs ses-851a2f5c;
Codex 2026-09-05). The resolver reads each harness's own `session_id_env`
so the wrap lands on the note the hooks already created, by construction.

Driven through the real entry point (`main(argv)`) so parser → dispatch →
handler are pinned together, exactly like test_cli_surface.
"""

from __future__ import annotations

import pytest

from thinkweave.core import harness
from thinkweave.surfaces.cli import main


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    # A stray in-process override or a real harness's exported var would make
    # these cases non-hermetic — clear both, plus the two session-id vars any
    # profile declares, before each case sets exactly what it needs.
    monkeypatch.setattr(harness, "_OVERRIDE", None)
    monkeypatch.delenv("THINKWEAVE_HARNESS", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("PI_SESSION_ID", raising=False)


def test_claude_code_prints_its_env_var(monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "cc-uuid-123")
    main(["session-id"])
    assert capsys.readouterr().out.strip() == "cc-uuid-123"


def test_pi_var_resolves_even_without_thinkweave_harness(monkeypatch, capsys):
    # The regression: a wrap on Pi runs as a model turn with no `--harness`
    # argv and usually no `$THINKWEAVE_HARNESS`, so `harness.active()` reports
    # claude-code. The resolver must still find PI_SESSION_ID by scanning every
    # profile's declared var — not read the Claude-only one and come up empty.
    monkeypatch.setenv("PI_SESSION_ID", "pi-uuid-456")
    main(["session-id"])
    assert capsys.readouterr().out.strip() == "pi-uuid-456"


def test_active_harness_var_wins_when_both_present(monkeypatch, capsys):
    # Pinning the harness (via $THINKWEAVE_HARNESS) makes its own var the
    # authoritative one even if another harness's leaked into the environment.
    monkeypatch.setenv("THINKWEAVE_HARNESS", "pi")
    monkeypatch.setenv("PI_SESSION_ID", "pi-uuid-456")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "cc-uuid-123")
    main(["session-id"])
    assert capsys.readouterr().out.strip() == "pi-uuid-456"


def test_codex_exits_nonzero_with_empty_output(monkeypatch, capsys):
    # Codex exports no session-id var — the resolver must fail silently so
    # `id=$(weave session-id)` leaves $id empty and /wrap falls back to
    # recency + the #209 identity guard rather than minting a fresh slug.
    monkeypatch.setenv("THINKWEAVE_HARNESS", "codex")
    with pytest.raises(SystemExit) as exc:
        main(["session-id"])
    assert exc.value.code == 1
    assert capsys.readouterr().out.strip() == ""


def test_no_variable_set_exits_nonzero(monkeypatch, capsys):
    # Genuinely headless (no harness var at all) → same silent non-zero.
    with pytest.raises(SystemExit) as exc:
        main(["session-id"])
    assert exc.value.code == 1
    assert capsys.readouterr().out.strip() == ""
