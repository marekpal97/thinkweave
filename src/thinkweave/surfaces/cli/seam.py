"""``weave seam`` — memory-seam maintenance CLI (the worker's Bash hands).

Two actions over :mod:`thinkweave.synthesis.memory_seam`:

- ``weave seam surface [--json]`` — re-emit the cheap, embedding-free dirty
  diff of CC auto-memory against the durable state map. Same payload the
  dream scan embeds as its ``memory_seam`` surface; handy for headless
  debugging and for a worker that wants the surface without re-reading the
  scan JSON.
- ``weave seam commit --verdicts <path>|- [--json]`` — the write path. Takes
  the ``dream-seam-worker``'s judged verdicts (``{key: {verdict, reason,
  twin}}``), recomputes the durable map from the *current* CC files
  (content hashes re-derived, so a verdict can never attach to stale text),
  and writes ``vault/.weave/memory_seam.json`` + the rendered
  ``vault/.weave/memory_seam.md``. Facts the worker didn't rule on keep their
  prior verdict; removed facts drop out.

The expensive half — twin resolution + the actual confirmed/stale/diverged
judgment — lives in the worker turn (``weave_search(mode='similar')`` + LLM
read), never here. This surface only does the deterministic diff + write.
"""

from __future__ import annotations

import argparse
import json
import sys

from thinkweave.core.config import load_config


def cmd_seam(args: argparse.Namespace) -> None:
    action = getattr(args, "seam_action", None)
    if action == "surface":
        _cmd_surface(args)
    elif action == "commit":
        _cmd_commit(args)
    else:
        print("Usage: weave seam {surface|commit}", file=sys.stderr)
        sys.exit(2)


def _cmd_surface(args: argparse.Namespace) -> None:
    from thinkweave.synthesis import memory_seam

    cfg = load_config()
    # --cap overrides config per-invocation (0 = unlimited — the backfill
    # lever the populate workflow uses to judge the whole backlog in one pass).
    cap_override = getattr(args, "cap", None)
    cap = int(cap_override) if cap_override is not None else int(
        getattr(cfg, "seam_cap", 20) or 0
    )
    facts = memory_seam.collect_cc_facts()
    state = memory_seam.load_state(cfg)
    surface = memory_seam.detect_dirty(
        facts,
        state,
        stale_age_days=int(getattr(cfg, "seam_stale_age_days", 30) or 30),
        recheck_days=int(getattr(cfg, "seam_recheck_days", 14) or 14),
        cap=cap,
    )
    surface["thresholds"] = {
        "twin": float(getattr(cfg, "seam_cosine_twin", 0.70) or 0.70),
        "none": float(getattr(cfg, "seam_cosine_none", 0.55) or 0.55),
    }
    surface["report_path"] = str(memory_seam.report_path(cfg))
    surface["state_path"] = str(memory_seam.state_path(cfg))

    if getattr(args, "json", False):
        print(json.dumps(surface, indent=2, sort_keys=True))
        return

    d = surface["dirty"]
    print(
        f"seam surface · {len(d)} dirty (of {surface['dirty_total']}) · "
        f"{len(surface['removed'])} removed · "
        f"{surface['carried_count']} carried"
    )
    for f in d:
        prior = f.get("prior_verdict") or "—"
        flag = " ⚑stale-prior" if f.get("stale_prior") else ""
        print(f"  [{f['reason']:<16}] {f['slug']}  (was {prior}){flag}")
    if surface["removed"]:
        print(f"  removed: {', '.join(surface['removed'])}")


def _cmd_commit(args: argparse.Namespace) -> None:
    from thinkweave.synthesis import memory_seam

    cfg = load_config()

    raw: str
    if args.verdicts == "-":
        raw = sys.stdin.read()
    else:
        with open(args.verdicts, encoding="utf-8") as f:
            raw = f.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON verdicts — {e}", file=sys.stderr)
        sys.exit(2)

    # Accept either a bare ``{key: {...}}`` map or an envelope
    # ``{"verdicts": {key: {...}}}`` (the worker's outcome shape).
    if isinstance(payload, dict) and "verdicts" in payload:
        verdicts = payload.get("verdicts") or {}
    else:
        verdicts = payload
    if not isinstance(verdicts, dict):
        print("error: verdicts must be a JSON object keyed by fact key",
              file=sys.stderr)
        sys.exit(2)

    facts = memory_seam.collect_cc_facts()
    prior = memory_seam.load_state(cfg)
    new_state = memory_seam.build_state(
        facts,
        prior,
        verdicts,
        stale_age_days=int(getattr(cfg, "seam_stale_age_days", 30) or 30),
    )
    state_p = memory_seam.save_state(cfg, new_state)
    report_p = memory_seam.report_path(cfg)
    report_p.parent.mkdir(parents=True, exist_ok=True)
    report_p.write_text(memory_seam.render_report(new_state), encoding="utf-8")

    counts: dict[str, int] = {}
    for r in new_state["facts"]:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    result = {
        "applied_verdicts": len(verdicts),
        "facts_total": len(new_state["facts"]),
        "counts": counts,
        "state_path": str(state_p),
        "report_path": str(report_p),
    }
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"seam commit · {len(verdicts)} verdicts · {result['facts_total']} facts")
    print("  " + " · ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    print(f"  state:  {state_p}")
    print(f"  report: {report_p}")
