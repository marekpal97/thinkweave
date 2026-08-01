"""Risk-lane classification of shipped PRs — pure over (signals, cfg).

Fail-closed on the three safety-critical signals (``baseline_green``,
``acceptance``, ``review_severity``): a missing key or an off-enum value
goes RED rather than green-eligible. That posture is part of the interface.
"""

from __future__ import annotations

from devloop import paths

# The rail already computes every triage signal (gate results incl. review
# severity, diff size, files touched, fix_rounds, degraded baseline). This
# classifies each shipped PR into green/yellow/red so a human reviews only what
# matters — escalation-not-gates, matching the loop's existing ready-for-human
# rung. Labels are APPLIED by the orchestrator (via gh); the rail only decides.

# Recognized enum values. "none"/"minor" review stay green-eligible (the
# issue's "review <= minor"); "met" acceptance is the only clean pass. A value
# OUTSIDE these sets is not benign — LLM-assembled signals make enum drift
# ("high", "partial", "blocker") realistic, so an unrecognized value fails
# closed to red rather than slipping through green-eligible.
_VALID_REVIEW = {"none", "minor", "major", "critical"}
_RED_REVIEW = {"major", "critical"}
_VALID_ACCEPTANCE = {"met", "uncertain", "not-met"}
_RED_ACCEPTANCE = {"uncertain", "not-met"}

# Green/yellow labels are loop-internal vocabulary. The red label is NOT here —
# it is sourced from labels.on_gate_failure (classify_pr's red_label arg) so
# triage-red and gate-failure share one label with no duplicate literal.
TRIAGE_LABELS = {"green": "auto-merge-ok", "yellow": "review-light"}


def classify_pr(signals: dict, cfg: dict, red_label: str | None = None) -> dict:
    """Classify one shipped PR into a risk lane. Pure over (signals, cfg).

    ``cfg`` is the resolved ``[triage]`` config section. ``red_label`` is the
    tracker label for the red lane — sourced from ``labels.on_gate_failure`` by
    the caller (default keeps the canonical ``ready-for-human``) so triage-red
    and gate-failure stay one label. Precedence is red > yellow > green, and
    every triggered rule is listed in ``reasons`` (short-circuit reasons: report
    all of them, not just the first). Returns ``{lane, label, reasons}``.

    **Fail-closed.** The three safety-critical signals — ``baseline_green``,
    ``acceptance``, ``review_severity`` — are REQUIRED: an absent key or an
    unrecognized enum value goes RED (naming the key/value), never
    green-eligible, because LLM-assembled signals make that drift realistic.
    The rest default benignly (absence is not a safety hole).

    Signals schema:
      - ``fix_rounds`` int — implement→gate→fix iterations (0 = first try) [opt, →0]
      - ``diff_lines`` int — total changed lines in the PR's diff [opt, →0]
      - ``files_touched`` list[str] — repo-relative paths changed [opt, →[]]
      - ``tests_touched`` bool — the change carries test coverage [opt, →False]
      - ``review_severity`` str — worst review finding: none|minor|major|critical [REQUIRED]
      - ``baseline_green`` bool — tests gate green on the pristine worktree [REQUIRED]
      - ``acceptance`` str — acceptance verdict: met|uncertain|not-met [REQUIRED]
    """
    if red_label is None:
        # Imported lazily: cli owns DEFAULT_CONFIG and imports this module,
        # so a module-level import would be a cycle. The CLI always passes
        # labels.on_gate_failure explicitly; this is the direct-caller default.
        from devloop.cli import DEFAULT_CONFIG

        red_label = DEFAULT_CONFIG["labels"]["on_gate_failure"]
    fix_rounds = int(signals.get("fix_rounds", 0) or 0)
    diff_lines = int(signals.get("diff_lines", 0) or 0)
    files = signals.get("files_touched") or []
    tests_touched = bool(signals.get("tests_touched", False))

    # --- red: any hard-escalation rule. List them all. -----------------------
    red: list[str] = []
    sensitive = paths.hits(files, cfg.get("sensitive_paths", []))
    if sensitive:
        red.append("sensitive path(s): " + ", ".join(sensitive))
    if diff_lines >= cfg["red_min_diff_lines"]:
        red.append(f"large diff: {diff_lines} lines >= {cfg['red_min_diff_lines']}")

    # baseline_green — required; missing, non-bool (a truthy "false" string must
    # not pass), or False → red.
    if "baseline_green" not in signals:
        red.append("baseline_green signal missing (fail-closed)")
    elif not isinstance(signals["baseline_green"], bool):
        red.append(f"non-bool baseline_green {signals['baseline_green']!r} (fail-closed)")
    elif not signals["baseline_green"]:
        red.append("degraded baseline (tests not green on the pristine worktree)")

    # review_severity — required; missing or off-enum → red.
    if "review_severity" not in signals:
        red.append("review_severity signal missing (fail-closed)")
    else:
        review = str(signals["review_severity"]).lower()
        if review not in _VALID_REVIEW:
            red.append(f"unrecognized review_severity '{signals['review_severity']}' (fail-closed)")
        elif review in _RED_REVIEW:
            red.append(f"review severity {review}")

    # acceptance — required; missing or off-enum → red.
    if "acceptance" not in signals:
        red.append("acceptance signal missing (fail-closed)")
    else:
        acceptance = str(signals["acceptance"]).lower()
        if acceptance not in _VALID_ACCEPTANCE:
            red.append(f"unrecognized acceptance '{signals['acceptance']}' (fail-closed)")
        elif acceptance in _RED_ACCEPTANCE:
            red.append(f"acceptance {acceptance}")

    if red:
        return {"lane": "red", "label": red_label, "reasons": red}

    # --- yellow: passed, but warrants a human skim. List them all. -----------
    yellow: list[str] = []
    if cfg.get("green_requires_first_try", True) and fix_rounds > 0:
        yellow.append(f"{fix_rounds} fix round(s)")
    if diff_lines >= cfg["green_max_diff_lines"]:
        yellow.append(f"medium diff: {diff_lines} lines >= {cfg['green_max_diff_lines']}")
    watched = paths.hits(files, cfg.get("watched_paths", []))
    if watched:
        yellow.append("watched path(s): " + ", ".join(watched))
    if not tests_touched:
        yellow.append("no test coverage signal (tests_touched=false)")
    if yellow:
        return {"lane": "yellow", "label": TRIAGE_LABELS["yellow"], "reasons": yellow}

    # --- green criteria all met. Only auto-merge-ok where green is enabled. ---
    if cfg.get("green_enabled", False):
        return {"lane": "green", "label": TRIAGE_LABELS["green"], "reasons": []}
    return {"lane": "yellow", "label": TRIAGE_LABELS["yellow"],
            "reasons": ["green lane disabled (training-mode graduation pending)"]}
