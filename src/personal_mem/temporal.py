"""Shared temporal-DAG primitives for concept hubs and themes.

Both concept hubs (learning log) and themes (catalyst log) share the same
log-entry shape: a dated bullet with an observational flag and an optional
reference to an earlier entry by date. This module turns that shape into
nodes + edges and renders a Mermaid graph for either surface.

Format both surfaces use, parsed by ``hubs.parse_concept_hub`` /
``themes.parse_theme_catalyst_log``::

    - 2026-04-22 · *contradicts 2026-04-15* — text — [[citation-id]]

The flag follows the citation by date when one is given, and ``new``
entries carry no ref. ``mem hubs link`` (in ``cli.py``) writes refs after
the fact via the OpenAI Batches API; ``/update-hubs`` writes them inline.

This module is intentionally agnostic about whether entries came from a
hub or a theme — it consumes a list of ``LogEntry``-shaped records and
produces a generic ``graph LR`` Mermaid diagram. Decisions implementing a
theme are attached as separate node kinds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Protocol


# Edge relation vocabulary — covers both hub flags (agrees / contradicts /
# extends) and theme flags (confirms / contradicts) plus the cross-cutting
# decision-implements edge that anchors trade decisions onto theme catalysts.
NodeKind = Literal["log_entry", "catalyst", "decision", "theme"]
EdgeRelation = Literal[
    "agrees", "contradicts", "extends", "confirms", "implements"
]


@dataclass(frozen=True)
class TemporalNode:
    id: str               # stable id — e.g. "2026-04-15" or "dec-XXXX"
    label: str            # one-line label for the node body
    kind: NodeKind
    date: str = ""        # YYYY-MM-DD when applicable; empty for theme/decision


@dataclass(frozen=True)
class TemporalEdge:
    src: str              # source node id
    dst: str              # destination node id
    relation: EdgeRelation


@dataclass
class TemporalGraph:
    nodes: list[TemporalNode] = field(default_factory=list)
    edges: list[TemporalEdge] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.nodes


class _LogEntryLike(Protocol):
    """Shape that ``hubs.LogEntry`` and ``themes.CatalystEntry`` both share.

    Defined as a Protocol so we don't take a hard dependency on either
    module — temporal.py stays leaf.
    """

    date: str
    flag: str
    ref: str
    text: str
    citation: str


def entries_to_graph(
    entries: Iterable[_LogEntryLike],
    *,
    decisions: Iterable[dict] | None = None,
    kind: NodeKind = "log_entry",
) -> TemporalGraph:
    """Build a TemporalGraph from a sequence of log entries.

    Each entry becomes one node, identified by its date. When two entries
    share a date, the second collides — id is suffixed ``#2`` etc. Edges
    are built only when ``ref`` points to a date that exists in the same
    sequence (no dangling edges).

    ``decisions`` is an optional list of dicts with at least keys
    ``id``, ``title``, and optional ``implements_catalyst`` (date string).
    A decision without a catalyst pin is attached to the latest catalyst
    in the sequence; if there are no catalysts at all, the decision is
    omitted (we have nothing to anchor it to).

    ``kind`` controls the NodeKind assigned to log entries — pass
    ``"catalyst"`` when rendering a theme.
    """
    sorted_entries = sorted(entries, key=lambda e: (e.date, e.citation))

    seen_dates: dict[str, int] = {}
    node_id_for_index: dict[int, str] = {}
    nodes: list[TemporalNode] = []

    for i, entry in enumerate(sorted_entries):
        n = seen_dates.get(entry.date, 0) + 1
        seen_dates[entry.date] = n
        node_id = entry.date if n == 1 else f"{entry.date}#{n}"
        node_id_for_index[i] = node_id

        label_text = entry.text or entry.citation or entry.flag
        # Mermaid node labels can't carry raw quotes / pipes / brackets
        # cleanly. Strip the worst offenders and trim length.
        label = _safe_label(label_text)
        nodes.append(
            TemporalNode(id=node_id, label=label, kind=kind, date=entry.date)
        )

    # Edges from the flag-with-ref machinery. A ref points to the *date*
    # of an earlier entry; we resolve to the first matching node id.
    edges: list[TemporalEdge] = []
    date_to_first_id: dict[str, str] = {}
    for n in nodes:
        date_to_first_id.setdefault(n.date, n.id)

    for i, entry in enumerate(sorted_entries):
        if entry.flag == "new" or not entry.ref:
            continue
        if entry.ref not in date_to_first_id:
            continue
        src = date_to_first_id[entry.ref]
        dst = node_id_for_index[i]
        if src == dst:
            continue
        edges.append(TemporalEdge(src=src, dst=dst, relation=entry.flag))

    if decisions:
        latest_catalyst_id = nodes[-1].id if nodes else ""
        for d in decisions:
            d_id = str(d.get("id", "")).strip()
            if not d_id:
                continue
            label = _safe_label(str(d.get("title", "")))
            anchor_date = str(d.get("implements_catalyst", "")).strip()
            anchor_id = (
                date_to_first_id.get(anchor_date, "")
                if anchor_date
                else latest_catalyst_id
            )
            if not anchor_id:
                continue
            nodes.append(
                TemporalNode(id=d_id, label=label, kind="decision")
            )
            edges.append(
                TemporalEdge(src=anchor_id, dst=d_id, relation="implements")
            )

    return TemporalGraph(nodes=nodes, edges=edges)


def render_mermaid(graph: TemporalGraph) -> str:
    """Render a TemporalGraph as Mermaid ``graph LR`` block (no fences)."""
    if graph.is_empty():
        return ""

    lines: list[str] = ["graph LR"]
    style_decisions: list[str] = []

    for node in graph.nodes:
        node_label = (
            f"{node.date}: {node.label}"
            if node.date and node.kind in ("log_entry", "catalyst")
            else node.label or node.id
        )
        node_label = _safe_label(node_label)
        # Use brackets for entries/catalysts, paren-shape for decisions.
        if node.kind == "decision":
            lines.append(f'    {_safe_id(node.id)}(("{node_label}"))')
            style_decisions.append(_safe_id(node.id))
        else:
            lines.append(f'    {_safe_id(node.id)}["{node_label}"]')

    for edge in graph.edges:
        arrow = "-.->" if edge.relation == "contradicts" else "-->"
        lines.append(
            f'    {_safe_id(edge.src)} {arrow}|{edge.relation}| {_safe_id(edge.dst)}'
        )

    if style_decisions:
        lines.append(
            "    classDef decision fill:#f5f5f5,stroke:#888,stroke-dasharray: 3 3;"
        )
        lines.append(
            "    class " + ",".join(style_decisions) + " decision;"
        )

    return "\n".join(lines)


def render_evolution_section(graph: TemporalGraph, *, heading: str = "## Evolution") -> str:
    """Wrap a Mermaid render as a complete `## Evolution` section.

    Empty graph → empty string (caller decides whether to omit the section
    entirely or leave a placeholder).
    """
    if graph.is_empty():
        return ""
    block = render_mermaid(graph)
    return f"{heading}\n\n```mermaid\n{block}\n```\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Mermaid node IDs must match `[A-Za-z0-9_]+`. Dates contain hyphens which
# aren't valid in IDs (only labels), so we encode them.
def _safe_id(raw: str) -> str:
    out = []
    for ch in raw:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "node"


def _safe_label(text: str, max_len: int = 80) -> str:
    """Trim and escape a node label for Mermaid.

    Strips wikilink syntax, replaces double quotes / pipes / backticks,
    collapses whitespace, and truncates to ``max_len`` chars.
    """
    if not text:
        return ""
    out = text.replace("[[", "").replace("]]", "")
    out = out.replace('"', "'").replace("|", "/").replace("`", "'")
    out = " ".join(out.split())
    if len(out) > max_len:
        out = out[: max_len - 1].rstrip() + "…"
    return out
