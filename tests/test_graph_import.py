"""Bulk graph ingestion: the producer's wire shape in, a laid-out diagram out."""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import TypedDict, Unpack

import pytest

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import InvalidArgument
from svg_mcp.ops import graph as graph_ops
from svg_mcp.ops.diagram import _box_of, read_edge_spec, read_node_spec
from svg_mcp.ops.graph import GraphEdge, GraphGroup, GraphImport, GraphNode, LabelMode
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

Wire = Mapping[str, str | int | float | list[str]]
"""One node, edge or group exactly as a producer wires it — the value types an export can hold."""

# A real code-graph export, verbatim in shape: `from`/`to`, and per-object keys we never asked
# for. Everything in this module is fed through model_validate so the test exercises the same
# parse an MCP call would.
EXPORT_NODES: list[Wire] = [
    {"id": "src/svg_mcp/ops/diagram.py", "file": "src/svg_mcp/ops/diagram.py", "symbols": 177},
    {"id": "src/svg_mcp/ops/annotate.py", "file": "src/svg_mcp/ops/annotate.py", "symbols": 46},
    {"id": "src/svg_mcp/ops/themes.py", "file": "src/svg_mcp/ops/themes.py", "symbols": 92},
]
EXPORT_EDGES: list[Wire] = [
    {"from": "src/svg_mcp/ops/annotate.py", "to": "src/svg_mcp/ops/diagram.py",
     "kind": "calls", "weight": 49},
    {"from": "src/svg_mcp/ops/diagram.py", "to": "src/svg_mcp/ops/themes.py",
     "kind": "calls", "weight": 12},
]


class IngestOptions(TypedDict, total=False):
    """The ingest knobs `_ingest` forwards — spelled out so the forwarding is type-checked."""

    label_mode: LabelMode
    size_field: str | None
    size_labels: bool
    themed: bool


def _doc() -> Document:
    return DocumentStore().create(900, 600)[1]


def _nodes(*raw: Wire) -> list[GraphNode]:
    return [GraphNode.model_validate(entry) for entry in raw]


def _edges(*raw: Wire) -> list[GraphEdge]:
    return [GraphEdge.model_validate(entry) for entry in raw]


def _groups(*raw: Wire) -> list[GraphGroup]:
    return [GraphGroup.model_validate(entry) for entry in raw]


def _ingest(doc: Document, **kwargs: Unpack[IngestOptions]) -> GraphImport:
    """The realistic export, ingested — every test that does not care about the data uses this."""
    return ops.add_diagram_graph(
        doc,
        nodes=_nodes(*EXPORT_NODES),
        edges=_edges(*EXPORT_EDGES),
        **kwargs,
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


def test_an_ingested_graph_id_is_a_usable_handle_afterwards() -> None:
    # The point of naming nodes after their graph ids: the export's vocabulary keeps working.
    doc = _doc()
    result = _ingest(doc)
    graph_id = "src/svg_mcp/ops/diagram.py"
    assert str(doc.resolve(graph_id).get_id()) == result.mapping[graph_id]
    callout = ops.add_callout(doc, target=graph_id, text="the big one")
    assert callout.ref.id


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


def test_an_id_containing_glob_characters_still_excludes_itself() -> None:
    # Ids are written out one at a time by whoever judged them noise, and an id may contain glob
    # metacharacters of its own (`operator[]`, `[id].tsx`). An exact id must name itself.
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "app/[id].tsx"}, {"id": "app/page.tsx"}),
        edges=[],
        exclude=["app/[id].tsx"],
    )
    assert result.nodes_created == 1
    assert _node_names(doc) == ["app/page.tsx"]


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


# --- 3b. collapse: the caller's judgement, mechanically applied ---------------


def test_a_group_replaces_its_members_and_rewires_their_edges() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "web"}, {"id": "pg"}, {"id": "redis"}, {"id": "s3"}),
        edges=_edges(
            {"from": "web", "to": "pg", "weight": 3},
            {"from": "web", "to": "redis", "weight": 4},
            {"from": "web", "to": "s3"},
        ),
        collapse=_groups({"id": "storage", "label": "Storage", "members": ["pg", "redis", "s3"]}),
        weight_labels=True,
    )
    assert (result.groups_created, result.nodes_collapsed) == (1, 3)
    assert (result.nodes_created, result.edges_created) == (2, 1)
    assert _node_names(doc) == ["web", "storage"]
    assert _labels(doc)["storage"] == "Storage"
    # The three edges re-point at the group and merge; their weights sum, 3 + 4 + (none).
    assert _edge_specs(doc)[0][3] == "7"


def test_an_edge_between_two_members_becomes_internal_and_is_counted() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "web"}, {"id": "pg"}, {"id": "redis"}),
        edges=_edges({"from": "pg", "to": "redis"}, {"from": "web", "to": "pg"}),
        collapse=_groups({"id": "storage", "members": ["pg", "redis"]}),
    )
    assert (result.self_edges_dropped, result.edges_created) == (1, 1)


def test_a_collapsed_member_still_resolves_through_the_mapping() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "pg"}, {"id": "redis"}),
        edges=[],
        collapse=_groups({"id": "storage", "members": ["pg", "redis"]}),
    )
    assert result.mapping["pg"] == result.mapping["storage"] == result.mapping["redis"]


def test_a_group_is_filtered_by_its_own_id_not_its_members() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "web"}, {"id": "pg"}, {"id": "redis"}),
        edges=_edges({"from": "web", "to": "pg"}),
        collapse=_groups({"id": "storage", "members": ["pg", "redis"]}),
        exclude=["storage"],
    )
    assert _node_names(doc) == ["web"]
    assert (result.nodes_filtered, result.edges_dropped_filtered) == (1, 1)


def test_a_group_naming_an_undeclared_member_is_refused() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="ghost"):
        ops.add_diagram_graph(
            doc,
            nodes=_nodes({"id": "pg"}),
            edges=[],
            collapse=_groups({"id": "storage", "members": ["pg", "ghost"]}),
        )
    assert not _node_names(doc)


def test_a_node_claimed_by_two_groups_is_refused() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument, match="one group"):
        ops.add_diagram_graph(
            doc,
            nodes=_nodes({"id": "pg"}, {"id": "redis"}),
            edges=[],
            collapse=_groups(
                {"id": "storage", "members": ["pg"]},
                {"id": "caches", "members": ["pg", "redis"]},
            ),
        )
    assert not _node_names(doc)


def test_a_group_may_take_the_id_of_a_member_it_swallows_but_not_of_a_stranger() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "pg"}, {"id": "redis"}),
        edges=[],
        collapse=_groups({"id": "pg", "members": ["pg", "redis"]}),
    )
    assert _node_names(doc) == ["pg"]

    other = _doc()
    with pytest.raises(InvalidArgument, match="does not contain"):
        ops.add_diagram_graph(
            other,
            nodes=_nodes({"id": "web"}, {"id": "pg"}, {"id": "redis"}),
            edges=[],
            collapse=_groups({"id": "web", "members": ["pg", "redis"]}),
        )


def test_a_group_takes_its_first_member_s_place_in_document_order() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}),
        edges=[],
        collapse=_groups({"id": "bc", "members": ["b", "c"]}),
        layout="none",
    )
    assert _node_names(doc) == ["a", "bc", "d"]


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


def test_a_double_colon_fqname_is_split_on_the_double_colon() -> None:
    # Rust, C++, Ruby. The "." convention is not the only one, and an id that uses neither must
    # come through whole rather than being chopped at some character that happens to be in it.
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes(
            {"id": "svg_mcp::ops::graph::add_diagram_graph"},
            {"id": "svg_mcp::model::errors::InvalidArgument"},
        ),
        edges=[],
    )
    assert set(_labels(doc).values()) == {
        "ops::graph::add_diagram_graph",
        "model::errors::InvalidArgument",
    }


def test_a_php_namespace_is_split_on_its_backslash() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": r"App\Models\User"}, {"id": r"App\Http\Controller"}),
        edges=[],
        label_mode="basename",
    )
    assert set(_labels(doc).values()) == {"User", "Controller"}


def test_a_go_package_path_keeps_the_symbol_that_follows_its_dot() -> None:
    # `net/http.Client` is a path AND a dotted symbol, and the dot is not an extension. A
    # "short alphabetic tail" heuristic would caption this box `http`.
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "net/http.Client"}, {"id": "net/url.URL"}),
        edges=[],
        label_mode="basename",
    )
    assert set(_labels(doc).values()) == {"http.Client", "url.URL"}


def test_a_symbol_hanging_off_a_c_plus_plus_file_cuts_at_the_symbol() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes(
            {"id": "src/render/canvas.cpp::Canvas::draw"},
            {"id": "src/render/canvas.cpp::Canvas::clear"},
        ),
        edges=[],
        label_mode="basename",
    )
    assert set(_labels(doc).values()) == {"draw", "clear"}


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


# --- 4b. size: extent as data ------------------------------------------------


def _boxes(doc: Document, result: GraphImport) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for graph_id, node_id in result.mapping.items():
        box = _box_of(doc, node_id)
        if box is not None:
            out[graph_id] = (box.w, box.h)
    return out


def test_size_field_reads_a_key_the_export_already_carries() -> None:
    doc = _doc()
    result = _ingest(doc, size_field="symbols", size_labels=True)
    assert _labels(doc)["src/svg_mcp/ops/diagram.py"] == "diagram (177)"
    assert _labels(doc)["src/svg_mcp/ops/annotate.py"] == "annotate (46)"
    assert result.nodes_created == 3


def test_an_explicit_size_beats_the_field() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a", "symbols": 10, "size": 999}, {"id": "b", "symbols": 20}),
        edges=[],
        size_field="symbols",
        size_labels=True,
    )
    assert set(_labels(doc).values()) == {"a (999)", "b (20)"}


def test_a_size_field_no_node_carries_is_rejected_with_the_keys_they_do() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument) as raised:
        ops.add_diagram_graph(
            doc, nodes=_nodes({"id": "a", "symbols": 10}), edges=[], size_field="symbol"
        )
    assert "symbol" in str(raised.value) and "symbols" in str(raised.value)
    assert not _node_names(doc)


def test_scaling_one_dimension_leaves_the_other_measured_from_the_label() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "small", "size": 1}, {"id": "big", "size": 100}),
        edges=[],
        scale_width=(80.0, 240.0),
    )
    boxes = _boxes(doc, result)
    assert boxes["small"][0] == pytest.approx(80.0)
    assert boxes["big"][0] == pytest.approx(240.0)
    assert boxes["small"][1] == boxes["big"][1]  # height still comes from the label


def test_scaling_both_dimensions_takes_the_root_so_area_carries_the_quantity() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a", "size": 0}, {"id": "b", "size": 25}, {"id": "c", "size": 100}),
        edges=[],
        scale_width=(0.0, 200.0),
        scale_height=(0.0, 100.0),
    )
    boxes = _boxes(doc, result)
    # sqrt(25)/sqrt(100) = 0.5, so the middle node lands halfway up each range, not a quarter.
    assert boxes["b"][0] == pytest.approx(100.0)
    assert boxes["b"][1] == pytest.approx(50.0)


def test_a_scaled_box_never_shrinks_below_what_its_label_needs() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes(
            {"id": "a_very_long_node_name_indeed_that_needs_room", "size": 1},
            {"id": "b", "size": 100},
        ),
        edges=[],
        label_mode="id",
        scale_width=(10.0, 300.0),
    )
    boxes = _boxes(doc, result)
    assert boxes["a_very_long_node_name_indeed_that_needs_room"][0] > 10.0


def test_a_group_takes_the_sum_of_its_members_sizes_unless_it_states_one() -> None:
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "pg", "size": 3}, {"id": "redis", "size": 4}, {"id": "web", "size": 5}),
        edges=[],
        collapse=_groups({"id": "storage", "members": ["pg", "redis"]}),
        size_labels=True,
    )
    assert set(_labels(doc).values()) == {"storage (7)", "web (5)"}

    other = _doc()
    ops.add_diagram_graph(
        other,
        nodes=_nodes({"id": "pg", "size": 3}, {"id": "redis", "size": 4}),
        edges=[],
        collapse=_groups({"id": "storage", "members": ["pg", "redis"], "size": 99}),
        size_labels=True,
    )
    assert _labels(other)["storage"] == "storage (99)"


def test_a_node_with_no_size_is_simply_measured_from_its_label() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "small", "size": 1}, {"id": "big", "size": 100}, {"id": "unsized"}),
        edges=[],
        scale_width=(200.0, 300.0),
        size_labels=True,
    )
    boxes = _boxes(doc, result)
    assert boxes["big"][0] == pytest.approx(300.0)
    assert boxes["small"][0] == pytest.approx(200.0)
    assert boxes["unsized"][0] < 200.0  # measured, not scaled
    assert _labels(doc)["unsized"] == "unsized"  # and nothing to write in the label


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


def test_the_reported_bounds_enclose_everything_the_call_drew() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes(*[{"id": f"pkg/mod{index}.py"} for index in range(6)]),
        edges=_edges({"from": "pkg/mod0.py", "to": "pkg/mod5.py"}),
    )
    assert result.bounds is not None
    x, y, width, height = result.bounds
    for node_id in result.mapping.values():
        box = _box_of(doc, node_id)
        assert box is not None
        assert x <= box.x and y <= box.y
        assert box.x + box.w <= x + width + 1e-6
        assert box.y + box.h <= y + height + 1e-6


def test_a_graph_too_big_for_its_canvas_says_so_in_the_bounds() -> None:
    # The reason bounds exist: nothing here resizes the canvas, so the caller has to be told
    # when the drawing has outgrown it (then `resize_document(mode="fit")`).
    doc = DocumentStore().create(300, 200)[1]
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes(*[{"id": f"pkg/mod{index}.py"} for index in range(12)]),
        edges=[],
    )
    assert result.bounds is not None
    assert result.bounds[1] + result.bounds[3] > 200


def test_a_graph_with_no_nodes_reports_no_bounds() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(doc, nodes=[], edges=[])
    assert result.bounds is None
    assert (result.nodes_created, result.ranks) == (0, None)


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

    def failing(
        doc: Document,
        *,
        source: str,
        target: str,
        kind: str = "data",
        label: str | None = None,
        parent: str | None = None,
        themed: bool = True,
    ) -> ops.PlacedEdge:
        calls["n"] += 1
        if calls["n"] == 2:
            raise InvalidArgument("boom, half way through the edges")
        return real(
            doc,
            source=source,
            target=target,
            kind=kind,
            label=label,
            parent=parent,
            themed=themed,
        )

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


# --- 8. review fixes ---------------------------------------------------------
#
# One test per execution-confirmed finding from the review of the graph-ingest work. Each
# reproduces the exact input that misbehaved, so a regression names the reading that was wrong
# rather than just a number that changed.


def test_magnitudes_that_are_all_at_or_below_zero_scale_uniformly() -> None:
    # The compression exponent lifts every magnitude through `max(n, 0)`, so a span that varies
    # only below zero collapses to a single point AFTER the lift — and the span it was checked
    # against, before it, looked perfectly healthy. That divided by zero.
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a", "size": -5}, {"id": "b", "size": 0}),
        edges=[],
        scale_width=(80.0, 240.0),
    )
    boxes = _boxes(doc, result)
    # No variation a box can carry is no reason to draw a difference: the all-equal answer.
    assert boxes["a"][0] == pytest.approx(80.0)
    assert boxes["b"][0] == pytest.approx(80.0)


def test_an_unthemed_ingest_still_resolves_its_kinds_and_merges_on_the_resolved_one() -> None:
    # `themed=False` withholds the CLASS, not the bookkeeping. Skipping the resolution left the
    # producer's own vocabulary in the specs: two arrows where a themed ingest draws one, and
    # nothing reported about the substitution that silently did not happen.
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a"}, {"id": "b"}),
        edges=_edges(
            {"from": "a", "to": "b", "kind": "calls"},
            {"from": "a", "to": "b", "kind": "imports"},
        ),
        themed=False,
    )
    assert result.edges_created == 1
    assert result.kinds_defaulted == ["calls", "imports"]
    assert {spec[2] for spec in _edge_specs(doc)} == {"data"}


def test_a_ghost_naming_itself_at_both_ends_is_a_hole_not_a_self_edge() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument) as raised:
        ops.add_diagram_graph(
            doc,
            nodes=_nodes({"id": "a"}),
            edges=_edges({"from": "ghost", "to": "ghost"}),
        )
    assert "'ghost'" in str(raised.value)
    assert not _node_names(doc)


def test_a_ghost_opposite_a_filtered_node_is_a_hole_not_a_dangling_edge() -> None:
    # The endpoint the caller excluded is a decision; the typo beside it is still a hole, and
    # counting the edge as filtered absorbed the typo into a number nobody reads as an error.
    doc = _doc()
    with pytest.raises(InvalidArgument) as raised:
        ops.add_diagram_graph(
            doc,
            nodes=_nodes({"id": "a"}, {"id": "b"}),
            edges=_edges({"from": "b", "to": "typoo"}),
            exclude=["b"],
        )
    message = str(raised.value)
    assert "'typoo'" in message and "target" in message
    assert not _node_names(doc)


def test_an_endpoint_that_was_declared_then_filtered_out_is_still_a_silent_drop() -> None:
    # The other half of the same rule: a node that WAS declared and then filtered away leaves a
    # legitimately dangling edge, which is counted rather than raised on.
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a"}, {"id": "b"}),
        edges=_edges({"from": "a", "to": "b"}),
        exclude=["b"],
    )
    assert (result.edges_dropped_filtered, result.edges_created) == (1, 0)


def test_a_mistyped_size_field_is_not_masked_by_a_node_that_states_its_own_size() -> None:
    # The guard asked whether any magnitude came out, and an explicit `size` answers yes — so a
    # size_field naming nothing at all sized the boxes by something the caller never asked for.
    doc = _doc()
    with pytest.raises(InvalidArgument) as raised:
        ops.add_diagram_graph(
            doc,
            nodes=_nodes({"id": "a", "symbols": 10, "size": 3}),
            edges=[],
            size_field="symbol",
        )
    message = str(raised.value)
    assert "no node carries" in message and "symbols" in message
    assert not _node_names(doc)


def test_a_size_field_whose_key_holds_no_numbers_says_that_rather_than_that_it_is_absent() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument) as raised:
        ops.add_diagram_graph(
            doc,
            nodes=_nodes({"id": "a", "symbols": "many"}),
            edges=[],
            size_field="symbols",
        )
    message = str(raised.value)
    assert "carries no numeric values" in message and "'many'" in message
    assert "no node carries" not in message  # the contradictory reading it used to give
    assert not _node_names(doc)


def test_a_flat_filename_id_is_a_filename_not_a_dotted_name() -> None:
    # An export with no directories at all: without a path separator to settle it, the dot read
    # as a hierarchy step and captioned every Python file `py` and every Go file `go`.
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "diagram.py"}, {"id": "themes.py"}, {"id": "main.go"}),
        edges=[],
        label_mode="basename",
    )
    assert list(_labels(doc).values()) == ["diagram", "themes", "main"]

    trimmed = _doc()
    ops.add_diagram_graph(
        trimmed,
        nodes=_nodes({"id": "diagram.py"}, {"id": "themes.py"}, {"id": "main.go"}),
        edges=[],
    )
    assert list(_labels(trimmed).values()) == ["diagram", "themes", "main"]


def test_a_collapse_group_does_not_defeat_the_prefix_the_other_labels_trim() -> None:
    # A group id is the caller's word, sharing no hierarchy with the producer's ids: left in the
    # prefix computation it drove the common prefix to nothing, and collapsing part of a graph
    # silently re-captioned the part it did not touch with its full path.
    doc = _doc()
    ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "pkg/a.py"}, {"id": "pkg/b.py"}, {"id": "pkg/c.py"}),
        edges=[],
        collapse=_groups({"id": "ab", "members": ["pkg/a.py", "pkg/b.py"]}),
    )
    assert _labels(doc) == {"ab": "ab", "pkg/c.py": "c"}


def test_a_producer_size_key_that_is_not_a_number_is_carried_rather_than_rejected() -> None:
    # `size` is a common enough word that an export means bytes by it. That must not fail the
    # validation of the whole graph — nor vanish: size_field can still name it afterwards.
    node = GraphNode.model_validate({"id": "img.png", "size": "12kB"})
    assert node.size is None
    assert (node.model_extra or {})["size"] == "12kB"

    doc = _doc()
    result = ops.add_diagram_graph(
        doc, nodes=[node], edges=[], scale_width=(200.0, 300.0), label_mode="id"
    )
    assert result.nodes_created == 1
    assert _boxes(doc, result)["img.png"][0] < 200.0  # measured from the label, not scaled

    named = _doc()
    with pytest.raises(InvalidArgument, match="carries no numeric values"):
        ops.add_diagram_graph(named, nodes=[node], edges=[], size_field="size")


def test_a_size_spelled_as_a_numeric_string_is_still_a_magnitude() -> None:
    doc = _doc()
    result = ops.add_diagram_graph(
        doc,
        nodes=_nodes({"id": "a", "size": "12"}, {"id": "b", "size": 24}),
        edges=[],
        scale_width=(100.0, 200.0),
        size_labels=True,
    )
    assert _labels(doc)["a"] == "a (12)"
    boxes = _boxes(doc, result)
    assert boxes["a"][0] == pytest.approx(100.0)
    assert boxes["b"][0] == pytest.approx(200.0)
