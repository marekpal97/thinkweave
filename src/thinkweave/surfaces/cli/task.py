"""``weave task`` — the dispatch seam's CLI verbs (the headless route).

Five actions over :mod:`thinkweave.operations.task_seam`:

- ``weave task open`` — mint a task at a dispatch boundary: stub note plus
  a ``task_open`` row in the events register. Prints the minted task id.
- ``weave task close <task-id>`` — record the boundary close, compile the
  performer's envelope rows into the stub's ``rounds[]``. Envelope rows
  that fail the schema are reported on stderr and the exit code is 1; the
  close row is recorded either way.
- ``weave task render <task-id>`` — re-emit the dispatch descriptor JSON.
- ``weave task ledger`` — list one session's task boundaries as JSON. The
  hooks mint seam children silently, so this is how the wrap declaration
  composer learns their ids before declaring ``children``.
- ``weave task record-run <payload.json>`` — land a devloop run as a
  ``route: devloop`` round on the open task its issue ref resolves to.

Open and close correlate by the task id alone — a harness without hooks
runs exactly this route and loses only the boundary automation.
"""

from __future__ import annotations

import argparse
import json
import sys


def _load_config():
    # Late-bound so a test's patched ``core.config.load_config`` (temp
    # vault) governs the verbs — an import-time binding here once let a
    # test write into the live vault.
    from thinkweave.core.config import load_config

    return load_config()


def cmd_task(args: argparse.Namespace) -> None:
    action = getattr(args, "task_action", None)
    if action == "open":
        _cmd_open(args)
    elif action == "close":
        _cmd_close(args)
    elif action == "render":
        _cmd_render(args)
    elif action == "ledger":
        _cmd_ledger(args)
    elif action == "record-run":
        _cmd_record_run(args)
    else:
        print(
            "Usage: weave task {open|close|render|ledger|record-run}",
            file=sys.stderr,
        )
        sys.exit(2)


def _session_key(args: argparse.Namespace) -> str:
    from thinkweave.core import harness

    return args.session or harness.env_session_id() or "unattributed"


def _cmd_open(args: argparse.Namespace) -> None:
    from thinkweave.operations import task_seam

    dispatch = task_seam.open_task(
        _load_config(),
        session_key=_session_key(args),
        project=args.project,
        title=args.title,
        grain=args.grain,
        role=args.role,
    )
    print(dispatch.task_id)


def _cmd_close(args: argparse.Namespace) -> None:
    from thinkweave.operations import task_seam

    result = task_seam.close_task(
        _load_config(), args.task_id, session_key=_session_key(args)
    )
    for error in result.errors:
        print(error, file=sys.stderr)
    print(
        f"task {result.task_id} closed · {result.envelopes} envelope "
        f"row(s) · {result.note}"
    )
    if result.errors:
        sys.exit(1)


def _cmd_render(args: argparse.Namespace) -> None:
    from thinkweave.operations import task_seam

    dispatch = task_seam.render_descriptor(_load_config(), args.task_id)
    print(json.dumps(dispatch.to_dict(), indent=2))


def _cmd_ledger(args: argparse.Namespace) -> None:
    from thinkweave.core.vault import parse_frontmatter
    from thinkweave.operations import task_seam

    cfg = _load_config()
    rows = task_seam.session_task_rows(cfg, _session_key(args))
    for task_id, entry in task_seam.task_ledger(rows).items():
        opened = entry["open"] or {}
        item = {
            "task_id": task_id,
            "grain": str(opened.get("grain", "")),
            "opened": str(opened.get("ts", "")),
            "closed": bool(entry["close"]),
        }
        stub = task_seam.find_stub(cfg, task_id)
        if stub is not None:
            fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
            item["title"] = str(fm.get("title", ""))
            item["status"] = str(fm.get("status", ""))
            if fm.get("parent"):
                item["parent"] = str(fm["parent"])
        print(json.dumps(item))


def _cmd_record_run(args: argparse.Namespace) -> None:
    from pathlib import Path

    from thinkweave.operations import task_seam

    try:
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        task_id = task_seam.record_devloop_run(
            _load_config(),
            payload,
            project=args.project,
            trajectory=args.trajectory,
            session_key=args.session,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"record-run: {exc}", file=sys.stderr)
        sys.exit(2)
    print(task_id)
