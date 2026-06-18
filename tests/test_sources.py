"""Tests for `weave sources scaffold` and the user-side registry overlay.

Covers:
  1. The CLI scaffold writes all three artifacts with the right content.
  2. ``load_user_specs`` parses the scaffolded YAML; ``get_spec`` with a
     vault_root returns the user-side spec.
  3. Re-scaffolding the same slug into the same vault refuses with a
     non-zero exit and a clear error.
  4. Scaffolding a slug that collides with the in-code REGISTRY refuses
     cleanly without writing anything.
  5. Regression: ``get_spec(slug)`` (no vault_root) still returns in-code
     REGISTRY entries unchanged — pre-overlay callers stay correct.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest

from thinkweave.acquisition.sources import REGISTRY, get_spec, load_user_specs
from thinkweave.surfaces.cli.util import cmd_sources


def _scaffold_args(
    slug: str,
    bucket: str,
    layout: str = "folder",
    description: str = "",
    aliases: str = "",
    skill_target: str = "repo",
) -> argparse.Namespace:
    return argparse.Namespace(
        sources_action="scaffold",
        slug=slug,
        bucket=bucket,
        layout=layout,
        description=description,
        aliases=aliases,
        skill_target=skill_target,
    )


@pytest.fixture
def isolated_commands_dir(monkeypatch, tmp_path: Path):
    """Redirect commands/ writes to a tmp dir so tests can't pollute the repo.

    The scaffold path resolves ``commands/`` from the package location
    (four levels up from ``surfaces/cli/util.py``). To intercept that we
    overlay an isolated tree containing a fresh ``src/thinkweave/...``
    layout, then point the scaffold at it by patching ``__file__``-based
    path resolution.

    Implementation: we copy the package's vault_templates/ into a tmp
    location and monkeypatch ``Path(__file__).resolve().parents`` lookup
    by intercepting the constants the scaffold reads.
    """
    # We instead patch the module-level lookups directly. The scaffold
    # reads `pkg_root` via Path(__file__).resolve().parents[1].parent —
    # rather than fight that, redirect the repo_root by symlinking the
    # template into our tmp tree and constructing a "fake repo" layout.
    fake_repo = tmp_path / "_fake_repo"
    fake_pkg = fake_repo / "src" / "thinkweave"
    fake_pkg.mkdir(parents=True)

    # Copy the vault_templates we depend on.
    src_pkg = (
        Path(__file__).resolve().parents[1] / "src" / "thinkweave"
    )
    shutil.copytree(src_pkg / "vault_templates", fake_pkg / "vault_templates")

    # Provide a fake `surfaces/cli/util.py` location so the scaffold's
    # parents[1].parent points into fake_pkg. We patch Path resolution
    # by monkeypatching the helper that computes pkg_root.
    fake_util = fake_pkg / "surfaces" / "cli" / "util.py"
    fake_util.parent.mkdir(parents=True)
    fake_util.write_text("# stub", encoding="utf-8")

    # The scaffold reads its own __file__. Patch it.
    import thinkweave.surfaces.cli.util as util_mod

    original_file = util_mod.__file__
    monkeypatch.setattr(util_mod, "__file__", str(fake_util))
    yield fake_repo
    monkeypatch.setattr(util_mod, "__file__", original_file)


@pytest.fixture
def vault(monkeypatch, tmp_path: Path) -> Path:
    """A throwaway vault root, exposed via THINKWEAVE_VAULT."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setenv("THINKWEAVE_VAULT", str(vault_root))
    return vault_root


def test_scaffold_writes_all_three_artifacts(
    vault: Path, isolated_commands_dir: Path, capsys
) -> None:
    args = _scaffold_args(
        slug="demo",
        bucket="demos",
        layout="folder",
        description="Demo source for scaffold tests",
        aliases="legacy-demo,demo-old",
    )
    cmd_sources(args)
    out = capsys.readouterr().out
    assert "Scaffolded source type 'demo'" in out

    # 1. user-side YAML overlay
    user_yaml = vault / "config" / "source_types.yaml"
    assert user_yaml.exists(), "source_types.yaml should be created"
    body = user_yaml.read_text(encoding="utf-8")
    assert "demo:" in body
    assert "bucket: demos" in body
    assert "layout: folder" in body
    assert 'description: "Demo source for scaffold tests"' in body
    assert "aliases: [legacy-demo, demo-old]" in body

    # 2. skill file under the (isolated) commands/ directory
    skill_path = isolated_commands_dir / "commands" / "demo.md"
    assert skill_path.exists(), "skill file should be created"
    skill_text = skill_path.read_text(encoding="utf-8")
    assert "source_type: demo" in skill_text
    assert "vault/sources/demos/" in skill_text
    assert "**folder**" in skill_text
    # Template placeholders should have been substituted.
    assert "{slug}" not in skill_text
    assert "{bucket}" not in skill_text
    assert "{layout}" not in skill_text
    # Escaped braces in weave_create({{...}}) should resolve to single braces.
    assert "frontmatter={" in skill_text
    assert 'frontmatter={{' not in skill_text

    # 3. default behaviour-config block in the LIVE vault overlay the runtime
    #    reads — <vault>/config/sources.yaml — NOT the shipped package template
    #    (which is seed-only and clobbered on upgrade).
    vault_sources = vault / "config" / "sources.yaml"
    assert vault_sources.exists()
    vault_sources_text = vault_sources.read_text(encoding="utf-8")
    assert "  demo:" in vault_sources_text
    assert "drain_strategy: inline" in vault_sources_text

    # The shipped template must be left untouched.
    template_sources = (
        isolated_commands_dir / "src" / "thinkweave" / "vault_templates"
        / "config" / "sources.yaml"
    )
    assert "  demo:" not in template_sources.read_text(encoding="utf-8")


def test_load_user_specs_returns_scaffolded_entry(
    vault: Path, isolated_commands_dir: Path, capsys
) -> None:
    cmd_sources(
        _scaffold_args(
            slug="podcast",
            bucket="podcasts",
            layout="author_folder",
            description="Podcast episodes",
            aliases="audio,pod",
        )
    )
    capsys.readouterr()

    specs = load_user_specs(vault)
    assert "podcast" in specs
    spec = specs["podcast"]
    assert spec.slug == "podcast"
    assert spec.bucket == "podcasts"
    assert spec.layout == "author_folder"
    assert spec.aliases == ("audio", "pod")
    assert spec.description == "Podcast episodes"

    # get_spec with vault_root surfaces the user-side entry…
    fetched = get_spec("podcast", vault_root=vault)
    assert fetched is not None
    assert fetched.slug == "podcast"
    assert fetched.bucket == "podcasts"

    # …and aliases resolve to the canonical slug.
    aliased = get_spec("audio", vault_root=vault)
    assert aliased is not None
    assert aliased.slug == "podcast"


def test_scaffold_refuses_duplicate_user_slug(
    vault: Path, isolated_commands_dir: Path, capsys
) -> None:
    cmd_sources(_scaffold_args(slug="demo", bucket="demos"))
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        cmd_sources(_scaffold_args(slug="demo", bucket="other"))
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "already declared" in err
    assert "source_types.yaml" in err


def test_scaffold_refuses_in_code_registry_collision(
    vault: Path, isolated_commands_dir: Path, capsys
) -> None:
    # `paper` is part of the in-code REGISTRY.
    assert "paper" in REGISTRY
    with pytest.raises(SystemExit) as exc:
        cmd_sources(_scaffold_args(slug="paper", bucket="papers"))
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "built-in source type" in err
    # And nothing should have been written.
    assert not (vault / "config" / "source_types.yaml").exists()


def test_get_spec_without_vault_root_unchanged_for_in_code_specs() -> None:
    """Regression: pre-overlay callers (no vault_root) see the in-code
    REGISTRY unchanged. The default arg keeps the signature
    backwards-compatible.
    """
    spec = get_spec("paper")  # no vault_root
    assert spec is not None
    assert spec.slug == "paper"
    assert spec.bucket == "papers"
    assert spec.layout == "folder"

    # And unregistered slugs without a vault_root remain None — the
    # open-world fallback shouldn't accidentally hit user overlays.
    assert get_spec("definitely-not-a-slug-xyz") is None
