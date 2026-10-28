"""The round manifest: which policy a stint of work ran under.

``collect`` is the one producer. Its dict is the ``manifest`` round field
(``task_contract._ROUND_CHECKERS``): the rsi-rail repo's commit and dirty
flag when a repo is configured, the harness that ran the stint, and the
reader versions — the thinkweave source commit (or its package version),
the funloops commit the installed ``devloop`` was pinned to, and the plugin
versions a dev-link exposes to each harness.
"""

from __future__ import annotations

import json
import re
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import thinkweave
from thinkweave.core._utils import git_stdout
from thinkweave.core.harness import PROFILES, lookup

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


def collect(cfg, harness: str) -> dict:
    """The manifest for a stint starting now, under ``harness``. A
    configured rsi-rail that is not a committed git repo raises
    ``RuntimeError``: a round cannot name a policy that does not exist."""
    manifest = {"harness": harness, "readers": _readers()}
    if cfg.rsi_rail:
        manifest["rsi_rail"] = _rail(Path(cfg.rsi_rail))
    return manifest


def _rail(repo: Path) -> dict:
    commit = git_stdout("rev-parse", "HEAD", cwd=str(repo))
    if not _FULL_SHA_RE.fullmatch(commit):
        raise RuntimeError(
            f"rsi-rail at {repo} is not a git repository with a commit; "
            "the round cannot record which policy it ran under"
        )
    dirty = bool(git_stdout("status", "--porcelain", cwd=str(repo)))
    return {"repo": str(repo), "commit": commit, "dirty": dirty}


def _readers() -> dict:
    readers = {"thinkweave": _source_commit(thinkweave.__file__) or thinkweave.__version__}
    if funloops := _pinned("devloop"):
        readers["funloops"] = funloops
    plugins = {
        name: version for name in PROFILES if (version := _plugin_version(name))
    }
    if plugins:
        readers["plugins"] = plugins
    return readers


def _source_commit(module_file: str) -> str:
    """HEAD of the checkout a module runs from; ``""`` for an installed copy."""
    path = Path(module_file).resolve()
    if "site-packages" in path.parts:
        return ""
    return git_stdout("rev-parse", "HEAD", cwd=str(path.parent))


def _pinned(package: str) -> str:
    """The commit a git-installed package was resolved from, else its
    version; ``""`` when it is not installed."""
    try:
        dist = distribution(package)
    except PackageNotFoundError:
        return ""
    try:
        origin = json.loads(dist.read_text("direct_url.json") or "{}")
    except (OSError, json.JSONDecodeError):
        origin = {}
    return str(origin.get("vcs_info", {}).get("commit_id") or dist.version)


def _plugin_version(harness: str) -> str:
    """The version of the thinkweave plugin dev-linked into ``harness``."""
    profile = lookup(harness)
    if profile is None:
        return ""
    try:
        data = json.loads(
            (profile.dev_link / profile.plugin_manifest_relpath).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("version") or "")
