"""Tests for temporal.py — the shared DAG renderer for hubs and themes."""

from __future__ import annotations

from dataclasses import dataclass

from thinkweave.retrieval.temporal import (
    TemporalEdge,
    TemporalGraph,
    TemporalNode,
    entries_to_graph,
    render_evolution_section,
    render_mermaid,
)


@dataclass
class _Entry:
    """Minimal LogEntry-shaped record used by these tests."""

    date: str
    flag: str
    ref: str = ""
    text: str = ""
    citation: str = ""


# ---------------------------------------------------------------------------
# entries_to_graph
# ---------------------------------------------------------------------------


class TestEntriesToGraph:
    def test_empty_input_yields_empty_graph(self):
        g = entries_to_graph([])
        assert g.is_empty()

    def test_single_new_entry_no_edges(self):
        g = entries_to_graph([_Entry("2026-01-01", "new", text="seed")])
        assert len(g.nodes) == 1
        assert g.nodes[0].kind == "log_entry"
        assert g.edges == []

    def test_extends_creates_edge_to_earlier_entry(self):
        entries = [
            _Entry("2026-01-01", "new", text="A"),
            _Entry("2026-02-01", "extends", ref="2026-01-01", text="B"),
        ]
        g = entries_to_graph(entries)
        assert len(g.nodes) == 2
        assert len(g.edges) == 1
        e = g.edges[0]
        assert e.src == "2026-01-01"
        assert e.dst == "2026-02-01"
        assert e.relation == "extends"

    def test_contradicts_relation_preserved(self):
        entries = [
            _Entry("2026-01-01", "new", text="A"),
            _Entry("2026-02-01", "contradicts", ref="2026-01-01", text="B"),
        ]
        g = entries_to_graph(entries)
        assert g.edges[0].relation == "contradicts"

    def test_dangling_ref_silently_dropped(self):
        # Entry references a date that doesn't exist in the input.
        entries = [
            _Entry("2026-02-01", "extends", ref="2099-01-01", text="orphan"),
        ]
        g = entries_to_graph(entries)
        assert len(g.nodes) == 1
        assert g.edges == []  # no dangling edges

    def test_collision_on_same_date_distinct_node_ids(self):
        entries = [
            _Entry("2026-01-01", "new", citation="src-a", text="A"),
            _Entry("2026-01-01", "new", citation="src-b", text="B"),
        ]
        g = entries_to_graph(entries)
        ids = [n.id for n in g.nodes]
        assert ids[0] == "2026-01-01"
        assert ids[1].startswith("2026-01-01#")
        assert ids[0] != ids[1]

    def test_decision_pinned_to_specific_catalyst(self):
        entries = [
            _Entry("2026-01-01", "new", text="catalyst A"),
            _Entry("2026-02-01", "new", text="catalyst B"),
        ]
        decisions = [
            {"id": "dec-1", "title": "trade X", "implements_catalyst": "2026-01-01"},
        ]
        g = entries_to_graph(entries, decisions=decisions, kind="catalyst")
        # 2 catalysts + 1 decision = 3 nodes; the decision edges from 01-01.
        assert len(g.nodes) == 3
        decision_node = [n for n in g.nodes if n.kind == "decision"][0]
        assert decision_node.id == "dec-1"
        impl_edges = [e for e in g.edges if e.relation == "implements"]
        assert len(impl_edges) == 1
        assert impl_edges[0].src == "2026-01-01"
        assert impl_edges[0].dst == "dec-1"

    def test_decision_without_pin_attaches_to_latest(self):
        entries = [
            _Entry("2026-01-01", "new"),
            _Entry("2026-02-01", "new"),
        ]
        decisions = [{"id": "dec-1", "title": "X"}]
        g = entries_to_graph(entries, decisions=decisions, kind="catalyst")
        impl = [e for e in g.edges if e.relation == "implements"][0]
        # latest catalyst is 2026-02-01.
        assert impl.src == "2026-02-01"

    def test_decision_dropped_when_no_catalysts(self):
        decisions = [{"id": "dec-1", "title": "X"}]
        g = entries_to_graph([], decisions=decisions, kind="catalyst")
        assert g.is_empty()  # nothing to anchor to


# ---------------------------------------------------------------------------
# render_mermaid
# ---------------------------------------------------------------------------


class TestRenderMermaid:
    def test_empty_graph_renders_empty_string(self):
        assert render_mermaid(TemporalGraph()) == ""

    def test_basic_render_has_graph_lr_header(self):
        g = entries_to_graph([_Entry("2026-01-01", "new", text="seed")])
        out = render_mermaid(g)
        assert out.startswith("graph LR")

    def test_dates_safe_in_node_ids(self):
        # Dashes aren't legal in Mermaid IDs; the renderer must encode them.
        g = entries_to_graph(
            [
                _Entry("2026-01-01", "new", text="A"),
                _Entry("2026-02-01", "extends", ref="2026-01-01", text="B"),
            ]
        )
        out = render_mermaid(g)
        # Dashes encoded as underscores; the original date appears in labels.
        assert "2026_01_01" in out
        assert "2026-01-01" in out  # in the label

    def test_contradicts_uses_dotted_arrow(self):
        g = entries_to_graph(
            [
                _Entry("2026-01-01", "new", text="A"),
                _Entry("2026-02-01", "contradicts", ref="2026-01-01", text="B"),
            ]
        )
        out = render_mermaid(g)
        assert "-.->" in out

    def test_decision_styled_as_pill(self):
        entries = [_Entry("2026-01-01", "new", text="A")]
        decisions = [{"id": "dec-1", "title": "trade X"}]
        g = entries_to_graph(entries, decisions=decisions, kind="catalyst")
        out = render_mermaid(g)
        assert 'dec_1((' in out  # circle/pill node
        assert "classDef decision" in out


# ---------------------------------------------------------------------------
# render_evolution_section
# ---------------------------------------------------------------------------


class TestEvolutionSection:
    def test_empty_graph_yields_empty_section(self):
        assert render_evolution_section(TemporalGraph()) == ""

    def test_section_wraps_in_mermaid_fences(self):
        g = entries_to_graph(
            [
                _Entry("2026-01-01", "new", text="A"),
                _Entry("2026-02-01", "extends", ref="2026-01-01", text="B"),
            ]
        )
        out = render_evolution_section(g)
        assert out.startswith("## Evolution")
        assert "```mermaid" in out
        assert out.rstrip().endswith("```")

    def test_custom_heading(self):
        g = entries_to_graph([_Entry("2026-01-01", "new", text="A")])
        out = render_evolution_section(g, heading="### Custom theme")
        assert out.startswith("### Custom theme")
