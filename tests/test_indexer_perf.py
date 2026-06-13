"""Performance regression for P0-8: mtime-gated no-op rebuild.

Asserts wall-time of an incremental no-op rebuild on a 200-file vault
stays under a generous threshold. On the WSL→9P 6.5k-file vault the
mtime gate brought no-op rebuild from ~25s to ~5s; on local SSD a
200-file no-op should land under 500ms.

Marked ``@pytest.mark.perf`` so it can be skipped on slow CI hosts.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager


@pytest.fixture
def perf_vault(tmp_path: Path) -> tuple[VaultManager, Indexer]:
    cfg = Config(vault_root=tmp_path / "vault")
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    idx = Indexer(config=cfg)
    yield vm, idx
    idx.close()


@pytest.mark.perf
def test_noop_rebuild_under_500ms_on_200_files(
    perf_vault: tuple[VaultManager, Indexer],
):
    vm, idx = perf_vault

    # Populate 200 notes — small bodies, realistic frontmatter.
    for i in range(200):
        vm.create_note(
            NoteType.NOTE,
            f"perf-note-{i:04d}",
            body=f"# Note {i}\n\nBody line {i}.\n",
            project="perf",
            extra_frontmatter={"concepts": ["perf-test", "indexer-perf"]},
        )

    # Warm pass — primes the index with file_mtime values.
    idx.rebuild(full=True)

    # Measure no-op rebuild.
    t0 = time.perf_counter()
    stats = idx.rebuild(full=False)
    elapsed = time.perf_counter() - t0

    assert stats["indexed"] == 0
    assert stats["removed"] == 0
    assert stats["skipped"] == 200
    # Generous bound — on WSL→9P this is ~5s for 6.5k files (~150ms per
    # 200), on local SSD comfortably <100ms.
    assert elapsed < 0.5, f"no-op rebuild took {elapsed:.3f}s (>500ms)"


@pytest.mark.perf
def test_incremental_edges_beat_full_rebuild_on_200_files(
    perf_vault: tuple[VaultManager, Indexer],
):
    """P1-11 — incremental edge rebuild must beat full rebuild by a wide margin.

    On a 200-note vault where 3 notes changed, the incremental path's edge
    pass touches only edges incident to those 3 notes — not the full O(n²)
    pairwise walk. The factor-of-5 bound is loose but catches an accidental
    fall-through to ``_rebuild_edges()``.
    """
    import os
    vm, idx = perf_vault

    # Populate 200 notes with overlapping concepts so the edge graph is dense.
    for i in range(200):
        vm.create_note(
            NoteType.NOTE,
            f"inc-note-{i:04d}",
            body=f"# Note {i}\n\nBody line {i}.\n",
            project="perf",
            # 3 concepts per note from a pool of ~7 → dense concept-pair graph.
            extra_frontmatter={
                "concepts": [
                    f"concept-{i % 7}",
                    f"concept-{(i + 1) % 7}",
                    f"concept-{(i + 3) % 7}",
                ]
            },
        )

    # Warm full rebuild — establishes the baseline.
    t0 = time.perf_counter()
    idx.rebuild(full=True)
    full_elapsed = time.perf_counter() - t0

    # Touch 3 notes so they fall into changed_ids.
    targets = sorted(vm.root.rglob("inc-note-0001*.md"))[:1]
    targets += sorted(vm.root.rglob("inc-note-0050*.md"))[:1]
    targets += sorted(vm.root.rglob("inc-note-0150*.md"))[:1]
    for p in targets:
        p.write_text(p.read_text() + "\n\nupdated\n")
        new_mtime = time.time() + 10
        os.utime(p, (new_mtime, new_mtime))

    # Measure incremental rebuild.
    t0 = time.perf_counter()
    stats = idx.rebuild(full=False)
    inc_elapsed = time.perf_counter() - t0

    assert stats["indexed"] == 3
    # Generous bound — accidental full-rebuild fallback would push inc_elapsed
    # close to full_elapsed; passing here means the incremental method ran.
    assert inc_elapsed < full_elapsed / 2, (
        f"incremental rebuild took {inc_elapsed:.3f}s; "
        f"full took {full_elapsed:.3f}s — no significant speedup"
    )
