"""Periodic vault-hygiene cycle — the deterministic backbone of ``/dream``.

``/dream`` is the cron-friendly successor to ``/mem-resolve-concepts`` and
``/themes-resolve``. It runs in three phases:

1. **scan** — read-only sweep. Composes drift candidates, promotion-eligible
   proposed concepts, theme candidate stubs, and dormant/resolved themes
   into a structured action plan. Cheap; ~1s on a 6500-note vault.
2. **LLM judgment** — the ``/dream`` skill applies semantic judgment to the
   scan output (which drift pairs are real, which candidates deserve
   canonicalisation, which themes have stale essences) and emits a plan
   dict. This phase lives in the skill, not here.
3. **apply** — execute the structural changes the LLM decided on with
   **one** index rebuild at the end and append a single line to
   ``vault/.mem/maintenance.jsonl``. This is the speed win — the existing
   ``mem concepts promote`` / ``mem themes promote-candidate`` paths each
   rebuild the index per call, which would be 20× full rebuilds at the
   per-cycle cap.

The architecture mirrors ``operations/wrap.py`` (the ``mem wrap-finalize``
backbone): structured-result dataclasses, per-step timings, errors recorded
but never cascade. Pure orchestration over existing ``synthesis/`` helpers;
imports ``core/`` / ``operations/`` / ``synthesis/`` only, never
``surfaces/``.

The maintenance.jsonl entry is the operations-log artifact that makes
autonomy trustable — every cron-fired cycle leaves one line saying what it
did, so a human can grep the last 30 days and verify the cycle hasn't gone
sideways.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.core.config import Config


# ---------------------------------------------------------------------------
# Maintenance log
# ---------------------------------------------------------------------------

MAINTENANCE_LOG_RELPATH = Path(".mem") / "maintenance.jsonl"


def maintenance_log_path(cfg: Config) -> Path:
    """Append-only operations log for the dream cycle.

    One JSON line per cycle invocation. Stable schema is the
    :meth:`DreamCycleResult.log_entry` output. Cron jobs reading this file
    should rely only on documented keys.
    """
    return cfg.vault_root / MAINTENANCE_LOG_RELPATH


def append_maintenance_log(cfg: Config, entry: dict) -> Path:
    """Append one JSON line to ``vault/.mem/maintenance.jsonl``.

    Creates the parent directory and file if missing. Returns the path of
    the log file (which the caller may surface to the user).
    """
    path = maintenance_log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return path


def _new_cycle_id() -> str:
    """Generate a cycle ID of the form ``dream-YYYYMMDD-HHMMSS-<hex6>``.

    Stable enough to grep the log by date; unique enough to disambiguate
    multiple cycles in the same second (rare, but plausible under
    test-suite parallelism).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"dream-{ts}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------------------
# Scan phase — read-only action plan
# ---------------------------------------------------------------------------


@dataclass
class DreamCycleScan:
    """Read-only action plan emitted by the scan phase.

    Consumed by the LLM judgment phase of ``/dream``, which constructs an
    ``apply`` plan from the survivors. Stable JSON schema — agents read it.
    """

    cycle_id: str
    project: str = ""
    promotion_cap: int = 20
    stats: dict = field(default_factory=dict)
    drift_pairs: list = field(default_factory=list)
    promotion_candidates: list = field(default_factory=list)
    theme_candidates: list = field(default_factory=list)
    # Raw cluster signals — clusters of event-grain sources sharing
    # concepts that aren't covered by any active theme and don't have a
    # candidate stub on disk. The dream apply phase composes a real slug
    # + essence from these (LLM naming step lives in the prompt, not in
    # the SDK), then mints canonical themes directly via the new
    # `theme_promotions_from_signal` plan key.
    theme_cluster_signals: list = field(default_factory=list)
    dormant_themes: list = field(default_factory=list)
    resolved_themes: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def scan(
    cfg: Config,
    *,
    project: str = "",
    promotion_cap: int = 20,
    promotion_threshold: int = 5,
) -> DreamCycleScan:
    """Compose a read-only action plan from five vault-global scans.

    1. **drift pairs** — ``operations.concepts.drift`` filtered through
       ``filter_drift_candidates`` (drops the substring/short-name noise
       documented in [[feedback_dont_trust_drift_blindly]]).
    2. **promotion candidates** — ``proposed_concepts`` at ``count ≥
       promotion_threshold`` (default 5), filtered through
       ``filter_promotion_candidates`` (drops domain-paths, generic
       process terms, underscore-bearing leakage), sorted by count desc,
       capped at ``promotion_cap``.
    3. **theme candidates** — stubs in ``vault/themes/_candidates/`` with
       their cluster frontmatter so the LLM can apply the disambiguation
       test (capability vs narrative arc).
    4. **dormant themes** — ``find_dormant_themes`` (90-day rule;
       deterministic).
    5. **resolved themes** — ``find_resolved_themes`` (all linked
       decisions in terminal status; deterministic).

    Steps are wrapped: a failure in one is recorded in ``errors``; the
    rest still run. Returns :class:`DreamCycleScan`.
    """
    result = DreamCycleScan(
        cycle_id=_new_cycle_id(),
        project=project,
        promotion_cap=promotion_cap,
    )

    # 1. drift pairs ------------------------------------------------------
    _t = time.perf_counter()
    try:
        from personal_mem.operations.concepts import drift as concept_drift
        from personal_mem.synthesis.concepts import filter_drift_candidates

        d = concept_drift(cfg, project=project)
        # drift_report's near_duplicates is already the tuple shape
        # filter_drift_candidates wants: [(a, b, reason), ...].
        # Convert defensively in case the producer ever switches to dicts.
        raw_pairs = (d.get("report") or {}).get("near_duplicates", []) or []
        tuples: list[tuple[str, str, str]] = []
        for p in raw_pairs:
            if isinstance(p, dict):
                tuples.append(
                    (p.get("from", ""), p.get("to", ""), p.get("reason", ""))
                )
            elif isinstance(p, (tuple, list)) and len(p) >= 2:
                tuples.append(
                    (p[0], p[1], p[2] if len(p) > 2 else "")
                )
        surviving = filter_drift_candidates(tuples)
        result.drift_pairs = [
            {"from": a, "to": b, "reason": r} for a, b, r in surviving
        ]
    except Exception as e:  # noqa: BLE001 — best-effort scan step
        result.errors.append(f"drift: {e}")
    finally:
        result.timings["drift"] = time.perf_counter() - _t

    # 2. promotion candidates ---------------------------------------------
    _t = time.perf_counter()
    try:
        from personal_mem.core.indexer import Indexer
        from personal_mem.synthesis.concepts import (
            filter_promotion_candidates,
            get_all_proposed_concepts,
        )

        idx = Indexer(config=cfg)
        try:
            proposed = get_all_proposed_concepts(idx.db)
        finally:
            idx.close()

        eligible = [c for c, n in proposed.items() if n >= promotion_threshold]
        survivors = filter_promotion_candidates(eligible)
        ranked = sorted(
            (
                {"concept": c, "count": proposed[c]}
                for c in survivors
            ),
            key=lambda r: (-r["count"], r["concept"]),
        )[:promotion_cap]
        result.promotion_candidates = ranked
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"promotion: {e}")
    finally:
        result.timings["promotion"] = time.perf_counter() - _t

    # 3. theme candidates -------------------------------------------------
    _t = time.perf_counter()
    try:
        from personal_mem.core.vault import parse_frontmatter

        cand_dir = cfg.vault_root / "themes" / "_candidates"
        if cand_dir.exists():
            for path in sorted(cand_dir.glob("cand-*.md")):
                try:
                    text = path.read_text(encoding="utf-8")
                    fm, _body = parse_frontmatter(text)
                except Exception:  # noqa: BLE001 — skip unreadable stubs
                    continue
                # Filename shape: `cand-XXXXXXXX-<slug>.md`. The candidate
                # id is the first two dash-segments.
                parts = path.stem.split("-", 2)
                cand_id = "-".join(parts[:2]) if len(parts) >= 2 else path.stem
                result.theme_candidates.append(
                    {
                        "candidate_id": cand_id,
                        "path": str(path.relative_to(cfg.vault_root)),
                        "title": fm.get("title", path.stem),
                        "cluster_size": fm.get("cluster_size"),
                        "cluster_concepts": fm.get("cluster_concepts") or [],
                        "cluster_sources": fm.get("cluster_sources") or [],
                        "candidacy": fm.get("candidacy", ""),
                        "source_type": fm.get("source_type", ""),
                    }
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_candidates: {e}")
    finally:
        result.timings["theme_candidates"] = time.perf_counter() - _t

    # 4. dormant themes ---------------------------------------------------
    _t = time.perf_counter()
    try:
        from personal_mem.core.vault import parse_frontmatter
        from personal_mem.synthesis.theme_candidates import find_dormant_themes

        for path, last in find_dormant_themes(cfg):
            try:
                fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                fm = {}
            result.dormant_themes.append(
                {
                    "theme_id": fm.get("id", ""),
                    "path": str(path.relative_to(cfg.vault_root)),
                    "title": fm.get("title", path.stem),
                    "last_catalyst": last.isoformat() if last else None,
                }
            )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"dormant: {e}")
    finally:
        result.timings["dormant"] = time.perf_counter() - _t

    # 5. resolved themes --------------------------------------------------
    _t = time.perf_counter()
    try:
        from personal_mem.core.vault import parse_frontmatter
        from personal_mem.synthesis.theme_candidates import find_resolved_themes

        for path, dec_ids in find_resolved_themes(cfg):
            try:
                fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                fm = {}
            result.resolved_themes.append(
                {
                    "theme_id": fm.get("id", ""),
                    "path": str(path.relative_to(cfg.vault_root)),
                    "title": fm.get("title", path.stem),
                    "linked_decisions": dec_ids,
                }
            )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"resolved: {e}")
    finally:
        result.timings["resolved"] = time.perf_counter() - _t

    # 6. theme cluster signals -------------------------------------------
    # Raw clusters of recent event-grain sources sharing concepts that
    # aren't already covered by an active theme or represented by an
    # existing candidate stub. These are the "fresh, name-able" clusters
    # the LLM judgment phase composes slugs for.
    _t = time.perf_counter()
    try:
        from personal_mem.synthesis.theme_candidates import detect_signals

        for sig in detect_signals(cfg):
            result.theme_cluster_signals.append(
                {
                    "source_type": sig.source_type,
                    "shared_concepts": sig.shared_concepts,
                    "cluster_source_ids": sig.cluster_source_ids,
                    "cluster_source_titles": sig.cluster_source_titles,
                    # Per-source proposed_theme votes — set when workers
                    # stamped proposed_theme: <slug> at write time (the
                    # structural analog of proposed_concepts: on the theme
                    # side). voted_slug is the top vote-getter for this
                    # cluster; None when no votes exist. /dream should
                    # prefer voted_slug over composing a fresh slug.
                    "voted_slug": sig.voted_slug,
                    "slug_votes": sig.slug_votes,
                }
            )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_cluster_signals: {e}")
    finally:
        result.timings["theme_cluster_signals"] = time.perf_counter() - _t

    result.stats = {
        "drift_pairs": len(result.drift_pairs),
        "promotion_candidates": len(result.promotion_candidates),
        "theme_candidates": len(result.theme_candidates),
        "theme_cluster_signals": len(result.theme_cluster_signals),
        "dormant_themes": len(result.dormant_themes),
        "resolved_themes": len(result.resolved_themes),
    }

    return result


# ---------------------------------------------------------------------------
# Apply phase — execute LLM-judged plan
# ---------------------------------------------------------------------------


@dataclass
class DreamCycleResult:
    """Structured outcome of :func:`apply`. Mirrors WrapFinalizeResult.

    ``ontology_grew`` is the gating signal for hub regeneration: True iff
    at least one promotion in this cycle added a *new* term to the
    ontology (vs. an idempotent sweep of a term already canonical). The
    apply phase reads this flag to decide whether to run the
    hub-skeleton/domain-hub regen chain — those are O(canonical_concepts)
    and were the dominant cost on the 2026-05-23 first-cycle (455s of
    the 481s total). Steady-state cycles, where every promotion is a
    sweep, skip the chain entirely.
    """

    cycle_id: str
    project: str = ""
    merges_applied: int = 0
    promotions_applied: int = 0
    candidates_promoted: int = 0
    candidates_archived: int = 0
    theme_status_changes: int = 0
    essence_rewrites_logged: int = 0  # body edits done by the skill, logged here
    ontology_grew: bool = False
    indexed: int = 0
    removed: int = 0
    edges: int = 0
    timings: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    log_path: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def log_entry(self, plan: dict) -> dict:
        """Build the maintenance.jsonl line for this cycle.

        Captures intent (the plan) alongside outcome (counts + errors).
        Lets a human grep the log later and answer "what did the cycle
        do on day X, and was anything left unfinished?".
        """
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "cycle_id": self.cycle_id,
            "project": self.project,
            "summary": {
                "merges": self.merges_applied,
                "promotions": self.promotions_applied,
                "candidates_promoted": self.candidates_promoted,
                "candidates_archived": self.candidates_archived,
                "theme_status_changes": self.theme_status_changes,
                "essence_rewrites": self.essence_rewrites_logged,
                "ontology_grew": self.ontology_grew,
            },
            "index": {
                "indexed": self.indexed,
                "removed": self.removed,
                "edges": self.edges,
            },
            "plan": plan,
            "errors": list(self.errors),
            "timings": dict(self.timings),
        }


def apply(
    cfg: Config,
    *,
    plan: dict,
    project: str = "",
    cycle_id: str | None = None,
) -> DreamCycleResult:
    """Execute the LLM-decided structural changes for one dream cycle.

    Plan shape (all keys optional; missing/empty = no-op for that surface)::

        {
          "merges": [
            {"from": "fastapi", "to": "api", "reason": "subset of api"},
            ...
          ],
          "promotions": [
            {"concept": "diagnostics", "domain": "swe", "reason": "..."},
            ...
          ],
          "theme_promotions": [
            {"candidate_id": "cand-abcd1234", "title": "ai-capex",
             "essence": "...", "parent": "thm-X" (optional), "project": ""},
            ...
          ],
          "theme_promotions_from_signal": [
            {"slug": "iran-war",
             "essence": "1-sentence narrative description.",
             "source_ids": ["src-A", "src-B", "src-C"],
             "concepts": ["geopolitics", "oil"],
             "project": "" (optional), "parent": "thm-X" (optional)},
            ...
          ],
          "candidates_archived": [
            {"candidate_id": "cand-X", "reason": "capability-named"},
            ...
          ],
          "theme_status_changes": [
            {"theme_id": "thm-X", "new_status": "dormant", "reason": "..."},
            ...
          ],
          "essence_rewrites": [
            {"theme_id": "thm-X", "reason": "..."},  # log-only; the skill
                                                     # already Edit'd the file
            ...
          ],
        }

    Order matters: merges → promotions → theme operations → ONE index
    rebuild → maintenance.jsonl append. Each step is wrapped; failure in
    one is recorded in ``errors`` and the rest still run.

    Returns :class:`DreamCycleResult` carrying counts, per-step wall
    times, and the path to the appended maintenance-log line.
    """
    result = DreamCycleResult(
        cycle_id=cycle_id or _new_cycle_id(),
        project=project,
    )

    # 1. merges -----------------------------------------------------------
    # Bypass operations.concepts.merge (which rebuilds per call). Use the
    # synthesis helpers directly and rebuild once at the end (step 4).
    _t = time.perf_counter()
    try:
        from personal_mem.synthesis.concepts import (
            delete_concept_hub,
            load_aliases,
            merge_concept_in_notes,
            save_aliases,
        )

        merges = plan.get("merges") or []
        if merges:
            aliases = load_aliases(cfg)
            for m in merges:
                try:
                    from_c = (m.get("from") or "").lower().strip()
                    to_c = (m.get("to") or "").lower().strip()
                    if not from_c or not to_c or from_c == to_c:
                        continue
                    merge_concept_in_notes(cfg.vault_root, from_c, to_c)
                    existing = aliases.get(to_c, [])
                    if from_c not in existing:
                        existing.append(from_c)
                    if from_c in aliases:
                        for old in aliases.pop(from_c):
                            if old != to_c and old not in existing:
                                existing.append(old)
                    aliases[to_c] = existing
                    delete_concept_hub(cfg, from_c)
                    result.merges_applied += 1
                except Exception as e:  # noqa: BLE001
                    result.errors.append(
                        f"merge {m.get('from', '?')}→{m.get('to', '?')}: {e}"
                    )
            if result.merges_applied:
                save_aliases(cfg, aliases)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"merges: {e}")
    finally:
        result.timings["merges"] = time.perf_counter() - _t

    # 2. proposed-concept promotions --------------------------------------
    # Each promotion walks the vault to shift the term proposed → canonical;
    # we suppress its per-call rebuild and do one full rebuild after.
    # The synthesis helper reports whether the ontology actually grew (vs
    # an idempotent sweep of a term already canonical) — we OR those flags
    # so step 4 can skip hub regen on pure-sweep cycles.
    _t = time.perf_counter()
    try:
        from personal_mem.synthesis.concepts import promote_proposed_concept

        for p in plan.get("promotions") or []:
            try:
                concept = (p.get("concept") or "").lower().strip()
                domain = (p.get("domain") or "").lower().strip()
                if not concept or not domain:
                    result.errors.append(
                        f"promote: missing concept/domain in {p}"
                    )
                    continue
                stats = promote_proposed_concept(
                    cfg, concept, domain=domain, rebuild_index=False
                )
                if stats.get("ontology_updated"):
                    result.ontology_grew = True
                result.promotions_applied += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(
                    f"promote {p.get('concept', '?')}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"promotions: {e}")
    finally:
        result.timings["promotions"] = time.perf_counter() - _t

    # 3a. theme candidate promotions --------------------------------------
    _t = time.perf_counter()
    try:
        from personal_mem.synthesis.theme_candidates import promote_candidate

        for tp in plan.get("theme_promotions") or []:
            try:
                cand_id = tp.get("candidate_id") or ""
                title = tp.get("title") or ""
                if not cand_id or not title:
                    result.errors.append(
                        f"theme_promote: missing candidate_id/title in {tp}"
                    )
                    continue
                promote_candidate(
                    cfg,
                    cand_id,
                    title=title,
                    essence=tp.get("essence") or "",
                    project=tp.get("project") or "",
                    parent=tp.get("parent") or "",
                    rebuild_index=False,
                )
                result.candidates_promoted += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(
                    f"theme_promote {tp.get('candidate_id', '?')}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_promotions: {e}")
    finally:
        result.timings["theme_promotions"] = time.perf_counter() - _t

    # 3c. signal-direct theme mints --------------------------------------
    # Plan items composed by /dream from raw `theme_cluster_signals`, where
    # no `cand-*` stub exists yet. Each item is
    # {slug, essence, source_ids, [concepts], [project], [parent]}.
    _t = time.perf_counter()
    try:
        from personal_mem.synthesis.theme_candidates import mint_theme_from_signal

        for ts in plan.get("theme_promotions_from_signal") or []:
            try:
                slug = ts.get("slug") or ""
                source_ids = ts.get("source_ids") or []
                if not slug or not source_ids:
                    result.errors.append(
                        f"theme_signal: missing slug/source_ids in {ts}"
                    )
                    continue
                mint_theme_from_signal(
                    cfg,
                    slug=slug,
                    essence=ts.get("essence") or "",
                    cluster_source_ids=list(source_ids),
                    cluster_concepts=list(ts.get("concepts") or []),
                    candidacy=ts.get("candidacy") or "inferred-from-signal",
                    project=ts.get("project") or "",
                    parent=ts.get("parent") or "",
                    rebuild_index=False,
                )
                result.candidates_promoted += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(
                    f"theme_signal {ts.get('slug', '?')}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_promotions_from_signal: {e}")
    finally:
        result.timings["theme_promotions_from_signal"] = time.perf_counter() - _t

    # 3b. candidate archivals --------------------------------------------
    _t = time.perf_counter()
    try:
        import shutil

        cand_dir = cfg.vault_root / "themes" / "_candidates"
        archive_dir = cand_dir / "_archive"
        for ca in plan.get("candidates_archived") or []:
            try:
                cand_id = ca.get("candidate_id") or ""
                if not cand_id:
                    continue
                matches = list(cand_dir.glob(f"{cand_id}-*.md"))
                if not matches:
                    result.errors.append(
                        f"archive {cand_id}: no matching stub"
                    )
                    continue
                archive_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(matches[0]), archive_dir / matches[0].name)
                result.candidates_archived += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(
                    f"archive {ca.get('candidate_id', '?')}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"candidates_archived: {e}")
    finally:
        result.timings["candidates_archived"] = time.perf_counter() - _t

    # 3c. theme status changes -------------------------------------------
    # Frontmatter updates only; no body changes here. The deterministic
    # helpers (find_dormant_themes / find_resolved_themes) drive the scan;
    # the LLM confirms; we write status.
    _t = time.perf_counter()
    try:
        from personal_mem.core.vault import parse_frontmatter, render_frontmatter

        for tsc in plan.get("theme_status_changes") or []:
            try:
                theme_id = tsc.get("theme_id") or ""
                new_status = tsc.get("new_status") or ""
                if not theme_id or not new_status:
                    result.errors.append(
                        f"theme_status: missing fields in {tsc}"
                    )
                    continue
                # Locate the theme file by scanning canonical themes/.
                themes_dir = cfg.vault_root / "themes"
                target: Path | None = None
                for path in themes_dir.glob("*.md"):
                    try:
                        fm, _ = parse_frontmatter(
                            path.read_text(encoding="utf-8")
                        )
                    except Exception:  # noqa: BLE001
                        continue
                    if fm.get("id") == theme_id:
                        target = path
                        break
                if target is None:
                    result.errors.append(
                        f"theme_status {theme_id}: not found"
                    )
                    continue
                text = target.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(text)
                fm["status"] = new_status
                target.write_text(
                    render_frontmatter(fm) + "\n" + body, encoding="utf-8"
                )
                result.theme_status_changes += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(
                    f"theme_status {tsc.get('theme_id', '?')}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_status_changes: {e}")
    finally:
        result.timings["theme_status_changes"] = time.perf_counter() - _t

    # 3d. essence rewrites — log-only (the skill already Edit'd files) ----
    result.essence_rewrites_logged = len(plan.get("essence_rewrites") or [])

    # 4. one index rebuild + concept-hub maintenance ----------------------
    _t = time.perf_counter()
    structural_changes = (
        result.merges_applied
        + result.promotions_applied
        + result.candidates_promoted
        + result.candidates_archived
        + result.theme_status_changes
        + result.essence_rewrites_logged
    )
    if structural_changes:
        try:
            from personal_mem.core.indexer import Indexer

            # Gate hub regen on *actual* ontology growth — not just
            # "promotions ran." Idempotent sweeps (where the concept was
            # already canonical) don't need new hub skeletons or domain
            # hubs. On the 2026-05-23 first cycle this chain was 95%+ of
            # apply wall-time on a 6500-note WSL vault; skipping it on
            # sweep-only cycles is the routine speed win.
            #
            # Wikilink materialization (add_hub_wikilinks) is *not* in
            # this chain by design — it's quadratic over notes × concepts
            # and belongs on the dedicated `mem index --materialize-links`
            # path that /update-hubs owns. Dream stays in its lane.
            if result.ontology_grew:
                from personal_mem.synthesis.concepts import (
                    generate_concept_hub_skeletons,
                    generate_domain_hubs,
                    hubs_marker_path,
                    load_ontology,
                )

                try:
                    ontology = load_ontology()
                    generate_domain_hubs(cfg, ontology)
                    generate_concept_hub_skeletons(cfg, ontology)
                    hubs_marker_path(cfg).touch()
                except Exception as e:  # noqa: BLE001
                    result.errors.append(f"hub_skeletons: {e}")

            idx = Indexer(config=cfg)
            try:
                stats = idx.rebuild(full=False)
            finally:
                idx.close()
            result.indexed = stats.get("indexed", 0)
            result.removed = stats.get("removed", 0)
            result.edges = stats.get("edges", 0)
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"index: {e}")
    result.timings["index"] = time.perf_counter() - _t

    # 5. maintenance.jsonl append ----------------------------------------
    _t = time.perf_counter()
    try:
        log_path = append_maintenance_log(cfg, result.log_entry(plan))
        result.log_path = str(log_path)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"log: {e}")
    result.timings["log"] = time.perf_counter() - _t

    return result
