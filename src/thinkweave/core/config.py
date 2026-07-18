"""Configuration loading for thinkweave.

Priority (vault_root resolution):
1. ``THINKWEAVE_VAULT`` env var
2. ``~/.config/thinkweave/config.toml`` (XDG-respectful user-scope file)
3. ``vault/config/config.toml`` (vault-internal — also owns embedding/edge/dream;
   a pre-2026-06-13 file at ``vault/.weave/config.toml`` is read as a fallback)
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


def _resolve_weave_dir_value(value: str, vault_root: Path) -> Path:
    """Expand ``~`` and anchor a relative path at ``vault_root``.

    Shared by the ``weave_dir`` TOML key and the ``THINKWEAVE_WEAVE_DIR``
    env override so both tiers apply the same rule: absolute paths pass
    through unchanged; relative paths resolve against the vault root, not
    the process cwd.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = vault_root / path
    return path


@dataclass
class PromptTimeRetrieval:
    """Config for prompt-time retrieval enrichment (R2).

    On each substantive user prompt the UserPromptSubmit hook runs a bounded
    similarity search, drops anything already served this session, applies
    hard caps, and prepends a small ignorable block. Defaults are deliberately
    conservative — this is default-on, so the caps are the safety net against
    the noise-tax failure mode that retired the old pre-Edit injection.
    """

    enabled: bool = True
    # Triviality gate only — skip trivially short inputs and slash-commands so
    # we don't pay an embedding on "ok"/"yes"/"/clear". NOT a semantic filter;
    # relevance is decided entirely by the cosine floor below.
    min_prompt_chars: int = 12
    # Wall-clock budget for the similarity (embedding) arm, run in a daemon
    # thread; on overrun the arm is abandoned and the turn gets no injection
    # (see operations/prompt_time_retrieval.py — there is no FTS fallback,
    # it was a structural no-op against prose prompts and was removed).
    # Generous enough to let the embedding complete on a normal network; kept
    # under the UserPromptSubmit hook timeout.
    embed_deadline_seconds: float = 4.0
    # Adaptive skip — consecutive deadline misses (embedding thread still
    # running when the wall-clock deadline hits) this session before the
    # similarity arm is skipped for the rest of the session. Protects against
    # re-paying a sunk ~embed_deadline_seconds cost on every remaining turn
    # when the embedding endpoint is slow or unreachable.
    deadline_miss_limit: int = 3
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
    # Relocates derived state (index.db, embeddings.db, buffer/, logs — see
    # weave_dir below) off the vault path. The vault markdown must stay put
    # (Obsidian owns it), but everything under weave_dir is derived and
    # rebuildable, so it can move to fast local disk when the vault lives on
    # slow/remote/virtualized storage (e.g. a Windows drive crossed from
    # WSL2 via 9P — measured ~30x slower SQLite reads — or a NAS/Dropbox
    # mount). None (default) keeps the historical vault_root/.weave layout.
    weave_dir_override: Path | None = None
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
    # ``weave_concepts(action='canonical_for', concept=X)``. Adds ~1s
    # per 100 active concepts on a typical vault (pure-Python power
    # iteration), so off by default until the user opts in.
    dream_compute_pagerank: bool = False

    # Cap on essence candidates surfaced per scan (themes + concept hubs,
    # placeholder-first). Keeps the nightly essence-worker payload bounded;
    # 0 = unlimited (the backfill lever — `weave dream scan --essence-cap 0`).
    dream_essence_cap: int = 12

    # Drift v2 — embedding-geometry dedup (concepts AND themes).
    # ``cosine_threshold``: pairs at/above go to the merge worker.
    # ``drift_cap``: max concept pairs surfaced per scan, cosine-ranked
    # (0 = unlimited). Judged pairs are excluded via the maintenance-log
    # verdicts history, so the cap bounds payload size without starving
    # the tail the way the old lexical [:5] slice did.
    dream_cosine_threshold: float = 0.8
    dream_drift_cap: int = 15

    # Grain coarsening (drift v2, N-ary). The tighten worker may collapse a
    # tight near-clique of fine concepts/themes onto one coarser term.
    # ``coarsen_threshold``: complete-linkage floor — stricter than the
    # pairwise ``cosine_threshold`` because a fold is destructive.
    # ``coarsen_cap``: clusters surfaced per scan, per family (cohesion-ranked).
    # ``coarsen_max_size``: bounds the greedy clique-grow.
    # ``coarsen_apply``: True = nightly applies the fold; False = surface-only
    # (the worker still judges and the verdict is recorded, but apply skips
    # the fold so the on-demand ``/tighten`` front door applies on approval).
    dream_coarsen_threshold: float = 0.85
    dream_coarsen_cap: int = 3
    dream_coarsen_max_size: int = 6
    dream_coarsen_apply: bool = True

    # Max folded hubs the phase-2 seam-link worker drains per cycle.
    dream_seam_link_cap: int = 10

    # Promotion gate (proposed → canonical concepts). ``threshold`` is the
    # min proposed-concept count for promotion eligibility; ``cap`` bounds
    # how many candidates one scan surfaces. The ``weave dream scan``
    # ``--promotion-{threshold,cap}`` flags override per-invocation; these
    # fields steer cron (which passes no flags).
    dream_promotion_threshold: int = 5
    dream_promotion_cap: int = 20

    # Probe-pressure lookback window (days). Read by BOTH probe surfaces in
    # the dream scan — the ``recent_probes`` payload (priority worker) and
    # the knowledge-delta probe-match slice (digest worker).
    dream_probe_window_days: int = 14

    # Behavioral-salience activity window (days) for the digest's
    # "## Most actionable" active-focus block: which projects saw recent
    # sessions and which concepts are under probe pressure. The automatic
    # signal is the default; PRIORITIES.yaml ``focus.*`` lists are appended
    # as optional pins (a pinned-but-quiet project/concept still surfaces).
    salience_activity_window_days: int = 14

    # How many rejudge entries one cycle hands to the phase-2 judge worker.
    # Shared by the scan collector AND apply's consumption step — apply
    # removes exactly this prefix of the on-disk queue, so anything beyond
    # the cap survives for the next cycle.
    dream_rejudge_cap: int = 20

    # Trajectory outcome judge (issue #60 — the phase-2 ``dream-outcome-worker``).
    # ``phase2_days``: the closed-horizon delay before the delayed-signal
    # (rework-blame + revert) pass runs, measured from the PR's merge date.
    # ``rework_threshold``: the fraction of a merged diff rewritten within that
    # window above which phase-2 rules ``reworked-post-merge`` (a categorical
    # label — the raw blame counts are always recorded on the entry, never a
    # composite score). ``agent_identities``: substrings that mark a PR commit
    # as agent-produced (matched against author login/email/name, incl.
    # co-authors) — loop commits carry the Claude co-author, so a pure-human
    # rework commit lacking it flips merged-clean → reworked.
    dream_trajectory_phase2_days: int = 14
    dream_trajectory_rework_threshold: float = 0.5
    dream_trajectory_agent_identities: tuple[str, ...] = ("claude", "noreply@anthropic.com")

    # Evidence-gated steering (issue #62 — the gate the slow self-improvement
    # loop #61 calls before filing proposals). ``weekly_budget``: max proposals
    # filed per run, ranked by evidence weight — the anti-invention cap.
    # ``steering_weights``: per-signal multipliers for the evidence weight (raw
    # counts are always preserved; the weights only rank). Empty by default so
    # every signal weighs 1.0 (see operations/steering.DEFAULT_WEIGHTS); the
    # ``[steering]`` TOML section fills it from ``weight_<signal>`` keys.
    steering_weekly_budget: int = 3
    steering_weights: dict = field(default_factory=dict)

    # Knowledge-delta window (hours) for the phase-2 digest worker.
    dream_knowledge_delta_hours: int = 24

    # Catalyst entries shipped per essence candidate: ``max_catalysts``
    # for substantive essences, ``placeholder_max_catalysts`` for
    # placeholder ones (which need more material to compose fresh).
    dream_essence_max_catalysts: int = 10
    dream_essence_placeholder_max_catalysts: int = 25

    # Memory seam (CC auto-memory ↔ vault reconciliation, phase-2
    # ``dream-seam-worker``). ``cosine_twin`` / ``cosine_none`` are the
    # calibrated bands the worker reads off ``weave_search(mode='similar')``
    # (≥twin = real twin, <none = no twin; the gap is an LLM read).
    # ``stale_age_days`` is the project-type stale prior (a ``project`` CC
    # fact untouched this long is a stale-state risk). ``recheck_days``
    # re-validates resolved verdicts periodically so vault drift is caught.
    # ``cap`` bounds how many dirty facts one cycle hands the worker.
    seam_cosine_twin: float = 0.70
    seam_cosine_none: float = 0.55
    seam_stale_age_days: int = 30
    seam_recheck_days: int = 14
    seam_cap: int = 20

    # Extraction — max insight notes one ``weave_extract`` call creates.
    extract_insights_cap: int = 3

    # Inline backfill fan-out — the keyless ``weave import claude-code --enrich
    # --via inline`` path (/seed-enrich). At/below ``fanout_threshold`` pending
    # sessions it synthesises in-process (no spawn overhead — see the
    # "no subagent for small wraps" finding); above it, it deterministically
    # fans out subagents, ``batch_size`` sessions per worker, ``parallelism``
    # workers concurrent (mirrors /drain's writer fan-out). Distinct from the
    # API batch path's provider ``batch_concurrency`` (vault/config/api.yaml).
    enrich_fanout_threshold: int = 12
    enrich_batch_size: int = 6
    enrich_parallelism: int = 3

    # Theme cluster detection (synthesis/theme_candidates.detect_signals).
    # ``min_cluster_size``: smallest concept cluster that surfaces a signal;
    # ``recent_days``: event-grain source lookback; ``min_shared_concepts``:
    # concepts a concept-cluster's sources must share; ``name_family_jaccard``:
    # token-Jaccard at/above which two ``proposed_theme`` slugs join one arc
    # family; ``generic_concept_ratio``: concepts on more than this fraction
    # of the recent pool are "generic" and dropped from covering-theme scoring.
    theme_min_cluster_size: int = 3
    theme_recent_days: int = 30
    theme_min_shared_concepts: int = 2
    theme_name_family_jaccard: float = 0.5
    theme_generic_concept_ratio: float = 0.5

    # Deterministic staleness auto-resolve: an ``active`` theme whose newest
    # catalyst-log entry (or, for an empty stub, its created date) is older
    # than this many days is auto-marked ``resolved`` by the dream apply
    # phase. 0 disables. The only automatic theme-lifecycle trigger — and
    # mechanically observable (no semantic inference).
    theme_resolve_after_days: int = 60

    # Landing docs — ``open_probes_cap``: classified prompt-probes gathered
    # into the landing context; ``probes_display_cap``: probes the rendered
    # STATE doc's "Open Probes" section displays.
    landing_open_probes_cap: int = 20
    landing_probes_display_cap: int = 10

    # RRF fusion constant for hybrid search (Σ 1/(k + rank)). 60 is the
    # standard constant from the original RRF paper.
    retrieval_rrf_k: int = 60

    # R2 — prompt-time retrieval enrichment (see PromptTimeRetrieval).
    retrieval_prompt_time: PromptTimeRetrieval = field(
        default_factory=PromptTimeRetrieval
    )

    @property
    def weave_dir(self) -> Path:
        if self.weave_dir_override is not None:
            return self.weave_dir_override
        return self.vault_root / ".weave"

    @property
    def config_dir(self) -> Path:
        return self.vault_root / "config"

    @property
    def index_db(self) -> Path:
        return self.weave_dir / "index.db"

    @property
    def embeddings_db(self) -> Path:
        return self.weave_dir / "embeddings.db"

    @property
    def config_path(self) -> Path:
        """Vault-internal tunables file (embedding / edge / dream knobs).

        Canonical home is ``vault/config/config.toml`` — alongside the other
        user-editable config (2026-06-13: moved out of ``.weave/`` so the whole
        config surface lives in one folder). A pre-move file at
        ``vault/.weave/config.toml`` is still read as a transparent fallback;
        move it to ``config/`` when convenient.
        """
        canonical = self.config_dir / "config.toml"
        if canonical.exists():
            return canonical
        legacy = self.weave_dir / "config.toml"
        if legacy.exists():
            return legacy
        return canonical  # neither exists — return canonical so writes commit forward

    @property
    def templates_dir(self) -> Path:
        return self.vault_root / "templates"


class LegacyConfigLocationError(RuntimeError):
    """Raised when a user-editable config still sits at the deprecated
    ``vault/.weave/<filename>`` path. Run ``scripts/move_configs_to_config_dir.sh``
    or ``mv`` the file to ``vault/config/<filename>``.
    """


def resolve_config_file(vault_root: Path, filename: str) -> Path:
    """Resolve a user-editable config file path under ``vault/config/``.

    Resolution rules:
    1. Canonical path (``<vault_root>/config/<filename>``) exists → return it.
    2. Only legacy path (``<vault_root>/.weave/<filename>``) exists → raise
       :class:`LegacyConfigLocationError`. The legacy fallback was retired
       in Phase 3.1B (2026-06-05); user is expected to move the file.
    3. Neither exists → return canonical path (writes commit forward).

    The returned path may not exist — the caller is responsible for the
    missing-file check.
    """
    new = vault_root / "config" / filename
    if new.exists():
        return new
    legacy = vault_root / ".weave" / filename
    if legacy.exists():
        raise LegacyConfigLocationError(
            f"{filename} still lives at vault/.weave/{filename}. "
            f"Move it to vault/config/{filename} "
            f"(e.g. `mv {legacy} {new}`) — the legacy fallback was retired "
            f"in Phase 3.1B."
        )
    return new  # neither exists — return canonical so writes commit forward


def user_config_path() -> Path:
    """Path to the user-scope thinkweave config, idiomatic per-OS.

    Resolution order for the base dir:
    1. ``$XDG_CONFIG_HOME`` when set (honoured on every OS — some Windows
       users export it deliberately).
    2. Windows: ``%APPDATA%`` (e.g. ``C:\\Users\\x\\AppData\\Roaming``).
    3. Otherwise: ``~/.config``.

    The final file is ``<base>/thinkweave/config.toml``. This is the tier
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
    return base / "thinkweave" / "config.toml"


def user_cache_dir() -> Path:
    """Platform cache base for thinkweave runtime artifacts (cron/Task
    Scheduler logs).

    Resolution order mirrors :func:`user_config_path`:
    1. ``$XDG_CACHE_HOME`` when set.
    2. Windows: ``%LOCALAPPDATA%`` (e.g. ``C:\\Users\\x\\AppData\\Local``).
    3. Otherwise: ``~/.cache``.

    Returns ``<base>/thinkweave`` (underscore form, matching the historic
    ``~/.cache/thinkweave`` layout the example crontab logs into).
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        base = Path(xdg)
    elif _is_windows() and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    else:
        base = Path.home() / ".cache"
    return base / "thinkweave"


def write_user_config(vault_root: Path) -> None:
    """Atomically persist ``vault_root`` to the user-scope config file.

    Creates parent dirs as needed. Mirrors the tempfile + ``os.replace``
    pattern from ``surfaces/cli/install.py:_atomic_write_json`` so an
    interrupted write never leaves a half-written TOML behind.

    The file shape is intentionally minimal — one key — because the
    vault-internal ``config.toml`` (tier 3, at ``vault/config/config.toml``)
    remains the home for embedding / edge / dream fields. This tier only
    ever sets the path.
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
    Phase-3.1 moved this file from ``vault/.weave/sources.yaml`` to
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
    every ``weave`` invocation.
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
    1. ``THINKWEAVE_VAULT`` env var
    2. ``~/.config/thinkweave/config.toml`` (or ``$XDG_CONFIG_HOME``)
    3. ``vault/config/config.toml`` (fallback: legacy ``vault/.weave/config.toml``)
    4. Built-in default (``~/vault``)
    """
    cfg = Config()

    # Tier 1: env var (highest priority — preserves override-everything)
    # PERSONAL_MEM_VAULT is the pre-rename name, honoured as a migration
    # fallback (rename → thinkweave, 2026-06-13); drop once shells are updated.
    vault_env = os.environ.get("THINKWEAVE_VAULT") or os.environ.get("PERSONAL_MEM_VAULT")
    # Tier 2: user-scope TOML (~/.config/thinkweave/config.toml).
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
        if "weave_dir" in data:
            cfg.weave_dir_override = _resolve_weave_dir_value(
                data["weave_dir"], cfg.vault_root
            )
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
        if "essence_cap" in dream_cfg:
            cfg.dream_essence_cap = int(dream_cfg["essence_cap"])
        if "cosine_threshold" in dream_cfg:
            cfg.dream_cosine_threshold = float(dream_cfg["cosine_threshold"])
        if "drift_cap" in dream_cfg:
            cfg.dream_drift_cap = int(dream_cfg["drift_cap"])
        if "coarsen_threshold" in dream_cfg:
            cfg.dream_coarsen_threshold = float(dream_cfg["coarsen_threshold"])
        if "coarsen_cap" in dream_cfg:
            cfg.dream_coarsen_cap = int(dream_cfg["coarsen_cap"])
        if "coarsen_max_size" in dream_cfg:
            cfg.dream_coarsen_max_size = int(dream_cfg["coarsen_max_size"])
        if "coarsen_apply" in dream_cfg:
            cfg.dream_coarsen_apply = bool(dream_cfg["coarsen_apply"])
        if "seam_link_cap" in dream_cfg:
            cfg.dream_seam_link_cap = int(dream_cfg["seam_link_cap"])
        if "promotion_threshold" in dream_cfg:
            cfg.dream_promotion_threshold = int(dream_cfg["promotion_threshold"])
        if "promotion_cap" in dream_cfg:
            cfg.dream_promotion_cap = int(dream_cfg["promotion_cap"])
        if "probe_window_days" in dream_cfg:
            cfg.dream_probe_window_days = int(dream_cfg["probe_window_days"])
        if "rejudge_cap" in dream_cfg:
            cfg.dream_rejudge_cap = int(dream_cfg["rejudge_cap"])
        if "trajectory_phase2_days" in dream_cfg:
            cfg.dream_trajectory_phase2_days = int(dream_cfg["trajectory_phase2_days"])
        if "trajectory_rework_threshold" in dream_cfg:
            cfg.dream_trajectory_rework_threshold = float(
                dream_cfg["trajectory_rework_threshold"]
            )
        if "trajectory_agent_identities" in dream_cfg:
            raw = dream_cfg["trajectory_agent_identities"]
            if isinstance(raw, str):
                raw = [t.strip() for t in raw.split(",") if t.strip()]
            cfg.dream_trajectory_agent_identities = tuple(raw)
        if "knowledge_delta_hours" in dream_cfg:
            cfg.dream_knowledge_delta_hours = int(
                dream_cfg["knowledge_delta_hours"]
            )
        if "essence_max_catalysts" in dream_cfg:
            cfg.dream_essence_max_catalysts = int(
                dream_cfg["essence_max_catalysts"]
            )
        if "essence_placeholder_max_catalysts" in dream_cfg:
            cfg.dream_essence_placeholder_max_catalysts = int(
                dream_cfg["essence_placeholder_max_catalysts"]
            )

        # Memory seam ([seam])
        seam_cfg = data.get("seam", {})
        if "cosine_twin" in seam_cfg:
            cfg.seam_cosine_twin = float(seam_cfg["cosine_twin"])
        if "cosine_none" in seam_cfg:
            cfg.seam_cosine_none = float(seam_cfg["cosine_none"])
        if "stale_age_days" in seam_cfg:
            cfg.seam_stale_age_days = int(seam_cfg["stale_age_days"])
        if "recheck_days" in seam_cfg:
            cfg.seam_recheck_days = int(seam_cfg["recheck_days"])
        if "cap" in seam_cfg:
            cfg.seam_cap = int(seam_cfg["cap"])

        # Evidence-gated steering ([steering] — issue #62)
        steering_cfg = data.get("steering", {})
        if "weekly_budget" in steering_cfg:
            cfg.steering_weekly_budget = int(steering_cfg["weekly_budget"])
        # Signal weights arrive as ``weight_<signal>`` keys (weight_rework,
        # weight_fix_rounds, weight_superseded, weight_gate_failures,
        # weight_hub_pressure); strip the prefix into the steering_weights map.
        weights: dict = {}
        for key, val in steering_cfg.items():
            if key.startswith("weight_"):
                weights[key[len("weight_"):]] = float(val)
        if weights:
            cfg.steering_weights = weights

        # Extraction policy
        extract_cfg = data.get("extract", {})
        if "insights_cap" in extract_cfg:
            cfg.extract_insights_cap = int(extract_cfg["insights_cap"])

        # Behavioral-salience window for the digest active-focus block.
        salience_cfg = data.get("salience", {})
        if "activity_window_days" in salience_cfg:
            cfg.salience_activity_window_days = int(
                salience_cfg["activity_window_days"]
            )

        # Inline backfill fan-out policy (/seed-enrich)
        enrich_cfg = data.get("enrich", {})
        if "fanout_threshold" in enrich_cfg:
            cfg.enrich_fanout_threshold = int(enrich_cfg["fanout_threshold"])
        if "batch_size" in enrich_cfg:
            cfg.enrich_batch_size = int(enrich_cfg["batch_size"])
        if "parallelism" in enrich_cfg:
            cfg.enrich_parallelism = int(enrich_cfg["parallelism"])

        # Theme cluster detection
        themes_cfg = data.get("themes", {})
        if "min_cluster_size" in themes_cfg:
            cfg.theme_min_cluster_size = int(themes_cfg["min_cluster_size"])
        if "recent_days" in themes_cfg:
            cfg.theme_recent_days = int(themes_cfg["recent_days"])
        if "min_shared_concepts" in themes_cfg:
            cfg.theme_min_shared_concepts = int(
                themes_cfg["min_shared_concepts"]
            )
        if "name_family_jaccard" in themes_cfg:
            cfg.theme_name_family_jaccard = float(
                themes_cfg["name_family_jaccard"]
            )
        if "generic_concept_ratio" in themes_cfg:
            cfg.theme_generic_concept_ratio = float(
                themes_cfg["generic_concept_ratio"]
            )
        if "resolve_after_days" in themes_cfg:
            cfg.theme_resolve_after_days = int(themes_cfg["resolve_after_days"])

        # Landing docs
        landing_cfg = data.get("landing", {})
        if "open_probes_cap" in landing_cfg:
            cfg.landing_open_probes_cap = int(landing_cfg["open_probes_cap"])
        if "probes_display_cap" in landing_cfg:
            cfg.landing_probes_display_cap = int(
                landing_cfg["probes_display_cap"]
            )

        # Retrieval ([retrieval] top-level keys + [retrieval.prompt_time])
        retrieval_cfg = data.get("retrieval", {})
        if "rrf_k" in retrieval_cfg:
            cfg.retrieval_rrf_k = int(retrieval_cfg["rrf_k"])

        # R2 — prompt-time retrieval enrichment ([retrieval.prompt_time])
        pt = retrieval_cfg.get("prompt_time", {})
        if pt:
            rpt = cfg.retrieval_prompt_time
            if "enabled" in pt:
                rpt.enabled = bool(pt["enabled"])
            if "min_prompt_chars" in pt:
                rpt.min_prompt_chars = int(pt["min_prompt_chars"])
            if "embed_deadline_seconds" in pt:
                rpt.embed_deadline_seconds = float(pt["embed_deadline_seconds"])
            if "deadline_miss_limit" in pt:
                rpt.deadline_miss_limit = int(pt["deadline_miss_limit"])
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

    # Per-field env overrides. PERSONAL_MEM_* are the pre-rename names,
    # honoured as migration fallbacks (rename → thinkweave, 2026-06-13).
    project_env = os.environ.get("THINKWEAVE_PROJECT") or os.environ.get("PERSONAL_MEM_PROJECT")
    if project_env:
        cfg.default_project = project_env
    db_env = os.environ.get("THINKWEAVE_DB") or os.environ.get("PERSONAL_MEM_DB")
    if db_env:
        # Override index db path directly
        cfg._index_db_override = Path(db_env)
    weave_dir_env = os.environ.get("THINKWEAVE_WEAVE_DIR")
    if weave_dir_env:
        cfg.weave_dir_override = _resolve_weave_dir_value(weave_dir_env, cfg.vault_root)

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
