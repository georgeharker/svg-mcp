"""Bulk graph ingestion: the producer's wire shape in, a laid-out diagram out."""

from __future__ import annotations

import io
from collections.abc import Mapping

import pytest

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.ops import graph as graph_ops
from svg_mcp.ops.diagram import _box_of, read_edge_spec, read_node_spec
from svg_mcp.ops.graph import GraphEdge, GraphImport, GraphNode
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

# A real code-graph export, verbatim in shape: `from`/`to`, and per-object keys we never asked
# for. Everything in this module is fed through model_validate so the test exercises the same
# parse an MCP call would.
EXPORT_NODES: list[Mapping[str, object]] = [
    {"id": "src/svg_mcp/ops/diagram.py", "file": "src/svg_mcp/ops/diagram.py", "symbols": 177},
    {"id": "src/svg_mcp/ops/annotate.py", "file": "src/svg_mcp/ops/annotate.py", "symbols": 46},
    {"id": "src/svg_mcp/ops/themes.py", "file": "src/svg_mcp/ops/themes.py", "symbols": 92},
]
EXPORT_EDGES: list[Mapping[str, object]] = [
    {"from": "src/svg_mcp/ops/annotate.py", "to": "src/svg_mcp/ops/diagram.py",
     "kind": "calls", "weight": 49},
    {"from": "src/svg_mcp/ops/diagram.py", "to": "src/svg_mcp/ops/themes.py",
     "kind": "calls", "weight": 12},
]


def _doc() -> Document:
    return DocumentStore().create(900, 600)[1]


def _nodes(*raw: Mapping[str, object]) -> list[GraphNode]:
    return [GraphNode.model_validate(entry) for entry in raw]


def _edges(*raw: Mapping[str, object]) -> list[GraphEdge]:
    return [GraphEdge.model_validate(entry) for entry in raw]


def _ingest(doc: Document, **kwargs: object) -> GraphImport:
    """The realistic export, ingested — every test that does not care about the data uses this."""
    return ops.add_diagram_graph(
        doc,
        nodes=_nodes(*EXPORT_NODES),
        edges=_edges(*EXPORT_EDGES),
        **kwargs,  # type: ignore[arg-type]
    )


def _node_names(doc: Document) -> list[str]:
    return [
        str(node.label)
        for node in doc.svg.iter()
        if isinstance(node.tag, str) and read_node_spec(node) is not None
    ]


def _labels(doc: Document) -> dict[str, str]:
    """Graph id (the node's name) → the label its box actually says."""
    out: dict[str, str] = {}
    for node in doc.svg.iter():
        spec = read_node_spec(node) if isinstance(node.tag, str) else None
        if spec is not None:
            out[str(node.label)] = spec.label
    return out


def _edge_specs(doc: Document) -> list[tuple[str, str, str, str]]:
    specs = []
    for node in doc.svg.iter():
        spec = read_edge_spec(node) if isinstance(node.tag, str) else None
        if spec is not None:
            specs.append((spec.source, spec.target, spec.kind, spec.label))
    return specs


# --- 1. the wire shape -------------------------------------------------------


def test_a_real_export_round_trips_into_nodes_edges_and_a_mapping() -> None:
    doc = _doc()
    result = _ingest(doc)
    assert (result.nodes_created, result.edges_created) == (3, 2)
    assert _node_names(doc) == [entry["id"] for entry in EXPORT_NODES]
    assert set(result.mapping) == {str(entry["id"]) for entry in EXPORT_NODES}
    for graph_id, node_id in result.mapping.items():
        assert str(doc.resolve(node_id).label) == graph_id
    ids = result.mapping
    assert _edge_specs(doc) == [
        (ids["src/svg_mcp/ops/annotate.py"], ids["src/svg_mcp/ops/diagram.py"], "data", ""),
        (ids["src/svg_mcp/ops/diagram.py"], ids["src/svg_mcp/ops/themes.py"], "data", ""),
    ]


def test_both_spellings_of_an_edge_s_endpoints_parse_to_the_same_edge() -> None:
    producer = GraphEdge.model_validate({"from": "a", "to": "b"})
    ours = GraphEdge.model_validate({"source": "a", "target": "b"})
    assert (producer.source, producer.target) == ("a", "b")
    assert (ours.source, ours.target) == ("a", "b")


def test_keys_the_producer_carries_and_we_do_not_want_are_ignored_not_rejected() -> None:
    node = GraphNode.model_validate({"id": "a", "file": "a.py", "symbols": 177, "loc": 1200})
    edge = GraphEdge.model_validate({"from": "a", "to": "b", "provenance": "static", "line": 12})
    assert node.id == "a" and node.label is None
    assert (edge.source, edge.target, edge.weight) == ("a", "b", None)


def test_a_kind_no_theme_can_dress_falls_back_to_the_default_and_is_reported() -> None:
    # The producer's "calls" is a taxonomy, not a design language — ingestion must not die on it.
    doc = _doc()
    result = _ingest(doc)
    assert result.kinds_defaulted == ["calls"]
    assert {spec[2] for spec in _edge_specs(doc)} == {"data"}


def test_a_kind_the_theme_does_serve_is_used_verbatim() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "queue", "kind": "datastore"}, {"id": "web"}),
        edges=_edges({"from": "web", "to": "queue", "kind": "control"}),
    )
    kinds = {
        str(node.label): spec.kind
        for node in doc.svg.iter()
        if isinstance(node.tag, str) and (spec := read_node_spec(node)) is not None
    }
    assert kinds == {"queue": "datastore", "web": "service"}
    assert _edge_specs(doc)[0][2] == "control"


# --- 2. what gets dropped ----------------------------------------------------


def test_a_self_edge_is_dropped_and_counted() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a"}, {"id": "b"}),
        edges=_edges({"from": "a", "to": "a"}, {"from": "a", "to": "b"}),
    )
    assert (result.self_edges_dropped, result.edges_created) == (1, 1)


def test_an_edge_naming_a_node_nobody_declared_is_rejected_by_name() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument) as raised:
        ops.add_diagram_graph(
            doc,
            nodes=_nodes({"id": "a"}, {"id": "b"}),
            edges=_edges({"from": "a", "to": "ghost"}),
        )
    message = str(raised.value)
    assert "'ghost'" in message and "target" in message
    assert not _node_names(doc)  # nothing was drawn, and no box was invented for the ghost


def test_a_light_edge_is_dropped_by_min_weight_but_an_unweighted_one_survives() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a"}, {"id": "b"}, {"id": "c"}),
        edges=_edges(
            {"from": "a", "to": "b", "weight": 2},
            {"from": "b", "to": "c", "weight": 40},
            {"from": "a", "to": "c"},
        ),
        min_weight=10,
    )
    assert (result.edges_dropped_weight, result.edges_created) == (1, 2)


def test_duplicate_edges_merge_into_one_arrow_summing_their_weights() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a"}, {"id": "b"}),
        edges=_edges(
            {"from": "a", "to": "b", "weight": 3},
            {"from": "a", "to": "b", "weight": 4},
            {"from": "a", "to": "b", "kind": "calls", "weight": 5},
        ),
        weight_labels=True,
    )
    # All three collapse: "calls" defaults to "data" BEFORE the merge, so the third is a parallel
    # copy of the first two rather than a second identical arrow.
    assert result.edges_created == 1
    assert _edge_specs(doc)[0][3] == "12"


# --- 3. the glob filters -----------------------------------------------------


def test_include_keeps_only_what_matches_and_drops_the_edges_it_orphaned() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "src/a.py"}, {"id": "src/b.py"}, {"id": "tests/t.py"}),
        edges=_edges(
            {"from": "src/a.py", "to": "src/b.py"},
            {"from": "tests/t.py", "to": "src/a.py"},
        ),
        include=["src/*"],
    )
    assert (result.nodes_filtered, result.nodes_created) == (1, 2)
    assert (result.edges_dropped_filtered, result.edges_created) == (1, 1)
    assert _node_names(doc) == ["src/a.py", "src/b.py"]


def test_exclude_beats_include_for_a_node_both_patterns_match() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "src/a.py"}, {"id": "src/a_test.py"}),
        edges=[],
        include=["src/*"],
        exclude=["*_test.py"],
    )
    assert (result.nodes_created, result.nodes_filtered) == (1, 1)
    assert _node_names(doc) == ["src/a.py"]


# --- 4. labels ---------------------------------------------------------------


def test_label_mode_id_writes_the_id_verbatim() -> None:
    doc = _doc()
    _ingest(doc, label_mode="id")
    assert _labels(doc)["src/svg_mcp/ops/themes.py"] == "src/svg_mcp/ops/themes.py"


def test_label_mode_basename_keeps_the_last_segment_without_its_extension() -> None:
    doc = _doc()
    _ingest(doc, label_mode="basename")
    assert _labels(doc)["src/svg_mcp/ops/themes.py"] == "themes"


def test_label_mode_trimmed_drops_the_prefix_every_id_shares() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "src/ops/diagram.py"}, {"id": "src/ops/deep/themes.py"}),
        edges=[],
    )
    # The shared prefix is cut back to a "/" boundary, so a path survives as a path.
    assert _labels(doc) == {
        "src/ops/diagram.py": "diagram",
        "src/ops/deep/themes.py": "deep/themes",
    }


def test_a_shared_prefix_is_never_cut_mid_segment() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc, nodes=_nodes({"id": "ops/diagram.py"}, {"id": "ops/diagrams.py"}), edges=[]
    )
    assert set(_labels(doc).values()) == {"diagram", "diagrams"}


def test_dotted_fqname_ids_are_split_on_the_dot_not_read_as_a_file_extension() -> None:
    # The other shape a code-graph producer emits: symbol ids, not file paths. Read as a path,
    # `svg_mcp.ops.graph.add_diagram_graph` has the "extension" `.add_diagram_graph`, and every
    # symbol in a module would end up captioned with the module's name.
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes(
            {"id": "svg_mcp.ops.graph.add_diagram_graph", "kind": "function"},
            {"id": "svg_mcp.ops.graph._shared_prefix", "kind": "function"},
            {"id": "svg_mcp.model.errors.InvalidArgument", "kind": "class"},
        ),
        edges=_edges(
            {"from": "svg_mcp.ops.graph.add_diagram_graph",
             "to": "svg_mcp.ops.graph._shared_prefix"},
            {"from": "svg_mcp.ops.graph.add_diagram_graph",
             "to": "svg_mcp.model.errors.InvalidArgument"},
        ),
    )
    # A symbol export's node kinds are a taxonomy too, and they default like an edge's does.
    assert result.kinds_defaulted == ["class", "function"]
    assert _labels(doc) == {
        "svg_mcp.ops.graph.add_diagram_graph": "ops.graph.add_diagram_graph",
        "svg_mcp.ops.graph._shared_prefix": "ops.graph._shared_prefix",
        "svg_mcp.model.errors.InvalidArgument": "model.errors.InvalidArgument",
    }


def test_basename_of_a_dotted_fqname_is_the_symbol_itself() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "svg_mcp.ops.graph.add_diagram_graph"}),
        edges=[],
        label_mode="basename",
    )
    assert _labels(doc)["svg_mcp.ops.graph.add_diagram_graph"] == "add_diagram_graph"


def test_a_dot_in_a_directory_name_is_still_not_an_extension() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc, nodes=_nodes({"id": "src/v1.2/alpha.py"}, {"id": "src/v1.2/beta.py"}), edges=[]
    )
    assert set(_labels(doc).values()) == {"alpha", "beta"}


def test_an_explicit_label_wins_over_every_label_mode() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "src/ops/themes.py", "label": "Theme engine"}),
        edges=[],
        label_mode="basename",
    )
    assert _labels(doc)["src/ops/themes.py"] == "Theme engine"


def test_weight_labels_write_an_integral_weight_as_an_integer_and_a_label_wins() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a"}, {"id": "b"}, {"id": "c"}),
        edges=_edges(
            {"from": "a", "to": "b", "weight": 49.0},
            {"from": "b", "to": "c", "weight": 2.5, "label": "hot path"},
        ),
        weight_labels=True,
    )
    assert [spec[3] for spec in _edge_specs(doc)] == ["49", "hot path"]


# --- 5. layout ---------------------------------------------------------------


def test_layout_none_leaves_the_nodes_in_the_stack_ingestion_built() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a"}, {"id": "b"}, {"id": "c"}),
        edges=_edges({"from": "a", "to": "c"}),
        layout="none",
    )
    assert (result.ranks, result.cycles_broken, result.edges_rerouted) == (None, None, None)
    boxes = [_box_of(doc, node_id) for node_id in result.mapping.values()]
    assert all(box is not None for box in boxes)
    xs = {box.x for box in boxes if box is not None}
    ys = [box.y for box in boxes if box is not None]
    assert len(xs) == 1  # stacked: one column
    assert ys == sorted(ys) and len(set(ys)) == 3


def test_a_layered_ingest_ranks_the_chain_and_reports_the_layout() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a"}, {"id": "b"}, {"id": "c"}),
        edges=_edges({"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "a"}),
    )
    assert result.ranks == 3
    assert result.cycles_broken == 1  # c → a is the back edge
    assert result.edges_rerouted == 3
    boxes = [_box_of(doc, result.mapping[key]) for key in ("a", "b", "c")]
    xs = [box.x for box in boxes if box is not None]
    assert xs == sorted(xs) and len(set(xs)) == 3  # LR: one rank per column


# --- 6. refusals and atomicity -----------------------------------------------


def test_a_graph_id_that_collides_with_an_existing_name_is_refused() -> None:
    doc = _doc()
    ops.add_diagram_node(doc, kind="service", label="taken", name="a")
    with pytest.raises(InvalidArgument, match="collides"):
        ops.add_diagram_graph(doc, nodes=_nodes({"id": "a"}), edges=[])
    assert _node_names(doc) == ["a"]  # only the one that was already there


def test_the_same_id_declared_twice_is_refused_before_anything_is_drawn() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="appears twice"):
        ops.add_diagram_graph(doc, nodes=_nodes({"id": "a"}, {"id": "a"}), edges=[])
    assert not _node_names(doc)


def test_a_default_kind_no_theme_serves_is_the_caller_s_error_and_raises() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="no theme defines a style"):
        ops.add_diagram_graph(
            doc, nodes=_nodes({"id": "a"}), edges=[], default_node_kind="bureau"
        )
    assert not _node_names(doc)


def test_a_failure_part_way_through_creation_leaves_the_document_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _doc()
    # A document that has already drawn a diagram: the theme block and the shared arrowhead are
    # in place, so what is compared is genuinely the ingest's own footprint.
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "old-a"}, {"id": "old-b"}),
        edges=_edges({"from": "old-a", "to": "old-b"}),
    )
    before = export_svg(doc)

    real = ops.add_diagram_edge
    calls = {"n": 0}

    def failing(*args: object, **kwargs: object) -> ops.PlacedEdge:
        calls["n"] += 1
        if calls["n"] == 2:
            raise InvalidArgument("boom, half way through the edges")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(graph_ops, "add_diagram_edge", failing)
    with pytest.raises(InvalidArgument, match="boom"):
        ops.add_diagram_graph(
            doc,
            nodes=_nodes({"id": "a"}, {"id": "b"}, {"id": "c"}),
            edges=_edges({"from": "a", "to": "b"}, {"from": "b", "to": "c"}),
        )
    assert export_svg(doc) == before


# --- 7. render ---------------------------------------------------------------


def test_an_eight_node_graph_renders_with_its_boxes_in_the_theme_s_paint() -> None:
    renderer = get_renderer()
    if not renderer.available():
        pytest.skip("no resvg backend available")
    from PIL import Image

    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes(*[{"id": f"pkg/mod{index}.py"} for index in range(8)]),
        edges=_edges(
            {"from": "pkg/mod0.py", "to": "pkg/mod1.py", "weight": 9},
            {"from": "pkg/mod0.py", "to": "pkg/mod2.py", "weight": 4},
            {"from": "pkg/mod1.py", "to": "pkg/mod3.py"},
            {"from": "pkg/mod2.py", "to": "pkg/mod3.py"},
            {"from": "pkg/mod3.py", "to": "pkg/mod4.py"},
            {"from": "pkg/mod5.py", "to": "pkg/mod1.py"},
            {"from": "pkg/mod6.py", "to": "pkg/mod7.py"},
        ),
        direction="LR",
        weight_labels=True,
    )
    assert result.nodes_created == 8 and result.edges_created == 7

    rendered = renderer.render(RenderRequest(svg=export_svg(doc)))
    image = Image.open(io.BytesIO(rendered.png)).convert("RGBA")
    box = _box_of(doc, result.mapping["pkg/mod3.py"])
    assert box is not None
    pixel = image.getpixel((int(box.x + box.w / 2), int(box.y + 4)))
    assert isinstance(pixel, tuple)
    assert pixel[:3] == (236, 238, 241)  # the default theme's service fill
