"""Configuration loading for personal_mem.

Priority (vault_root resolution):
1. ``PERSONAL_MEM_VAULT`` env var
2. ``~/.config/personal-mem/config.toml`` (XDG-respectful user-scope file)
3. ``vault/.mem/config.toml`` (vault-internal — also owns embedding/edge/dream)
4. Built-in defaults

The user-scope tier (2) only ever provides ``vault_root``; vault-internal
fields (embeddings, edges, dream gates) remain owned by the vault-internal
file at tier 3.
"""

from __future__ import annotations

import os
import platform
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


_DEFAULT_VAULT = Path.home() / "vault"


def _is_windows() -> bool:
    return platform.system() == "Windows"


@dataclass
class PromptTimeRetrieval:
    """Config for prompt-time retrieval enrichment (R2).

    On each substantive user prompt the UserPromptSubmit hook runs a bounded
    hybrid search, drops anything already served this session, applies hard
    caps, and prepends a small ignorable block. Defaults are deliberately
    conservative — this is default-on, so the caps are the safety net against
    the noise-tax failure mode that retired the old pre-Edit injection.
    """

    enabled: bool = True
    # Triviality gate only — skip trivially short inputs and slash-commands so
    # we don't pay an embedding on "ok"/"yes"/"/clear". NOT a semantic filter;
    # relevance is decided entirely by the cosine floor below.
    min_prompt_chars: int = 12
    # Wall-clock budget for the similarity (embedding) arm. The FTS arm is
    # synchronous; on overrun the similarity arm is abandoned (daemon thread)
    # and we fall back to FTS. Generous enough to let the embedding complete on
    # a normal network; kept under the UserPromptSubmit hook timeout (10s).
    embed_deadline_seconds: float = 4.0
    # Cosine floor on the similarity arm — THE relevance gate. Drops low-cosine
    # nearest neighbours, so generic/meta prompts (~0.22–0.36) no-op while
    # domain prompts (~0.40+) fire. Model-dependent (text-embedding-3-small);
    # retune if you swap embedding models.
    min_similarity: float = 0.38
    # Per-turn caps.
    max_pieces_per_turn: int = 3
    max_injected_chars_per_turn: int = 1200
    # Per-session caps — make R2 self-extinguish as the session matures.
    max_firings_per_session: int = 8
    max_injected_chars_per_session: int = 6000
    # Bias toward the axes startup under-serves (sources + learnings); decisions
    # are already well-exposed at boot (Key Files table + Decisions Worth
    # Understanding), so they're left out of the default bias.
    bias_types: tuple[str, ...] = ("source", "note")


@dataclass
class Config:
    vault_root: Path = field(default_factory=lambda: _DEFAULT_VAULT)
    default_project: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key_env: str = "OPENAI_API_KEY"  # env var name holding the key
    embedding_api_url: str = "https://api.openai.com/v1/embeddings"

    # Edge generation thresholds
    concept_edge_threshold: int = 1  # min shared concepts for relates_to edge
    concept_edge_max_freq_pct: float = 0.05  # skip concepts in >5% of notes
    tag_edge_threshold: int = 2  # min shared tags for relates_to edge
    tag_edge_max_freq_pct: float = 0.10  # skip tags in >10% of notes
    tag_edge_exclude: tuple[str, ...] = ("todo", "probe", "parked", "til")

    # Dream apply-phase gates (Slice 1.5)
    # When False (default), priority_signals with action='enqueue' are
    # counted as logged-only — the LLM's intent is preserved on disk
    # but no queue write happens. Flip to True after the first cycle's
    # report has been reviewed.
    dream_enqueue_priority_signals: bool = False

    # C19b — per-concept PageRank. When True, the dream apply phase
    # computes per-concept-induced-subgraph PageRank after each
    # rebuild. Stored in the ``graph_ranks`` table; consumed by
    # ``mem_concepts(action='canonical_for', concept=X)``. Adds ~1s
    # per 100 active concepts on a typical vault (pure-Python power
    # iteration), so off by default until the user opts in.
    dream_compute_pagerank: bool = False

    # R2 — prompt-time retrieval enrichment (see PromptTimeRetrieval).
    retrieval_prompt_time: PromptTimeRetrieval = field(
        default_factory=PromptTimeRetrieval
    )

    @property
    def mem_dir(self) -> Path:
        return self.vault_root / ".mem"

    @property
    def config_dir(self) -> Path:
        return self.vault_root / "config"

    @property
    def index_db(self) -> Path:
        return self.mem_dir / "index.db"

    @property
    def embeddings_db(self) -> Path:
        return self.mem_dir / "embeddings.db"

    @property
    def config_path(self) -> Path:
        return self.mem_dir / "config.toml"

    @property
    def templates_dir(self) -> Path:
        return self.vault_root / "templates"


class LegacyConfigLocationError(RuntimeError):
    """Raised when a user-editable config still sits at the deprecated
    ``vault/.mem/<filename>`` path. Run ``scripts/move_configs_to_config_dir.sh``
    or ``mv`` the file to ``vault/config/<filename>``.
    """


def resolve_config_file(vault_root: Path, filename: str) -> Path:
    """Resolve a user-editable config file path under ``vault/config/``.

    Resolution rules:
    1. Canonical path (``<vault_root>/config/<filename>``) exists → return it.
    2. Only legacy path (``<vault_root>/.mem/<filename>``) exists → raise
       :class:`LegacyConfigLocationError`. The legacy fallback was retired
       in Phase 3.1B (2026-06-05); user is expected to move the file.
    3. Neither exists → return canonical path (writes commit forward).

    The returned path may not exist — the caller is responsible for the
    missing-file check.
    """
    new = vault_root / "config" / filename
    if new.exists():
        return new
    legacy = vault_root / ".mem" / filename
    if legacy.exists():
        raise LegacyConfigLocationError(
            f"{filename} still lives at vault/.mem/{filename}. "
            f"Move it to vault/config/{filename} "
            f"(e.g. `mv {legacy} {new}`) — the legacy fallback was retired "
            f"in Phase 3.1B."
        )
    return new  # neither exists — return canonical so writes commit forward


def user_config_path() -> Path:
    """Path to the user-scope personal-mem config, idiomatic per-OS.

    Resolution order for the base dir:
    1. ``$XDG_CONFIG_HOME`` when set (honoured on every OS — some Windows
       users export it deliberately).
    2. Windows: ``%APPDATA%`` (e.g. ``C:\\Users\\x\\AppData\\Roaming``).
    3. Otherwise: ``~/.config``.

    The final file is ``<base>/personal-mem/config.toml``. This is the tier
    ``/onboard`` writes to when persisting the user's chosen vault root —
    the seam that lets the plugin path work without a shell-rc edit. Reader
    (:func:`_load_user_config_vault_root`) and writer
    (:func:`write_user_config`) both go through this one function, so the
    per-OS branch can never drift between them.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg)
    elif _is_windows() and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path.home() / ".config"
    return base / "personal-mem" / "config.toml"


def user_cache_dir() -> Path:
    """Platform cache base for personal_mem runtime artifacts (cron/Task
    Scheduler logs, the spend ledger).

    Resolution order mirrors :func:`user_config_path`:
    1. ``$XDG_CACHE_HOME`` when set.
    2. Windows: ``%LOCALAPPDATA%`` (e.g. ``C:\\Users\\x\\AppData\\Local``).
    3. Otherwise: ``~/.cache``.

    Returns ``<base>/personal_mem`` (underscore form, matching the historic
    ``~/.cache/personal_mem`` layout the example crontab logs into).
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        base = Path(xdg)
    elif _is_windows() and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    else:
        base = Path.home() / ".cache"
    return base / "personal_mem"


def write_user_config(vault_root: Path) -> None:
    """Atomically persist ``vault_root`` to the user-scope config file.

    Creates parent dirs as needed. Mirrors the tempfile + ``os.replace``
    pattern from ``surfaces/cli/install.py:_atomic_write_json`` so an
    interrupted write never leaves a half-written TOML behind.

    The file shape is intentionally minimal — one key — because the
    vault-internal ``config.toml`` (tier 3) remains the home for
    embedding / edge / dream fields. This tier only ever sets the path.
    """
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f'vault_root = "{vault_root}"\n'
    # Atomic write via tempfile in the same dir (so os.replace is on the
    # same filesystem) + os.replace. Mirrors install.py._atomic_write_json.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup of the tempfile on failure
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def is_vault_initialized(cfg: Config) -> bool:
    """True iff the vault's canonical ``config/sources.yaml`` exists.

    Single canonical predicate for "is this vault wired up?" — used by
    the hook handler's early-return gate (replaces the bash gate in
    ``hooks/hooks.json``) and by ``/onboard``'s idempotency checks.
    Phase-3.1 moved this file from ``vault/.mem/sources.yaml`` to
    ``vault/config/sources.yaml``; the predicate tracks the canonical
    location only — legacy paths are not honoured.
    """
    return (cfg.vault_root / "config" / "sources.yaml").exists()


def _load_user_config_vault_root() -> Path | None:
    """Read ``vault_root`` from the user-scope config, if present.

    Returns ``None`` on missing file, missing key, or any parse error —
    callers fall through to the next tier. We deliberately swallow
    parse errors here rather than raising; the user-scope file is
    populated by ``/onboard`` and a malformed file shouldn't brick
    every ``mem`` invocation.
    """
    path = user_config_path()
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    vr = data.get("vault_root")
    if not vr:
        return None
    return Path(vr)


def load_config() -> Config:
    """Load config from env vars, user-scope TOML, vault-internal TOML, defaults.

    Vault-root precedence (high → low):
    1. ``PERSONAL_MEM_VAULT`` env var
    2. ``~/.config/personal-mem/config.toml`` (or ``$XDG_CONFIG_HOME``)
    3. ``vault/.mem/config.toml``
    4. Built-in default (``~/vault``)
    """
    cfg = Config()

    # Tier 1: env var (highest priority — preserves override-everything)
    vault_env = os.environ.get("PERSONAL_MEM_VAULT")
    # Tier 2: user-scope TOML (~/.config/personal-mem/config.toml).
    # Only sets vault_root; embedding/edge/dream fields stay owned
    # by the vault-internal config at tier 3.
    user_vault = _load_user_config_vault_root() if not vault_env else None
    if vault_env:
        cfg.vault_root = Path(vault_env)
    elif user_vault is not None:
        cfg.vault_root = user_vault

    # Tier 3: vault-internal config.toml. Only sets vault_root when
    # neither tier 1 (env) nor tier 2 (user-config) provided one — the
    # embedding/edge/dream fields below are always honoured because
    # they're owned by this tier alone.
    toml_path = cfg.config_path
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        if "vault_root" in data and not vault_env and user_vault is None:
            cfg.vault_root = Path(data["vault_root"])
        if "default_project" in data:
            cfg.default_project = data["default_project"]
        embed = data.get("embeddings", {})
        if "model" in embed:
            cfg.embedding_model = embed["model"]
        if "api_key_env" in embed:
            cfg.embedding_api_key_env = embed["api_key_env"]
        if "api_url" in embed:
            cfg.embedding_api_url = embed["api_url"]

        # Edge generation config
        edges = data.get("edges", {})
        if "concept_threshold" in edges:
            cfg.concept_edge_threshold = int(edges["concept_threshold"])
        if "concept_max_freq_pct" in edges:
            cfg.concept_edge_max_freq_pct = float(edges["concept_max_freq_pct"])
        if "tag_threshold" in edges:
            cfg.tag_edge_threshold = int(edges["tag_threshold"])
        if "tag_max_freq_pct" in edges:
            cfg.tag_edge_max_freq_pct = float(edges["tag_max_freq_pct"])
        if "tag_exclude" in edges:
            cfg.tag_edge_exclude = tuple(edges["tag_exclude"])

        # Dream apply-phase gates
        dream_cfg = data.get("dream", {})
        if "enqueue_priority_signals" in dream_cfg:
            cfg.dream_enqueue_priority_signals = bool(
                dream_cfg["enqueue_priority_signals"]
            )
        if "compute_pagerank" in dream_cfg:
            cfg.dream_compute_pagerank = bool(dream_cfg["compute_pagerank"])

        # R2 — prompt-time retrieval enrichment ([retrieval.prompt_time])
        pt = data.get("retrieval", {}).get("prompt_time", {})
        if pt:
            rpt = cfg.retrieval_prompt_time
            if "enabled" in pt:
                rpt.enabled = bool(pt["enabled"])
            if "min_prompt_chars" in pt:
                rpt.min_prompt_chars = int(pt["min_prompt_chars"])
            if "embed_deadline_seconds" in pt:
                rpt.embed_deadline_seconds = float(pt["embed_deadline_seconds"])
            if "min_similarity" in pt:
                rpt.min_similarity = float(pt["min_similarity"])
            if "max_pieces_per_turn" in pt:
                rpt.max_pieces_per_turn = int(pt["max_pieces_per_turn"])
            if "max_injected_chars_per_turn" in pt:
                rpt.max_injected_chars_per_turn = int(pt["max_injected_chars_per_turn"])
            if "max_firings_per_session" in pt:
                rpt.max_firings_per_session = int(pt["max_firings_per_session"])
            if "max_injected_chars_per_session" in pt:
                rpt.max_injected_chars_per_session = int(
                    pt["max_injected_chars_per_session"]
                )
            if "bias_types" in pt:
                rpt.bias_types = tuple(pt["bias_types"])

    # Per-field env overrides
    if os.environ.get("PERSONAL_MEM_PROJECT"):
        cfg.default_project = os.environ["PERSONAL_MEM_PROJECT"]
    if os.environ.get("PERSONAL_MEM_DB"):
        # Override index db path directly
        cfg._index_db_override = Path(os.environ["PERSONAL_MEM_DB"])

    cfg.default_project = normalize_project_name(cfg.default_project)
    return cfg


def normalize_project_name(name: str) -> str:
    """Canonicalize a project name: lowercase, dashes/spaces -> underscores.

    Prevents duplicate projects that differ only by separator or case
    (``trade-ideas`` vs ``trade_ideas``). Empty / None pass through as "".
    Applied at every boundary where a project name enters the system —
    config load and ``VaultManager.create_note`` — so a stray dash can
    never mint a second project folder.
    """
    if not name:
        return ""
    return name.strip().lower().replace("-", "_").replace(" ", "_")
