"""Evidence-gated steering + weekly budget for slow-loop proposals (issue #62).

The slow self-improvement loop (#61 — the weekly Routine that RUNS
improve-arch / ponytail-audit and files issues) must not invent work: every
proposal it files has to cite evidence from the self-improvement substrate, and
only the top-``weekly_budget`` by evidence weight survive per run. This module
is the gate #61 calls — candidates in, ``{filed, dropped}`` out; #61 files ONLY
what the gate returns.

Design mirrors #60's ``operations/trajectory_outcome`` split:

- **Pure logic** (unit-tested, no I/O): the per-signal aggregators
  (:func:`aggregate_rework`, :func:`aggregate_gate_failures`,
  :func:`aggregate_superseded`, :func:`hub_pressure_from_ranks`),
  :func:`evidence_for` / :func:`has_evidence`, and :func:`gate_proposals`.
  Each aggregator is a total function over *already-queried rows*, so tests feed
  hand-built fixtures and the expecteds are hand-computed.
- **The index seam** (:func:`build_evidence_index`) is the only surface that
  touches the derived SQLite index / vault — read-only, one pass, mirroring
  ``trajectory_outcome._candidate_trajectories``. It builds an
  :class:`EvidenceIndex` snapshot the pure layer consumes.
- **The CLI** (``weave steering evidence`` / ``weave steering gate``) is the
  thin surface #61's Routine invokes.

The four signals (all raw counts — never a composite score; the weights are for
ranking, the raw counts ride the evidence block):

1. **rework rate** per module path — loop-run trajectory notes' ``outcome_label``
   in {reworked, reworked-post-merge} (#60's phase-1/2 verdicts) counted over
   ``files_touched``, plus summed ``fix_rounds`` (churn) over the same files.
2. **superseded/contested decision density** — decisions whose status is
   superseded/deprecated OR that declare a supersedes/superseded_by link,
   counted per ``file_paths``.
3. **gate-failure hotspots** — trajectory ``gates[]`` entries with
   ``passed=false`` counted over ``files_touched``.
4. **behavioral pressure** — concept-hub centrality (per-concept PageRank from
   the ``graph_ranks`` table). **Optional / zero-default:** PageRank is only
   populated by the dream apply phase when ``dream.compute_pagerank`` is on, so
   on a vault that has not dreamed this signal is uniformly ``0`` and simply
   contributes nothing to the weight (the other three signals still gate). A
   candidate carries the concepts it touches; its hub pressure is the sum of
   those concepts' PageRank.

Config knobs live in the ``[steering]`` section of ``config.toml`` (see
``core/config.py``): ``weekly_budget`` (default 3) and the five signal weights.
Nothing here is hardcoded posture — the numbers are all knobs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from thinkweave.core.config import Config

# --- verdict / status vocabularies -----------------------------------------
# #60's reworked verdicts: phase-1 ``reworked`` (human commit before merge) and
# phase-2 ``reworked-post-merge`` (merged diff substantially rewritten within
# the window). Both mark a module the loop had to redo.
REWORKED_LABELS = frozenset({"reworked", "reworked-post-merge"})
# A decision is "contested" if the judge flipped it or it sits in a supersession
# chain — either endpoint counts toward density.
CONTESTED_STATUSES = frozenset({"superseded", "deprecated"})

# --- defaults (every one is a [steering] config knob) ----------------------
DEFAULT_WEEKLY_BUDGET = 3
DEFAULT_WEIGHTS: dict[str, float] = {
    "rework": 1.0,
    "fix_rounds": 1.0,
    "superseded": 1.0,
    "gate_failures": 1.0,
    "hub_pressure": 1.0,
}


# ---------------------------------------------------------------------------
# Evidence snapshot
# ---------------------------------------------------------------------------


@dataclass
class EvidenceIndex:
    """A read-only snapshot of the self-improvement substrate's signals.

    Four file-keyed count maps + one concept-keyed pressure map. Built once from
    the index by :func:`build_evidence_index`; the pure :func:`evidence_for`
    reads it per candidate. Every map defaults empty so a fresh vault (no
    trajectories, no dreamed PageRank) yields all-zero evidence — which the gate
    correctly reads as "no evidence, drop".
    """

    rework: dict[str, int] = field(default_factory=dict)
    fix_rounds: dict[str, int] = field(default_factory=dict)
    superseded: dict[str, int] = field(default_factory=dict)
    gate_failures: dict[str, int] = field(default_factory=dict)
    hub_pressure: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure aggregators — over already-queried rows
# ---------------------------------------------------------------------------


def _as_str_list(raw: Any) -> list[str]:
    """Coerce a frontmatter list-or-scalar field to a clean list of strings."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def aggregate_rework(trajectory_rows: Iterable[dict]) -> tuple[dict[str, int], dict[str, int]]:
    """``(rework_by_file, fix_rounds_by_file)`` from loop-run trajectory rows.

    Each row is ``{outcome_label, fix_rounds, files_touched}`` (the #60 judged
    frontmatter). A trajectory contributes ``+1`` rework to every file it
    touched iff its ``outcome_label`` is a reworked verdict; its ``fix_rounds``
    are summed over touched files regardless of verdict (churn is churn even on
    a clean merge). Pure.
    """
    rework: dict[str, int] = {}
    fix: dict[str, int] = {}
    for row in trajectory_rows:
        files = _as_str_list(row.get("files_touched"))
        if not files:
            continue
        is_reworked = str(row.get("outcome_label") or "") in REWORKED_LABELS
        try:
            fr = int(row.get("fix_rounds") or 0)
        except (TypeError, ValueError):
            fr = 0
        for f in files:
            if is_reworked:
                rework[f] = rework.get(f, 0) + 1
            if fr:
                fix[f] = fix.get(f, 0) + fr
    return rework, fix


def aggregate_gate_failures(trajectory_rows: Iterable[dict]) -> dict[str, int]:
    """``gate_failures_by_file`` — count of ``passed=false`` gate entries per file.

    Each row is ``{gates: [{id, passed, summary}], files_touched}``. A run's
    failed-gate count is attributed to every file it touched (the failing gate
    ran over the whole diff). Pure.
    """
    out: dict[str, int] = {}
    for row in trajectory_rows:
        gates = row.get("gates") or []
        if not isinstance(gates, (list, tuple)):
            continue
        n_failed = sum(1 for g in gates if isinstance(g, dict) and g.get("passed") is False)
        if not n_failed:
            continue
        for f in _as_str_list(row.get("files_touched")):
            out[f] = out.get(f, 0) + n_failed
    return out


def _is_contested(row: dict) -> bool:
    if str(row.get("status") or "") in CONTESTED_STATUSES:
        return True
    return bool(_as_str_list(row.get("supersedes"))) or bool(_as_str_list(row.get("superseded_by")))


def aggregate_superseded(decision_rows: Iterable[dict]) -> dict[str, int]:
    """``superseded_by_file`` — count of contested decisions touching each file.

    Each row is ``{status, supersedes, superseded_by, file_paths}``. A decision
    counts once per file it touched iff it is contested (:func:`_is_contested` —
    superseded/deprecated status, or either end of a supersession link). Pure.
    """
    out: dict[str, int] = {}
    for row in decision_rows:
        if not _is_contested(row):
            continue
        for f in _as_str_list(row.get("file_paths")):
            out[f] = out.get(f, 0) + 1
    return out


def hub_pressure_from_ranks(rank_rows: Iterable[tuple[str, float]]) -> dict[str, float]:
    """``{concept: max_score}`` from ``graph_ranks`` ``(rank_type, score)`` rows.

    Only ``pagerank:{concept}`` rows contribute; the per-concept scalar is the
    MAX score across that concept's induced subgraph (its most-central note).
    Empty in → empty out — the zero-default that makes this signal optional on a
    vault that has not computed PageRank. Pure.
    """
    out: dict[str, float] = {}
    for rank_type, score in rank_rows:
        rt = str(rank_type or "")
        if not rt.startswith("pagerank:"):
            continue
        concept = rt.split(":", 1)[1]
        try:
            s = float(score)
        except (TypeError, ValueError):
            continue
        if concept not in out or s > out[concept]:
            out[concept] = s
    return out


# ---------------------------------------------------------------------------
# Per-candidate evidence assembly
# ---------------------------------------------------------------------------


def candidate_paths(candidate: dict) -> list[str]:
    """The repo paths a candidate targets — ``paths`` list, else the ``module`` scalar."""
    paths = candidate.get("paths")
    if paths is None:
        module = candidate.get("module")
        paths = [module] if module else []
    return _as_str_list(paths)


def candidate_concepts(candidate: dict) -> list[str]:
    """The domain concepts a candidate cites (drives the hub-pressure signal)."""
    return _as_str_list(candidate.get("concepts"))


def _path_covers(file_key: str, target: str) -> bool:
    """True if a candidate ``target`` covers a signal's ``file_key``.

    Exact match, or ``target`` is a directory prefix of ``file_key`` at a path
    boundary — ``src/ops/`` covers ``src/ops/dream.py`` but ``a/b.py`` does NOT
    swallow ``a/bc.py``. A target ending in ``/`` is an explicit directory; any
    other target matches a child only across a ``/`` boundary.
    """
    if file_key == target:
        return True
    if target.endswith("/"):
        return file_key.startswith(target)
    return file_key.startswith(target + "/")


def _sum_matching(mapping: dict[str, Any], paths: list[str]) -> Any:
    total = 0
    for file_key, value in mapping.items():
        if any(_path_covers(file_key, p) for p in paths):
            total += value
    return total


def evidence_for(index: EvidenceIndex, candidate: dict, weights: Optional[dict] = None) -> dict:
    """Assemble a candidate's machine-readable evidence block. Pure.

    Sums the file-keyed signals over the candidate's paths (prefix-aware) and
    the concept-keyed hub pressure over its concepts, then computes the weighted
    ``weight``. Raw counts are always preserved on the block (per #60 — the
    downstream ranker normalizes, this gate does not).
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    paths = candidate_paths(candidate)
    concepts = candidate_concepts(candidate)

    rework = _sum_matching(index.rework, paths)
    fix_rounds = _sum_matching(index.fix_rounds, paths)
    superseded = _sum_matching(index.superseded, paths)
    gate_failures = _sum_matching(index.gate_failures, paths)
    hub = round(sum(index.hub_pressure.get(c, 0.0) for c in concepts), 4)

    weight = round(
        w["rework"] * rework
        + w["fix_rounds"] * fix_rounds
        + w["superseded"] * superseded
        + w["gate_failures"] * gate_failures
        + w["hub_pressure"] * hub,
        4,
    )
    return {
        "module": paths[0] if paths else "",
        "paths": paths,
        "rework_count": rework,
        "fix_rounds": fix_rounds,
        "superseded_decisions": superseded,
        "gate_failures": gate_failures,
        "hub_pressure": hub,
        "weight": weight,
    }


def has_evidence(block: dict) -> bool:
    """True iff any raw signal is nonzero.

    Admission is about *evidence presence*, deliberately independent of the
    weights (a signal weighted 0 but with a nonzero raw count is still real
    evidence). Ranking is what the weights drive.
    """
    return any(
        block.get(k, 0) > 0
        for k in ("rework_count", "fix_rounds", "superseded_decisions", "gate_failures", "hub_pressure")
    )


# ---------------------------------------------------------------------------
# Evidence-block rendering (the machine-readable block filed proposals carry)
# ---------------------------------------------------------------------------


_BLOCK_KEYS = ("module", "rework_count", "fix_rounds", "superseded_decisions", "gate_failures", "hub_pressure", "weight")


def render_evidence_block(block: dict) -> str:
    """A fenced ```json evidence block for a filed proposal's body.

    Machine-readable (parses straight back to the raw counts) so a reader — or a
    later learner — can recover exactly which signals justified the proposal.
    """
    payload = {k: block.get(k) for k in _BLOCK_KEYS}
    return "```json\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n```"


def _proposal_body(candidate: dict, block: dict) -> str:
    rationale = str(candidate.get("rationale") or "").strip()
    parts: list[str] = []
    if rationale:
        parts.append(rationale)
    parts.append("## Evidence\n" + render_evidence_block(block))
    return "\n\n".join(parts)


def _candidate_ref(candidate: dict) -> dict:
    """The identity fields echoed on every filed/dropped entry."""
    ref: dict[str, Any] = {
        "module": (candidate_paths(candidate) or [""])[0],
        "paths": candidate_paths(candidate),
        "rationale": str(candidate.get("rationale") or ""),
    }
    if candidate.get("title"):
        ref["title"] = candidate["title"]
    return ref


# ---------------------------------------------------------------------------
# The gate — candidates in, {filed, dropped} out
# ---------------------------------------------------------------------------


def gate_proposals(
    candidates: Iterable[dict],
    index: EvidenceIndex,
    cfg: Optional[Config] = None,
    *,
    weekly_budget: Optional[int] = None,
    weights: Optional[dict] = None,
) -> dict:
    """Gate a batch of candidate proposals against the evidence substrate.

    ``candidates`` are ``{module|paths, rationale, concepts?}`` dicts. Returns
    ``{filed: [...], dropped: [...]}``:

    - a candidate with **no nonzero evidence signal** is dropped
      (``reason='no cited evidence'``) — this is the anti-invention gate;
    - survivors are ranked by evidence ``weight`` (desc, stable on ties) and
      capped at ``weekly_budget``; the overflow is dropped
      (``reason='exceeded weekly budget'``);
    - each filed entry gains a ``body`` embedding the machine-readable evidence
      block and an ``evidence`` dict of the raw counts + weight.

    ``weekly_budget`` / ``weights`` default from ``cfg`` (the ``[steering]``
    knobs) when not passed explicitly — the CLI passes ``cfg`` only.
    """
    budget = weekly_budget if weekly_budget is not None else _cfg_budget(cfg)
    resolved_weights = weights if weights is not None else _cfg_weights(cfg)

    scored: list[tuple[float, dict, dict]] = []
    dropped: list[dict] = []
    for cand in candidates:
        block = evidence_for(index, cand, resolved_weights)
        if not has_evidence(block):
            dropped.append({**_candidate_ref(cand), "reason": "no cited evidence", "evidence": block})
            continue
        scored.append((block["weight"], cand, block))

    # Stable sort: Python's sort preserves input order for equal keys, so ties
    # keep the caller's candidate order (deterministic, no coin-flip).
    scored.sort(key=lambda t: t[0], reverse=True)

    filed: list[dict] = []
    for rank, (weight, cand, block) in enumerate(scored):
        if rank < budget:
            filed.append(
                {
                    **_candidate_ref(cand),
                    "evidence": block,
                    "weight": weight,
                    "body": _proposal_body(cand, block),
                }
            )
        else:
            dropped.append(
                {**_candidate_ref(cand), "reason": "exceeded weekly budget", "evidence": block, "weight": weight}
            )
    return {"filed": filed, "dropped": dropped}


# ---------------------------------------------------------------------------
# Config knob resolution
# ---------------------------------------------------------------------------


def _cfg_budget(cfg: Optional[Config]) -> int:
    if cfg is None:
        return DEFAULT_WEEKLY_BUDGET
    return int(getattr(cfg, "steering_weekly_budget", DEFAULT_WEEKLY_BUDGET) or DEFAULT_WEEKLY_BUDGET)


def _cfg_weights(cfg: Optional[Config]) -> dict[str, float]:
    if cfg is None:
        return dict(DEFAULT_WEIGHTS)
    raw = getattr(cfg, "steering_weights", None)
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    return {**DEFAULT_WEIGHTS, **raw}


# ---------------------------------------------------------------------------
# The index seam — the ONLY surface that reads the derived index / vault
# ---------------------------------------------------------------------------


def _row_get(row: Any, key: str, pos: int) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        try:
            return row[pos]
        except (KeyError, IndexError, TypeError):
            return None


def _trajectory_rows(cfg: Config) -> list[dict]:
    """``{outcome_label, fix_rounds, files_touched, gates}`` per loop-run note.

    Index-driven candidate discovery (``note_tags`` join), then a read of each
    note's frontmatter via the vault — mirroring
    ``trajectory_outcome.scan_trajectory_outcomes``. Never a filesystem crawl.
    """
    from thinkweave.core.indexer import Indexer
    from thinkweave.core.vault import VaultManager

    idx = Indexer(config=cfg)
    try:
        rows = idx.db.execute(
            """
            SELECT DISTINCT n.id AS id, n.path AS path
              FROM notes n
              JOIN note_tags t ON t.note_id = n.id
             WHERE n.type = 'note' AND t.tag = 'loop-run'
             ORDER BY n.id
            """
        ).fetchall()
    finally:
        idx.close()

    vm = VaultManager(config=cfg)
    out: list[dict] = []
    for r in rows:
        rel = _row_get(r, "path", 1)
        if not rel:
            continue
        try:
            note = vm.read_note(vm.root / rel)
        except Exception:
            continue
        fm = note.frontmatter
        out.append(
            {
                "outcome_label": fm.get("outcome_label") or "",
                "fix_rounds": fm.get("fix_rounds") or 0,
                "files_touched": fm.get("files_touched") or [],
                "gates": fm.get("gates") or [],
            }
        )
    return out


def _decision_rows(cfg: Config) -> list[dict]:
    """``{status, supersedes, superseded_by, file_paths}`` per decision note."""
    from thinkweave.core.indexer import Indexer
    from thinkweave.core.vault import VaultManager

    idx = Indexer(config=cfg)
    try:
        rows = idx.db.execute(
            "SELECT id AS id, path AS path FROM notes WHERE type = 'decision' ORDER BY id"
        ).fetchall()
    finally:
        idx.close()

    vm = VaultManager(config=cfg)
    out: list[dict] = []
    for r in rows:
        rel = _row_get(r, "path", 1)
        if not rel:
            continue
        try:
            note = vm.read_note(vm.root / rel)
        except Exception:
            continue
        fm = note.frontmatter
        out.append(
            {
                "status": fm.get("status") or "",
                "supersedes": fm.get("supersedes") or [],
                "superseded_by": fm.get("superseded_by") or [],
                "file_paths": fm.get("file_paths") or [],
            }
        )
    return out


def _rank_rows(cfg: Config) -> list[tuple[str, float]]:
    """``(rank_type, max_score)`` for every ``pagerank:*`` concept subgraph.

    One grouped query over ``graph_ranks``. Empty when the dream apply phase has
    not computed PageRank (``dream.compute_pagerank`` off / never dreamed) — the
    zero-default that makes behavioral pressure an optional signal.
    """
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    try:
        rows = idx.db.execute(
            """
            SELECT rank_type AS rank_type, MAX(score) AS score
              FROM graph_ranks
             WHERE rank_type LIKE 'pagerank:%'
             GROUP BY rank_type
            """
        ).fetchall()
    finally:
        idx.close()
    return [(_row_get(r, "rank_type", 0), _row_get(r, "score", 1)) for r in rows]


def build_evidence_index(cfg: Config) -> EvidenceIndex:
    """Assemble the :class:`EvidenceIndex` from the derived index (read-only)."""
    rework, fix_rounds = aggregate_rework(_trajectory_rows(cfg))
    return EvidenceIndex(
        rework=rework,
        fix_rounds=fix_rounds,
        superseded=aggregate_superseded(_decision_rows(cfg)),
        gate_failures=aggregate_gate_failures(_trajectory_rows(cfg)),
        hub_pressure=hub_pressure_from_ranks(_rank_rows(cfg)),
    )


def evidence_signals(cfg: Config, *, module: str = "") -> dict:
    """Read-only view of the computed signals, optionally filtered to a module.

    Powers ``weave steering evidence``. Returns ``{modules: [...]}`` — one entry
    per file carrying any signal (or, with ``module``, the single aggregated
    evidence block for that path prefix). Each per-file entry is the same shape
    as an evidence block so the CLI and #61 read one schema.
    """
    index = build_evidence_index(cfg)
    if module:
        block = evidence_for(index, {"module": module})
        return {"module": module, "evidence": block}

    files = set(index.rework) | set(index.fix_rounds) | set(index.superseded) | set(index.gate_failures)
    modules = [evidence_for(index, {"module": f}) for f in sorted(files)]
    modules.sort(key=lambda b: b["weight"], reverse=True)
    return {"modules": modules, "hub_pressure": dict(sorted(index.hub_pressure.items()))}
