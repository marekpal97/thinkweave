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

from personal_mem.config import Config
from personal_mem.indexer import Indexer
from personal_mem.schemas import NoteType
from personal_mem.vault import VaultManager


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
