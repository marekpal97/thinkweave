"""The round manifest: which policy a stint of work ran under.

``collect`` is the one producer. Its dict is the ``manifest`` round field
(``task_contract._ROUND_CHECKERS``): the rsi-rail repo's commit and dirty
flag when a repo is configured, the harness that ran the stint, and the
reader versions — the thinkweave source commit (or its package version),
the funloops commit the installed ``devloop`` was pinned to, and the plugin
versions a dev-link exposes to each harness.

The harness readout (dec-4f158b58) rides along: the handful of settings
values that change how the agent behaves (``harness_settings``), the hook
events the profile registers, and the profile's hook literals. Witnessed,
never held: the settings file is the harness's own, rewritten with machine
state, so the round records what it said rather than the rail owning it.
Permissions and env are deliberately left out.
"""

from __future__ import annotations

import json
import re
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import thinkweave
from thinkweave.core._utils import git_stdout
from thinkweave.core.harness import PROFILES, HarnessProfile, active, lookup

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


#: The settings keys the readout witnesses: the ones that change how the
#: agent behaves, not machine state, permissions or env.
HARNESS_SETTINGS_KEYS = ("model", "effortLevel", "outputStyle", "tui")


def collect(cfg, harness: str, *, model: str = "") -> dict:
    """The manifest for a stint starting now, under ``harness``. ``model``
    is the one the harness reported running (a hook input's ``model``); it
    wins over the settings file's. A configured rsi-rail that is not a
    committed git repo raises ``RuntimeError``: a round cannot name a
    policy that does not exist."""
    manifest = {"harness": harness, "readers": _readers()}
    if cfg.rsi_rail:
        manifest["rsi_rail"] = _rail(Path(cfg.rsi_rail))
    profile = _profile(harness)
    if settings := _harness_settings(profile, model):
        manifest["harness_settings"] = settings
    if profile is not None:
        if events := sorted(e for e, native in profile.hook_events.items() if native):
            manifest["hook_events"] = events
        if literals := _literals(profile):
            manifest["literals"] = literals
    return manifest


def _profile(harness: str) -> HarnessProfile | None:
    """The running profile when it is ``harness``, else the registered one."""
    running = active()
    return running if running.id == harness else lookup(harness)


def _harness_settings(profile: HarnessProfile | None, model: str) -> dict:
    """The witnessed values of :data:`HARNESS_SETTINGS_KEYS` from the
    profile's settings file, scalars only; ``model`` overrides the file's."""
    data: dict = {}
    if profile is not None:
        try:
            loaded = json.loads(profile.user_settings.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    settings = {
        key: data[key]
        for key in HARNESS_SETTINGS_KEYS
        if isinstance(data.get(key), (str, bool, int, float))
    }
    if model:
        settings["model"] = model
    return settings


def _literals(profile: HarnessProfile) -> dict:
    """The profile's hook literals: per-event budgets and the context cap."""
    literals: dict = {}
    if profile.hook_budget_ms:
        literals["hook_budget_ms"] = dict(profile.hook_budget_ms)
    if profile.additional_context_cap_chars is not None:
        literals["additional_context_cap_chars"] = profile.additional_context_cap_chars
    return literals


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
