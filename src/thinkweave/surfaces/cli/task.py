"""``weave task`` — the task routes' CLI verbs (the headless route).

Five actions over :mod:`thinkweave.operations.tasks`:

- ``weave task open`` — mint a child task at a dispatch boundary. Prints
  the minted task id.
- ``weave task close <task-id>`` — close it: the performer's envelope rows
  and the digest of the session a prompt bound to it become its round.
  Envelope rows that fail the schema are reported on stderr and the exit
  code is 1; the close row is recorded either way.
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
    # vault) governs the verbs and never the live vault.
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


def _project(args: argparse.Namespace) -> str:
    from thinkweave.core.config import detect_project

    return args.project or detect_project()


def _cmd_open(args: argparse.Namespace) -> None:
    from thinkweave.operations import tasks

    dispatch = tasks.open_child(
        _load_config(),
        session_key=_session_key(args),
        project=_project(args),
        title=args.title,
        grain=args.grain,
        role=args.role,
        asked=args.asked,
    )
    print(dispatch.task_id)


def _cmd_close(args: argparse.Namespace) -> None:
    from thinkweave.operations import tasks

    try:
        result = tasks.close_child(
            _load_config(), args.task_id, session_key=_session_key(args)
        )
    except ValueError as exc:
        print(f"close: {exc}", file=sys.stderr)
        sys.exit(2)
    for error in result.errors:
        print(error, file=sys.stderr)
    for gap in result.gaps:
        print(f"digest: {gap}", file=sys.stderr)
    print(
        f"task {result.task_id} closed · {result.envelopes} envelope "
        f"row(s) · {result.note}"
    )
    if result.errors:
        sys.exit(1)


def _cmd_render(args: argparse.Namespace) -> None:
    from thinkweave.operations import tasks

    dispatch = tasks.dispatch_descriptor(_load_config(), args.task_id)
    print(json.dumps(dispatch.to_dict(), indent=2))


def _cmd_ledger(args: argparse.Namespace) -> None:
    from thinkweave.operations import tasks

    register = tasks.Register(_load_config(), _session_key(args))
    for entry in register.listing():
        print(json.dumps(entry.to_dict()))


def _cmd_record_run(args: argparse.Namespace) -> None:
    from pathlib import Path

    from thinkweave.operations import tasks

    try:
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        landed = tasks.record_run(
            _load_config(),
            payload,
            project=_project(args),
            trajectory=args.trajectory,
            session_key=args.session,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"record-run: {exc}", file=sys.stderr)
        sys.exit(2)
    for warning in landed.warnings:
        print(f"record-run: {warning}", file=sys.stderr)
    print(landed.task_id)
