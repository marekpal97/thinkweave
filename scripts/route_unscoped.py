"""Route notes from _unscoped and _automated pseudo-projects to real projects.

Routing rules:
  _unscoped:
    captain|DAG|briefing|companion|swarm|autonomous|hive -> hive_swarm
    novelty|autoresearch|RLVR|gate.mechanism|decoder|residual|thinkmesh|neural -> thinkmesh_neural
    wrap|hook|memory.system|obsidian|vault|personal.mem|session.reconcil -> personal_mem
    unmatched -> hive_swarm

  _automated:
    transaction|classifier|dashboard|merchant|ingestion|savings|budget -> personal_finance_assistant
    ranking|annotation|novelty|annotator -> thinkmesh_neural
    langgraph|langchain|agent.scaffold|crewai -> hive_swarm
    unmatched -> thinkmesh_neural
"""

import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

# Add src to path so we can import personal_mem
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from personal_mem.core.vault import parse_frontmatter, render_frontmatter

VAULT = Path.home() / "vault"

# --- Routing rules ---
UNSCOPED_RULES = [
    (re.compile(r"captain|DAG|briefing|companion|swarm|autonomous|hive", re.IGNORECASE), "hive_swarm"),
    (re.compile(r"novelty|autoresearch|RLVR|gate.mechanism|decoder|residual|thinkmesh|neural", re.IGNORECASE), "thinkmesh_neural"),
    (re.compile(r"wrap|hook|memory.system|obsidian|vault|personal.mem|session.reconcil", re.IGNORECASE), "personal_mem"),
]
UNSCOPED_DEFAULT = "hive_swarm"

AUTOMATED_RULES = [
    (re.compile(r"transaction|classifier|dashboard|merchant|ingestion|savings|budget", re.IGNORECASE), "personal_finance_assistant"),
    (re.compile(r"ranking|annotation|novelty|annotator", re.IGNORECASE), "thinkmesh_neural"),
    (re.compile(r"langgraph|langchain|agent.scaffold|crewai", re.IGNORECASE), "hive_swarm"),
]
AUTOMATED_DEFAULT = "thinkmesh_neural"


def classify(text: str, rules: list, default: str) -> str:
    for pattern, project in rules:
        if pattern.search(text):
            return project
    return default


def get_snippet(text: str, body: str) -> str:
    """Get title line + first 300 chars of body for matching."""
    # Extract the first heading (title) from body
    title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    title = title_match.group(1) if title_match else ""
    snippet = title + " " + body[:300]
    return snippet


def ensure_misc(project: str) -> Path:
    """Ensure vault/projects/{project}/sessions/misc/ exists."""
    misc = VAULT / "projects" / project / "sessions" / "misc"
    misc.mkdir(parents=True, exist_ok=True)
    return misc


def safe_dest(dest_dir: Path, filename: str) -> Path:
    """Return a destination path, appending a counter suffix if name collides."""
    dest = dest_dir / filename
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    counter = 1
    while True:
        candidate = dest_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def process_pseudo_project(source_project: str, rules: list, default: str, counts: dict) -> list[dict]:
    """Process all .md files in a pseudo-project and route them. Returns list of move records."""
    sessions_dir = VAULT / "projects" / source_project / "sessions"
    if not sessions_dir.exists():
        print(f"  [WARN] {sessions_dir} does not exist, skipping.")
        return []

    records = []
    files = list(sessions_dir.rglob("*.md"))
    print(f"\nProcessing {source_project}: found {len(files)} .md files")

    for fpath in files:
        raw = fpath.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)

        if not fm:
            # No frontmatter — skip (landing docs etc.)
            print(f"  [SKIP] No frontmatter: {fpath.relative_to(sessions_dir)}")
            continue

        # Build snippet: title from frontmatter or first heading + first 300 chars body
        fm_title = str(fm.get("title", ""))
        snippet = get_snippet(fm_title, body)

        target_project = classify(snippet, rules, default)
        dest_misc = ensure_misc(target_project)
        dest_path = safe_dest(dest_misc, fpath.name)

        # Update project field in frontmatter
        fm["project"] = target_project
        new_content = render_frontmatter(fm) + "\n" + body

        # Write updated content to destination
        dest_path.write_text(new_content, encoding="utf-8")

        # Remove source file
        fpath.unlink()

        counts[target_project] += 1
        records.append({
            "source": str(fpath),
            "dest": str(dest_path),
            "project": target_project,
        })

    # Clean up empty session subdirectories
    for session_dir in sorted(sessions_dir.iterdir(), reverse=True):
        if session_dir.is_dir():
            try:
                session_dir.rmdir()  # only removes if empty
            except OSError:
                pass  # not empty, leave it

    return records


def main():
    counts: dict[str, int] = defaultdict(int)
    all_records = []

    # Process _unscoped
    records = process_pseudo_project("_unscoped", UNSCOPED_RULES, UNSCOPED_DEFAULT, counts)
    all_records.extend(records)

    # Process _automated
    records = process_pseudo_project("_automated", AUTOMATED_RULES, AUTOMATED_DEFAULT, counts)
    all_records.extend(records)

    # Print routing summary
    print("\n" + "=" * 60)
    print("ROUTING SUMMARY")
    print("=" * 60)
    total = sum(counts.values())
    for project in sorted(counts.keys()):
        print(f"  {project:35s}  {counts[project]:>4d} notes")
    print(f"  {'TOTAL':35s}  {total:>4d} notes")

    # Detailed breakdown by source pseudo-project
    print("\nDetailed breakdown:")
    unscoped_counts: dict[str, int] = defaultdict(int)
    automated_counts: dict[str, int] = defaultdict(int)
    for r in all_records:
        if "_unscoped" in r["source"]:
            unscoped_counts[r["project"]] += 1
        else:
            automated_counts[r["project"]] += 1

    print("  From _unscoped:")
    for p in sorted(unscoped_counts):
        print(f"    {p}: {unscoped_counts[p]}")
    print("  From _automated:")
    for p in sorted(automated_counts):
        print(f"    {p}: {automated_counts[p]}")

    print(f"\nDone. {total} notes moved.")


if __name__ == "__main__":
    main()
