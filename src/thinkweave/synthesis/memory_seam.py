"""Memory seam — reconcile Claude Code auto-memory against the vault.

Two always-on knowledge channels feed every session and are assembled
*independently*:

- **CC auto-memory** (``~/.claude/projects/*/memory/*.md``) — the DURABLE
  layer. Preferences, feedback, hard-won lessons; rarely stale.
- **The vault SessionStart payload** — the FRESH layer. Recent sessions /
  decisions / state, regenerated each session.

The seam is the missing reconciliation between them: an **active correctness
guard** that keeps the durable layer from going stale against, or silently
duplicating, the fresh layer. Empirically (2026-06-13 study) staleness
concentrates in ``project``-type CC facts (state snapshots that race the
vault — "14 MCP tools" when the vault has 18) and almost never in
``feedback`` facts (durable principles don't expire). So ``project``-type +
age is a cheap stale *prior* the worker then confirms.

This module is the deterministic, embedding-free half — the promotion of the
old ``scripts/seam_map.py`` analysis pass into a maintained module. It:

1. walks the CC memory dirs into fact records carrying a content hash + mtime
   (the incremental keys),
2. diffs them against the durable state map (``vault/.weave/memory_seam.json``)
   to decide which facts are *dirty* and need (re)judgment this cycle,
3. computes deterministic priors (the project-type/age stale prior, the
   cosine bands) the worker reasons from,
4. renders the small human/SessionStart-facing report
   (``vault/.weave/memory_seam.md``).

What it deliberately does NOT do: resolve vault twins or call any embedding
API. Twin resolution is irreducibly an LLM judgment on a dense vault (every
fact has *some* nearest neighbour; "is it the SAME fact?" is semantic, not a
threshold), and the dream *scan* phase is contractually API-free. So the
``dream-seam-worker`` resolves twins via ``weave_search(mode='similar')`` in its
own turn and writes its verdicts back through ``weave seam commit``. The split
mirrors concepts/themes: Python records, the agent judges.

Verdict taxonomy (assigned by the worker, stored in the state map):

- ``confirmed-fresh`` — a vault twin exists, agrees, and is itself current.
  Rendered as a one-line corroboration pointer.
- ``stale`` — the CC fact's claim contradicts the twin's *current* state
  (count drift, status drift, "impl REMOVED" vs a twin still ``accepted``).
  The actionable bucket.
- ``diverged`` — twin exists but fact and twin disagree without a clear
  stale direction; needs a human look.
- ``durable-unique`` — no real twin; genuine CC-only durable knowledge.
  Counted, not enumerated.
- ``unjudged`` — surfaced but not yet ruled on (e.g. beyond the per-cycle
  cap); resurfaces next cycle.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from thinkweave.core.config import Config

# CC auto-memory lives per-project under ~/.claude/projects/<slug>/memory/
# plus an (often empty) global ~/.claude/memory.
CC_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CC_GLOBAL_DIR = Path.home() / ".claude" / "memory"

# Durable map (state) + rendered lens. Both vault-internal, never indexed.
STATE_RELPATH = Path(".weave") / "memory_seam.json"
REPORT_RELPATH = Path(".weave") / "memory_seam.md"

VERDICTS = (
    "confirmed-fresh",
    "stale",
    "diverged",
    "durable-unique",
    "unjudged",
)
#: Verdicts that warrant re-judgment every cycle until they resolve — an
#: unresolved fact is never left to age out silently.
_UNRESOLVED = frozenset({"stale", "diverged", "unjudged"})

_FM_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
# A CC project dir slug is the project's absolute path with "/" → "-"
# (e.g. /home/x/python_projects/thinkweave →
#  -home-x-python_projects-thinkweave). For DISPLAY we strip the home
# prefix; we never map back to a vault project — twin resolution is
# whole-vault by design (twins aren't co-located by project).
_HOME_PREFIX = str(Path.home()).replace("/", "-")


# ---------------------------------------------------------------------------
# Fact collection (the promoted scripts/seam_map.py walk)
# ---------------------------------------------------------------------------


def _hash(raw: str) -> str:
    """Content hash for a CC memory file — the incremental change key.

    Shared by :func:`collect_cc_facts` (the walk) and
    :func:`recompute_fact_hash` (the per-served-fact live drift check) so the
    two can never disagree about what "changed".
    """
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _parse_md(text: str) -> tuple[dict, str]:
    """Split a memory file into (frontmatter dict, body)."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def _label_from_dir_slug(dir_slug: str) -> str:
    """Human label for a CC project dir slug (display only)."""
    if dir_slug == "__global__":
        return "(global)"
    rest = dir_slug
    if rest.startswith(_HOME_PREFIX):
        rest = rest[len(_HOME_PREFIX):]
    rest = rest.lstrip("-")
    # Keep the trailing path component(s) as a readable name.
    return rest.replace("-", "_") or dir_slug


def _build_query(fm: dict, body: str) -> str:
    """The text the worker hands to ``weave_search(mode='similar')``.

    Description + a markdown-stripped body slice — enough signal to land
    the twin without the frontmatter noise. Capped so a long fact doesn't
    blow the embedding the worker runs.
    """
    desc = (fm.get("description") or "").strip()
    body_clean = re.sub(r"[#*`>\[\]|]", " ", body).strip()
    q = (desc + "\n" + body_clean).strip()
    return q[:1200]


def collect_cc_facts() -> list[dict]:
    """Walk every CC memory dir into fact records.

    Each record carries the incremental keys (``content_hash`` over the raw
    file, ``mtime``) plus the fields the worker judges from (``description``,
    ``query``, ``weave_type``). ``key`` is ``<dir_slug>::<slug>`` — slugs can
    repeat across project dirs, so the dir qualifies them.

    Pure filesystem walk over ``~/.claude`` — NOT a vault crawl (the
    operational no-crawl rule is about the vault index, which this never
    touches). ``MEMORY.md`` indexes are skipped (they hold no fact).
    """
    facts: list[dict] = []
    dirs: list[tuple[str, Path]] = []
    if CC_GLOBAL_DIR.is_dir():
        dirs.append(("__global__", CC_GLOBAL_DIR))
    if CC_PROJECTS_ROOT.is_dir():
        for d in sorted(CC_PROJECTS_ROOT.iterdir()):
            weave = d / "memory"
            if weave.is_dir():
                dirs.append((d.name, weave))

    for dir_slug, weave_dir in dirs:
        for f in sorted(weave_dir.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            try:
                raw = f.read_text(encoding="utf-8", errors="replace")
                mtime = f.stat().st_mtime
            except OSError:
                continue
            fm, body = _parse_md(raw)
            meta = fm.get("metadata") or {}
            weave_type = (
                meta.get("type") if isinstance(meta, dict) else None
            ) or "?"
            slug = fm.get("name") or f.stem
            content_hash = _hash(raw)
            facts.append({
                "key": f"{dir_slug}::{slug}",
                "dir_slug": dir_slug,
                "scope": "global" if dir_slug == "__global__" else "project",
                "label": _label_from_dir_slug(dir_slug),
                "file": f.name,
                "slug": slug,
                "weave_type": weave_type,
                "description": (fm.get("description") or "").strip(),
                "query": _build_query(fm, body),
                "content_hash": content_hash,
                "mtime": round(mtime, 3),
            })
    return facts


# ---------------------------------------------------------------------------
# Durable state map I/O
# ---------------------------------------------------------------------------


def state_path(cfg: Config) -> Path:
    return cfg.vault_root / STATE_RELPATH


def report_path(cfg: Config) -> Path:
    return cfg.vault_root / REPORT_RELPATH


def load_state(cfg: Config) -> dict:
    """Read the durable map; tolerate missing / corrupt with an empty shell."""
    p = state_path(cfg)
    if not p.exists():
        return {"facts": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"facts": []}
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        return {"facts": []}
    return data


def save_state(cfg: Config, state: dict) -> Path:
    p = state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Deterministic priors
# ---------------------------------------------------------------------------


def stale_prior(fact: dict, *, stale_age_days: int, now: datetime) -> bool:
    """Cheap "this might be stale" flag the worker reasons from.

    ``project``-type facts are state snapshots that race the vault's fresh
    layer; one untouched for ``stale_age_days`` is a stale-state *risk*.
    ``feedback``/``user``/``reference`` facts are durable — never flagged.
    mtime is the proxy for "last asserted" (it resets on edit, which is
    exactly when the fact stops being stale).
    """
    if str(fact.get("weave_type")) != "project":
        return False
    mtime = fact.get("mtime") or 0.0
    if not mtime:
        return False
    age_days = (now.timestamp() - float(mtime)) / 86400.0
    return age_days >= stale_age_days


def _recheck_due(prior: dict, *, now: datetime, recheck_days: int) -> bool:
    """True when a previously-judged fact is due for re-validation.

    Re-validating ``confirmed-fresh`` / ``durable-unique`` periodically is
    how the seam catches *vault* drift (the twin moved, not the CC fact) —
    a simpler, lookup-free substitute for tracking each twin's updated_at.
    A fact with no ``judged_at`` is always due.
    """
    judged = str(prior.get("judged_at") or "")
    if not judged:
        return True
    try:
        ts = datetime.fromisoformat(judged)
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).days >= recheck_days


# ---------------------------------------------------------------------------
# Dirty detection (cheap — the dream scan surface)
# ---------------------------------------------------------------------------


def detect_dirty(
    facts: list[dict],
    state: dict,
    *,
    stale_age_days: int,
    recheck_days: int,
    cap: int,
    now: datetime | None = None,
) -> dict:
    """Diff current CC facts against the durable map.

    A fact is *dirty* (needs the worker this cycle) when it is:
    - ``new`` (no prior record),
    - ``content_changed`` (the file was edited),
    - ``prior_unresolved`` (last verdict was stale/diverged/unjudged), or
    - ``recheck_due`` (a resolved verdict aged past ``recheck_days``).

    Returns a surface dict — ``dirty`` (capped, each annotated with reason +
    prior verdict/twin + the stale prior), ``removed`` (state keys whose file
    is gone), ``carried_count`` (clean facts that keep their prior verdict),
    plus the cosine ``thresholds`` and file paths the worker needs. Cheap:
    pure dict diffing, no index or embedding access.
    """
    now = now or datetime.now(timezone.utc)
    prior = {r.get("key"): r for r in state.get("facts", []) if r.get("key")}

    dirty: list[dict] = []
    for f in facts:
        f["stale_prior"] = stale_prior(
            f, stale_age_days=stale_age_days, now=now
        )
        p = prior.get(f["key"])
        if p is None:
            reason = "new"
        elif p.get("content_hash") != f["content_hash"]:
            reason = "content_changed"
        elif p.get("verdict") in _UNRESOLVED:
            reason = "prior_unresolved"
        elif _recheck_due(p, now=now, recheck_days=recheck_days):
            reason = "recheck_due"
        else:
            continue  # clean — carry the prior verdict forward unchanged
        twin = (p or {}).get("twin") or {}
        dirty.append({
            **f,
            "reason": reason,
            "prior_verdict": (p or {}).get("verdict"),
            "prior_twin_id": twin.get("id"),
        })

    current_keys = {f["key"] for f in facts}
    removed = sorted(k for k in prior if k not in current_keys)

    # New / unresolved first (most actionable), then rechecks. Stable within
    # a bucket by key so the surface is deterministic across runs.
    _ORDER = {"new": 0, "content_changed": 1, "prior_unresolved": 2, "recheck_due": 3}
    dirty.sort(key=lambda d: (_ORDER.get(d["reason"], 9), d["key"]))
    capped = dirty[:cap] if cap else dirty

    return {
        "dirty": capped,
        "dirty_total": len(dirty),
        "removed": removed,
        "carried_count": len(facts) - len(dirty),
        "thresholds": {"twin": None, "none": None},  # filled by caller
        "report_path": "",  # filled by caller
        "state_path": "",  # filled by caller
    }


# ---------------------------------------------------------------------------
# State rebuild (the `weave seam commit` core) + renderer
# ---------------------------------------------------------------------------


def build_state(
    facts: list[dict],
    prior_state: dict,
    verdicts: dict,
    *,
    stale_age_days: int,
    now: datetime | None = None,
) -> dict:
    """Merge the worker's fresh verdicts with carried-forward priors.

    ``verdicts`` maps ``key`` → ``{"verdict", "reason", "twin": {...}}`` (the
    worker's judgments this cycle). For every *current* CC fact: take the
    worker verdict if present, else carry the prior state record, else mark
    ``unjudged``. Removed facts drop out (not in ``facts``). The content hash
    is always recomputed from the just-collected fact, so the map can never
    record a verdict against stale text.
    """
    now = now or datetime.now(timezone.utc)
    prior = {r.get("key"): r for r in prior_state.get("facts", []) if r.get("key")}
    judged_at = now.isoformat()

    out_facts: list[dict] = []
    for f in facts:
        key = f["key"]
        base = {
            "key": key,
            "scope": f["scope"],
            "label": f["label"],
            "file": f["file"],
            "slug": f["slug"],
            "weave_type": f["weave_type"],
            "description": f["description"],
            "content_hash": f["content_hash"],
            "mtime": f["mtime"],
            "stale_prior": stale_prior(
                f, stale_age_days=stale_age_days, now=now
            ),
        }
        v = verdicts.get(key)
        if v and v.get("verdict") in VERDICTS:
            base["verdict"] = v["verdict"]
            base["verdict_reason"] = (v.get("reason") or "").strip()
            base["twin"] = v.get("twin") or {}
            base["judged_at"] = judged_at
        else:
            p = prior.get(key)
            if p:
                base["verdict"] = p.get("verdict") or "unjudged"
                base["verdict_reason"] = p.get("verdict_reason") or ""
                base["twin"] = p.get("twin") or {}
                base["judged_at"] = p.get("judged_at") or ""
            else:
                base["verdict"] = "unjudged"
                base["verdict_reason"] = ""
                base["twin"] = {}
                base["judged_at"] = ""
        out_facts.append(base)

    out_facts.sort(key=lambda r: r["key"])
    return {"generated_at": judged_at, "facts": out_facts}


def _twin_ref(twin: dict) -> str:
    """Compact ``id (cos)`` pointer for a twin, or ``—``."""
    if not twin or not twin.get("id"):
        return "—"
    cid = twin.get("id")
    cos = twin.get("cosine")
    return f"`{cid}`" + (f" (cos {cos})" if cos is not None else "")


def render_report(state: dict) -> str:
    """Render the small, deltas-only lens over the two channels.

    Stale + diverged are enumerated with their actionable line; confirmed
    facts collapse to one-line corroboration pointers; durable-unique is a
    single count. This is the artifact a future SessionStart flip would
    inject at the TOP of the vault payload — for now it's report-only.
    """
    facts = state.get("facts", [])
    by: dict[str, list[dict]] = {v: [] for v in VERDICTS}
    for r in facts:
        by.setdefault(r.get("verdict") or "unjudged", []).append(r)

    gen = state.get("generated_at", "")
    lines: list[str] = [
        "# Memory Seam",
        "",
        f"*Generated {gen}. Reconciles Claude Code auto-memory (durable "
        f"layer) against the vault (fresh layer). {len(facts)} facts "
        f"tracked. This is the full human-facing report; in-session, only "
        f"the stale/diverged twins that intersect a session's served notes "
        f"are surfaced (the SessionStart guard).*",
        "",
        "Legend: ⚠ stale (durable fact contradicts current vault) · "
        "⚲ diverged (disagrees, direction unclear) · ✓ confirmed-fresh · "
        "◇ durable-unique (CC-only).",
        "",
    ]

    stale = by.get("stale", [])
    lines.append(f"## ⚠ Stale ({len(stale)})")
    if stale:
        for r in stale:
            reason = r.get("verdict_reason") or "(no reason recorded)"
            lines.append(
                f"- **{r['slug']}** [{r['label']}] — {reason} "
                f"· twin {_twin_ref(r.get('twin') or {})}"
            )
    else:
        lines.append("- none")
    lines.append("")

    diverged = by.get("diverged", [])
    lines.append(f"## ⚲ Diverged ({len(diverged)})")
    if diverged:
        for r in diverged:
            reason = r.get("verdict_reason") or "(no reason recorded)"
            lines.append(
                f"- **{r['slug']}** [{r['label']}] — {reason} "
                f"· twin {_twin_ref(r.get('twin') or {})}"
            )
    else:
        lines.append("- none")
    lines.append("")

    confirmed = by.get("confirmed-fresh", [])
    lines.append(f"## ✓ Confirmed-fresh ({len(confirmed)})")
    if confirmed:
        for r in confirmed:
            lines.append(
                f"- **{r['slug']}** [{r['label']}] ↔ twin "
                f"{_twin_ref(r.get('twin') or {})}"
            )
    else:
        lines.append("- none")
    lines.append("")

    unique = by.get("durable-unique", [])
    unjudged = by.get("unjudged", [])
    lines.append(
        f"## ◇ Durable-unique: {len(unique)} facts"
        + (f" · {len(unjudged)} unjudged (resurface next cycle)" if unjudged else "")
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session-scoped serving lens — cross-match the map against served context
# ---------------------------------------------------------------------------
#
# The seam map is NOT dumped wholesale at SessionStart. It's a precomputed
# cross-reference (CC fact ↔ vault twin id + verdict); serving intersects it
# against the vault notes ACTUALLY served into the session. A warning fires
# only when a note Claude is relying on right now is the twin of a durable
# memory the seam flagged stale/diverged — so the durable layer can't quietly
# contradict the fresh layer in front of the model. No served twin matches →
# nothing injected.

#: Verdicts worth interrupting the model over. confirmed-fresh / durable-unique
#: are the healthy states and never surface in-session.
_ACTIONABLE = frozenset({"stale", "diverged"})


def flagged_twin_index(state: dict) -> dict[str, list[dict]]:
    """Reverse index ``twin_note_id → [flagged fact, ...]`` from the map.

    Only ``stale`` / ``diverged`` facts with a resolved twin id are indexed —
    the actionable set. One twin can back several CC facts, so the value is a
    list.
    """
    index: dict[str, list[dict]] = {}
    for r in state.get("facts", []):
        if r.get("verdict") not in _ACTIONABLE:
            continue
        twin = r.get("twin") or {}
        tid = twin.get("id")
        if not tid:
            continue
        index.setdefault(str(tid), []).append(r)
    return index


def _fact_file_path(fact: dict) -> Path | None:
    """Reconstruct the on-disk CC memory file for a state fact.

    ``key`` is ``<dir_slug>::<slug>``; combined with ``scope`` + ``file`` it
    locates the source file so a served fact's hash can be re-checked live.
    """
    key = fact.get("key") or ""
    file = fact.get("file") or ""
    if "::" not in key or not file:
        return None
    dir_slug = key.rsplit("::", 1)[0]
    if fact.get("scope") == "global" or dir_slug == "__global__":
        return CC_GLOBAL_DIR / file
    return CC_PROJECTS_ROOT / dir_slug / "memory" / file


def recompute_fact_hash(fact: dict) -> str | None:
    """Re-hash a single CC fact's current file, or ``None`` if unreadable.

    Bounded-cost live drift check: the serving lens calls this only for the
    handful of facts whose twins are actually in the session's context, so a
    flagged verdict can be marked "memory edited since judged — re-verify"
    without re-reading all ~138 files.
    """
    p = _fact_file_path(fact)
    if p is None:
        return None
    try:
        return _hash(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def session_guard_section(
    cfg: Config, served_ids, *, max_items: int = 8
) -> str:
    """Render the session-scoped durable-memory guard, or ``""`` if no hit.

    Intersects the served note ids against the flagged-twin index. For each
    hit, a live hash re-check flags whether the CC memory was edited since the
    verdict (in which case the stale/diverged call may no longer hold — the
    nightly cycle will re-judge it). Returns a small markdown block intended to
    be prepended to the SessionStart payload; empty string means inject
    nothing.
    """
    served = {str(i) for i in (served_ids or [])}
    if not served:
        return ""
    index = flagged_twin_index(load_state(cfg))
    if not index:
        return ""

    hits: list[tuple[str, dict]] = []
    for nid in served:
        for fact in index.get(nid, []):
            hits.append((nid, fact))
    if not hits:
        return ""

    # Stable, actionable-first ordering: stale before diverged, then slug.
    hits.sort(key=lambda h: (h[1].get("verdict") != "stale", h[1].get("slug", "")))

    lines = [
        "## ⚠ Durable-memory guard",
        "",
        "Notes in your context match Claude Code memories the seam flagged as "
        "possibly stale against the vault. **Verify against the live note "
        "before trusting the memory:**",
        "",
    ]
    for nid, fact in hits[:max_items]:
        verdict = fact.get("verdict")
        sym = "⚠" if verdict == "stale" else "⚲"
        reason = fact.get("verdict_reason") or "(no reason recorded)"
        drift = ""
        cur = recompute_fact_hash(fact)
        if cur is not None and cur != fact.get("content_hash"):
            drift = " — ⟳ *memory edited since this verdict; treat as unverified*"
        lines.append(
            f"- {sym} Memory **{fact.get('slug')}** may be {verdict}: {reason} "
            f"→ cross-check [[{nid}]] (in your context){drift}"
        )
    extra = len(hits) - max_items
    if extra > 0:
        lines.append(f"- *…and {extra} more flagged twins in context.*")
    lines.append("")
    return "\n".join(lines)
