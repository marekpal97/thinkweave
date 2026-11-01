"""The #346 demo: one simulated devloop run on a fixture issue, through the
real ``scripts/issue_loop.py`` shim, offline.

Steps, each a subprocess of the shim or the ``weave`` CLI with this folder's
``bin/gh`` stub first on PATH (no GitHub read or write leaves the machine):
``config`` → ``prime`` → ``pack`` (implementer: persona + standing orders) →
``trajectory`` (the shim adds ``--manifest-json``) → the trajectory note →
``weave task record-run``. It prints the observables: where the pack texts
came from, the round's manifest (rail commit, funloops pin) and the stage
log's ``skill`` key.

Run it from the repo root with a throwaway vault::

    THINKWEAVE_VAULT=$(mktemp -d) uv run --no-sync python tests/fixtures/devloop_seam/demo.py

Whether the rail is configured is the caller's environment
(``THINKWEAVE_RSI_RAIL`` or the user-scope ``rsi_rail`` key), exactly as for
a real run. ``tests/test_issue_loop_seam.py`` drives :func:`run_demo` both
ways.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent
ROOT = FIXTURES.parents[2]
SHIM = ROOT / "scripts" / "issue_loop.py"
ISSUE = json.loads((FIXTURES / "issue.json").read_text(encoding="utf-8"))["number"]
LIVE_VAULT = Path("/mnt/c/Users/marek/vault")

_CREATE_NOTE = """
import json, sys
from thinkweave.core.config import load_config
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
p = json.load(open(sys.argv[1], encoding="utf-8"))
vm = VaultManager(config=load_config())
path = vm.create_note(NoteType.NOTE, p["title"], body=p["body_skeleton"], project=sys.argv[2],
                      tags=p["tags"], extra_frontmatter=p["frontmatter"])
print(json.dumps({"id": vm.read_note(path).id, "path": str(path)}))
"""


def _run(argv: list[str], env: dict, out: Path | None = None) -> str:
    done = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"{' '.join(argv[:4])} … rc={done.returncode}\n{done.stdout}{done.stderr}")
    if out is not None:
        out.write_text(done.stdout, encoding="utf-8")
    return done.stdout


def _two_commit_repo(path: Path) -> Path:
    """A throwaway git repo with two commits, so ``trajectory --base-ref
    HEAD~1`` has a range to walk whatever the checkout's own history is (CI
    clones at depth 1, where ``HEAD~1`` does not exist)."""
    path.mkdir()
    git = ["git", "-c", "user.name=demo", "-c", "user.email=demo@example.invalid"]
    subprocess.run([*git, "init", "-q", str(path)], check=True)
    for n in (1, 2):
        (path / "work.txt").write_text(f"step {n}\n", encoding="utf-8")
        subprocess.run([*git, "-C", str(path), "add", "work.txt"], check=True)
        subprocess.run([*git, "-C", str(path), "commit", "-q", "-m", f"step {n}"], check=True)
    return path


def run_demo(vault: Path, env: dict | None = None, project: str = "demo-346") -> dict:
    """Run the simulated loop into ``vault``; return the observables."""
    vault = Path(vault).resolve()
    if vault == LIVE_VAULT or LIVE_VAULT in vault.parents:
        raise SystemExit("refusing the live vault: pass a throwaway THINKWEAVE_VAULT")
    env = dict(os.environ if env is None else env)
    env["THINKWEAVE_VAULT"] = str(vault)
    env["PATH"] = f"{FIXTURES / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    (vault / "config").mkdir(parents=True, exist_ok=True)
    sources = vault / "config" / "sources.yaml"
    if not sources.exists():
        sources.write_text("sources: []\n", encoding="utf-8")
    py = sys.executable
    shim = [py, str(SHIM)]

    with tempfile.TemporaryDirectory(prefix="demo-346-") as tmp:
        work = Path(tmp)
        config = json.loads(_run([*shim, "config"], env))
        prime = json.loads(_run([*shim, "prime", str(ISSUE), "--run-id", "demo-346",
                                 "--concepts", "devloop", "--query", ISSUE_QUERY,
                                 "--vault", str(vault), "--dry-run"], env))
        (work / "prime.md").write_text(prime.get("text") or prime.get("note") or "", encoding="utf-8")
        pack = _run([*shim, "pack", str(ISSUE), "--role", "implementer", "--cwd", str(ROOT),
                     "--codegraph-bin", "false", "--prime", str(work / "prime.md"),
                     "--branch", "demo-346", "--baseline", "green"], env)
        payload_file = work / "payload.json"
        repo = _two_commit_repo(work / "repo")
        _run([*shim, "trajectory", str(ISSUE), "--cwd", str(repo), "--base-ref", "HEAD~1",
              "--branch", "HEAD", "--gates-json", str(FIXTURES / "gates.json"),
              "--skills-json", str(FIXTURES / "skills.json"), "--outcome", "shipped",
              "--run-id", "demo-346"], env, out=payload_file)
        payload = json.loads(payload_file.read_text(encoding="utf-8"))
        note = json.loads(_run([py, "-c", _CREATE_NOTE, str(payload_file), project], env))
        task_id = _run([str(Path(py).parent / "weave"), "task", "record-run",
                        str(payload_file), "--trajectory", note["id"], "--project", project,
                        "--session", "demo-346"], env).strip().splitlines()[-1]

    task_path = next(vault.rglob(f"{task_id}.md"))
    from thinkweave.core.vault import parse_frontmatter

    task = parse_frontmatter(task_path.read_text(encoding="utf-8"))[0]
    (round_,) = [r for r in task["rounds"] if r.get("route") == "devloop"]
    return {
        "policy_dir": env.get("DEVLOOP_POLICY_DIR") or _policy_dir_seen(config),
        "constitution": config["constitution"],
        "primed": prime.get("primed"),
        "pack": pack,
        "payload_manifest": payload.get("manifest"),
        "trajectory_note": note["path"],
        "task_note": str(task_path),
        "round": round_,
    }


ISSUE_QUERY = "devloop reads its policy from the rsi-rail repo"


def _policy_dir_seen(config: dict) -> str:
    """The rail's devloop/ when the constitution came from it, else ``""``."""
    first = Path(config["constitution"][0])
    return str(first.parent) if "site-packages" not in first.parts else ""


def main() -> int:
    vault = os.environ.get("THINKWEAVE_VAULT")
    if not vault:
        print("set THINKWEAVE_VAULT to a throwaway directory", file=sys.stderr)
        return 2
    from devloop import pack as devloop_pack

    seen = run_demo(Path(vault))
    manifest = seen["round"].get("manifest", {})
    rail = manifest.get("rsi_rail") or {}
    print(f"policy dir         : {seen['policy_dir'] or '(none: packaged defaults)'}")
    print(f"constitution       : {seen['constitution'][0]}")
    print(f"standing orders    : "
          f"{'module constant' if devloop_pack.STANDING_ORDERS in seen['pack'] else 'policy dir file'}")
    print(f"standing-orders h2 : {_heading(seen['pack'], '## Standing orders')}")
    print(f"prime primed       : {seen['primed']}")
    print(f"trajectory note    : {seen['trajectory_note']}")
    print(f"task note          : {seen['task_note']}")
    print(f"round manifest     : rsi_rail.commit={rail.get('commit', '(none)')} "
          f"dirty={rail.get('dirty', '-')}")
    print(f"round readers      : {json.dumps(manifest.get('readers', {}), sort_keys=True)}")
    print(f"stage skill keys   : {[s.get('skill') for s in seen['round'].get('skills', [])]}")
    return 0


def _heading(text: str, prefix: str) -> str:
    return next((line for line in text.splitlines() if line.startswith(prefix)), "(absent)")


if __name__ == "__main__":
    sys.exit(main())
