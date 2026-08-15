"""Regenerate the documentation gallery images (docs/img/facade-*.png and .svg).

Run from the repo root with the project venv:

    .venv/bin/python examples/generate.py

Everything is built through the ops layer with the bundled default theme — no external
theme, no coordinates for the diagram (layout_diagram places it), data-derived charts.
"""

from __future__ import annotations

from pathlib import Path

from svg_mcp import ops
from svg_mcp.model.document import Document
from svg_mcp.ops.chart import (
    BarData,
    DonutData,
    HistogramData,
    LineData,
    PointSeries,
    RadarData,
    ScatterData,
    Series,
    SeriesBand,
    Slice,
    SparklineData,
)
from svg_mcp.ops.graph import GraphEdge, GraphGroup, GraphNode
from svg_mcp.render import get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"


def _emit(doc: Document, name: str, *, scale: float = 2.0) -> None:
    svg = export_svg(doc)
    (OUT / f"{name}.svg").write_text(svg)
    result = get_renderer().render(RenderRequest(svg=svg, scale=scale, background="#ffffff"))
    (OUT / f"{name}.png").write_bytes(result.png)
    print(f"wrote {name}.svg / {name}.png")


def facade_diagram() -> None:
    doc = DocumentStore().create(760, 400)[1]
    nodes = {
        "browser": ops.add_diagram_node(doc, kind="external", label="Browser", name="browser"),
        "gateway": ops.add_diagram_node(doc, kind="service", label="API Gateway", name="gateway"),
        "auth": ops.add_diagram_node(doc, kind="service", label="Auth", name="auth"),
        "billing": ops.add_diagram_node(doc, kind="service", label="Billing", name="billing"),
        "jobs": ops.add_diagram_node(doc, kind="queue", label="Job Queue", name="jobs"),
        "db": ops.add_diagram_node(doc, kind="datastore", label="Postgres", name="db"),
        "stripe": ops.add_diagram_node(doc, kind="external", label="Stripe", name="stripe"),
    }

    def edge(a: str, b: str, kind: str, label: str | None = None) -> None:
        ops.add_diagram_edge(
            doc, source=nodes[a].ref.id, target=nodes[b].ref.id, kind=kind, label=label
        )

    edge("browser", "gateway", "data", "HTTPS")
    edge("gateway", "auth", "control", "verify")
    edge("gateway", "billing", "data")
    edge("billing", "stripe", "dependency", "charge")
    edge("gateway", "jobs", "control", "enqueue")
    edge("auth", "db", "data")
    edge("billing", "db", "data")
    edge("jobs", "db", "dependency")
    ops.add_diagram_container(
        doc,
        members=[nodes[n].ref.id for n in ("gateway", "auth", "billing", "jobs")],
        kind="zone",
        label="backend",
    )
    ops.layout_diagram(doc, algorithm="layered", direction="LR")
    _emit(doc, "facade-diagram")


def facade_charts() -> None:
    doc = DocumentStore().create(1120, 800)[1]

    # --- row 1: grouped bars · a banded line · a waterfall -------------------
    ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["Q1", "Q2", "Q3", "Q4"],
            series=[
                Series(name="2025", values=[42, 55, 48, 71]),
                Series(name="2026", values=[51, 62, 58, 79]),
            ],
        ),
        title="Revenue by quarter",
        y_label="$M",
        x=20,
        y=20,
        width=330,
        height=230,
    )

    def hourly(values: list[float]) -> list[tuple[float, float]]:
        return [(float(hour), value) for hour, value in enumerate(values)]

    ops.add_chart(
        doc,
        kind="line",
        data=LineData(
            series=[
                PointSeries(name="p10", points=hourly([8, 9, 9, 11, 10, 9, 10, 12])),
                PointSeries(name="p50", points=hourly([12, 14, 13, 18, 16, 15, 17, 21])),
                PointSeries(name="p90", points=hourly([22, 31, 27, 48, 35, 30, 36, 44])),
            ],
            points=True,
            marker="circle",
            markevery=2,
            bands=[SeriesBand(between=("p10", "p90"), label="p10–p90")],
        ),
        title="Latency (ms)",
        x_label="hour",
        x=380,
        y=20,
        width=340,
        height=230,
    )
    ops.add_chart(
        doc,
        kind="bar",
        data=BarData(
            categories=["Opening", "New", "Upsell", "Churn", "Refunds"],
            series=[Series(name="ARR", values=[120, 34, 18, -22, -7])],
            waterfall=True,
            total_label="Closing",
            value_labels=True,
        ),
        title="ARR walk ($M)",
        x=750,
        y=20,
        width=350,
        height=230,
    )

    # --- row 2: a histogram · bubbles · a radar ------------------------------
    ops.add_chart(
        doc,
        kind="histogram",
        data=HistogramData(
            values=[
                11,
                13,
                14,
                14,
                15,
                16,
                16,
                17,
                17,
                17,
                18,
                18,
                18,
                19,
                19,
                19,
                19,
                20,
                20,
                20,
                21,
                21,
                21,
                22,
                22,
                23,
                23,
                24,
                25,
                26,
                27,
                29,
                31,
                34,
                38,
                41,
                47,
                55,
                62,
                78,
            ],
            bins=12,
        ),
        title="Render time (ms)",
        x_label="ms",
        y_label="renders",
        x=20,
        y=280,
        width=330,
        height=230,
    )
    ops.add_chart(
        doc,
        kind="scatter",
        data=ScatterData(
            series=[
                PointSeries(
                    name="batch",
                    points=[(1, 3), (2, 5), (3, 4), (5, 8), (6, 7), (8, 11)],
                    sizes=[4, 18, 9, 30, 12, 46],
                ),
                PointSeries(
                    name="stream",
                    points=[(1, 6), (3, 9), (4, 7), (6, 12), (7, 10)],
                    sizes=[22, 8, 15, 38, 6],
                ),
            ],
            marker_scale=(4, 18),
            open=True,
        ),
        title="Cost vs size (area = calls)",
        x=380,
        y=280,
        width=340,
        height=230,
    )
    ops.add_chart(
        doc,
        kind="radar",
        data=RadarData(
            axes=["Speed", "Filters", "Text", "Vector out", "Fonts", "Fidelity"],
            series=[
                Series(name="resvg", values=[9, 8, 8, 5, 7, 9]),
                Series(name="cairo", values=[6, 2, 7, 9, 6, 4]),
            ],
            marker="circle",
            rings=4,
        ),
        title="Backend profile",
        x=750,
        y=280,
        width=350,
        height=230,
    )

    # --- row 3: a donut · a wide sparkline -----------------------------------
    ops.add_chart(
        doc,
        kind="donut",
        data=DonutData(
            slices=[
                Slice(label="API", value=46),
                Slice(label="Web", value=28),
                Slice(label="Jobs", value=17),
                Slice(label="Other", value=9),
            ],
            center_text="46%",
            center_subtext="API",
        ),
        title="Traffic share",
        x=20,
        y=545,
        width=300,
        height=230,
    )
    ops.add_chart(
        doc,
        kind="sparkline",
        data=SparklineData(values=[3, 5, 4, 8, 6, 9, 7, 12, 10, 14, 11, 16], last_point=True),
        x=380,
        y=630,
        width=700,
        height=70,
    )
    _emit(doc, "facade-charts")


def facade_annotate() -> None:
    doc = DocumentStore().create(760, 460)[1]
    api = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=60)
    db = ops.add_diagram_node(doc, kind="datastore", label="Postgres", x=240, y=60)
    ops.add_diagram_edge(doc, source=api.ref.id, target=db.ref.id, kind="data")
    ops.add_callout(
        doc,
        target=db.ref.id,
        text="hot shard — see the runbook before resizing",
        kind="warning",
        side="S",
    )
    ops.add_legend(doc, x=560, y=40, title="Key")
    ops.add_table(
        doc,
        rows=[
            ["api", "p99 latency", "88 ms"],
            ["db", "connections", "412"],
            ["jobs", "queue depth", "1,204"],
        ],
        header=["node", "metric", "value"],
        title="Snapshot",
        x=40,
        y=220,
    )
    ops.add_callout_card(
        doc,
        title="Deploy window",
        body="Schema migration lands Tuesday 09:00 UTC; expect a brief read-only period.",
        kind="info",
        x=400,
        y=240,
        width=280,
    )
    _emit(doc, "facade-annotate")


def facade_architecture() -> None:
    """The project's own module graph, ingested with one `add_diagram_graph` call.

    The tables below are a code-graph export as it comes off the indexer — ids, a `symbols`
    count, `from`/`to` edges with call weights — with the common `src/svg_mcp/` prefix factored
    out for readability. Nothing here places a box: `collapse` says which modules read as one
    thing, `size_field` says how big each one is, and the layout does the rest.
    """
    prefix = "src/svg_mcp/"

    # (id, symbols, kind, label) — kind/label empty where the defaults are right.
    modules: list[tuple[str, int, str, str]] = [
        ("server.py", 246, "external", "server (MCP tools)"),
        ("session.py", 11, "", ""),
        ("preview.py", 32, "", ""),
        ("widget.py", 6, "", ""),
        ("config.py", 10, "", ""),
        ("geom.py", 28, "", ""),
        ("typeset.py", 16, "", ""),
        ("serialize/__init__.py", 1, "", ""),
        ("model/__init__.py", 1, "", ""),
        ("model/document.py", 32, "", ""),
        ("model/errors.py", 6, "", ""),
        ("model/handles.py", 8, "", ""),
        ("ops/annotate.py", 215, "service", "ops/annotate (legend · callout · table)"),
        ("ops/chart.py", 315, "service", "ops/chart"),
        ("ops/construct.py", 66, "service", "ops/construct"),
        ("ops/diagram.py", 178, "service", "ops/diagram (facades · routing)"),
        ("ops/diagram_layout.py", 34, "service", "ops/diagram_layout"),
        ("ops/geometry.py", 7, "", ""),
        ("ops/graph.py", 69, "service", "ops/graph (ingest)"),
        ("ops/layers.py", 5, "", ""),
        ("ops/meta.py", 9, "", ""),
        ("ops/modify.py", 22, "", ""),
        ("ops/pages.py", 5, "", ""),
        ("ops/paint.py", 2, "", ""),
        ("ops/paths.py", 12, "", ""),
        ("ops/resources.py", 114, "service", "ops/resources (styles · defs)"),
        ("ops/themes.py", 79, "service", "ops/themes (residency)"),
        ("query/__init__.py", 1, "", ""),
        ("query/inspect.py", 30, "", ""),
        ("query/outline.py", 11, "", ""),
        ("query/select.py", 3, "", ""),
        ("render/__init__.py", 4, "", ""),
        ("render/base.py", 18, "", ""),
        ("render/cairo.py", 4, "", ""),
        ("render/export.py", 5, "", ""),
        ("render/feedback.py", 8, "", ""),
        ("render/inkscape.py", 5, "", ""),
        ("render/resvg.py", 6, "", ""),
        ("render/resvg_py.py", 5, "", ""),
        ("schemas/__init__.py", 1, "", ""),
        ("schemas/filters.py", 4, "", ""),
        ("schemas/gradients.py", 4, "", ""),
        ("schemas/style.py", 28, "", ""),
        ("theme/__init__.py", 1, "", ""),
        ("theme/css.py", 43, "", ""),
        ("theme/loader.py", 15, "", ""),
        ("theme/model.py", 29, "", ""),
    ]

    # (from, to, weight) — call counts. Edges internal to a collapse group are left out;
    # the ingest would drop them as self-edges anyway.
    calls: list[tuple[str, str, int]] = [
        ("ops/annotate.py", "model/document.py", 26),
        ("ops/annotate.py", "ops/chart.py", 10),
        ("ops/annotate.py", "ops/diagram.py", 49),
        ("ops/annotate.py", "ops/themes.py", 19),
        ("ops/annotate.py", "query/outline.py", 5),
        ("ops/chart.py", "model/document.py", 27),
        ("ops/chart.py", "ops/construct.py", 4),
        ("ops/chart.py", "ops/diagram.py", 21),
        ("ops/chart.py", "ops/paint.py", 1),
        ("ops/chart.py", "ops/resources.py", 2),
        ("ops/chart.py", "ops/themes.py", 6),
        ("ops/chart.py", "query/outline.py", 1),
        ("ops/construct.py", "geom.py", 11),
        ("ops/construct.py", "model/document.py", 49),
        ("ops/construct.py", "ops/geometry.py", 7),
        ("ops/construct.py", "ops/paint.py", 2),
        ("ops/construct.py", "ops/themes.py", 4),
        ("ops/construct.py", "typeset.py", 4),
        ("ops/diagram.py", "model/document.py", 26),
        ("ops/diagram.py", "ops/annotate.py", 1),
        ("ops/diagram.py", "ops/construct.py", 10),
        ("ops/diagram.py", "ops/geometry.py", 2),
        ("ops/diagram.py", "ops/modify.py", 1),
        ("ops/diagram.py", "ops/paint.py", 1),
        ("ops/diagram.py", "ops/resources.py", 3),
        ("ops/diagram.py", "ops/themes.py", 9),
        ("ops/diagram.py", "query/outline.py", 5),
        ("ops/diagram.py", "typeset.py", 1),
        ("ops/diagram_layout.py", "model/document.py", 2),
        ("ops/diagram_layout.py", "ops/diagram.py", 6),
        ("ops/diagram_layout.py", "ops/modify.py", 1),
        ("ops/diagram_layout.py", "ops/themes.py", 1),
        ("ops/diagram_layout.py", "query/outline.py", 1),
        ("ops/geometry.py", "model/document.py", 5),
        ("ops/graph.py", "model/document.py", 3),
        ("ops/graph.py", "ops/diagram.py", 4),
        ("ops/graph.py", "ops/diagram_layout.py", 1),
        ("ops/graph.py", "ops/themes.py", 2),
        ("ops/graph.py", "query/outline.py", 1),
        ("ops/layers.py", "model/document.py", 5),
        ("ops/meta.py", "model/document.py", 4),
        ("ops/modify.py", "model/document.py", 19),
        ("ops/pages.py", "model/document.py", 1),
        ("ops/paint.py", "model/document.py", 1),
        ("ops/paths.py", "model/document.py", 6),
        ("ops/paths.py", "ops/construct.py", 2),
        ("ops/resources.py", "model/document.py", 44),
        ("ops/resources.py", "ops/paint.py", 2),
        ("ops/resources.py", "ops/themes.py", 2),
        ("ops/themes.py", "model/document.py", 8),
        ("ops/themes.py", "ops/diagram.py", 2),
        ("ops/themes.py", "ops/resources.py", 9),
        ("ops/themes.py", "theme/loader.py", 17),
        ("preview.py", "render/export.py", 2),
        ("query/inspect.py", "model/document.py", 5),
        ("query/outline.py", "model/document.py", 2),
        ("query/select.py", "model/document.py", 3),
        ("render/__init__.py", "config.py", 1),
        ("serialize/__init__.py", "model/document.py", 1),
        ("server.py", "model/document.py", 83),
        ("server.py", "ops/annotate.py", 8),
        ("server.py", "ops/chart.py", 2),
        ("server.py", "ops/construct.py", 38),
        ("server.py", "ops/diagram.py", 7),
        ("server.py", "ops/diagram_layout.py", 1),
        ("server.py", "ops/geometry.py", 7),
        ("server.py", "ops/graph.py", 1),
        ("server.py", "ops/layers.py", 4),
        ("server.py", "ops/meta.py", 4),
        ("server.py", "ops/modify.py", 18),
        ("server.py", "ops/pages.py", 4),
        ("server.py", "ops/paths.py", 8),
        ("server.py", "ops/resources.py", 44),
        ("server.py", "ops/themes.py", 8),
        ("server.py", "preview.py", 6),
        ("server.py", "query/inspect.py", 10),
        ("server.py", "query/outline.py", 3),
        ("server.py", "query/select.py", 3),
        ("server.py", "render/__init__.py", 5),
        ("server.py", "render/base.py", 8),
        ("server.py", "render/export.py", 2),
        ("server.py", "render/feedback.py", 3),
        ("server.py", "schemas/style.py", 19),
        ("server.py", "serialize/__init__.py", 7),
        ("server.py", "session.py", 16),
        ("server.py", "typeset.py", 4),
        ("server.py", "widget.py", 1),
        ("session.py", "model/document.py", 7),
        ("theme/css.py", "model/errors.py", 5),
        ("theme/loader.py", "model/errors.py", 5),
    ]

    # (id, label, kind, members) — the judgement calls: what reads as one box.
    grouping: list[tuple[str, str, str, list[str]]] = [
        (
            "model",
            "model (document · handles · errors)",
            "datastore",
            ["model/__init__.py", "model/document.py", "model/errors.py", "model/handles.py"],
        ),
        (
            "theme engine",
            "theme engine (loader · css · model)",
            "datastore",
            ["theme/__init__.py", "theme/css.py", "theme/loader.py", "theme/model.py"],
        ),
        (
            "render",
            "render (resvg · export · feedback)",
            "external",
            [
                "render/__init__.py",
                "render/base.py",
                "render/cairo.py",
                "render/export.py",
                "render/feedback.py",
                "render/inkscape.py",
                "render/resvg.py",
                "render/resvg_py.py",
            ],
        ),
        (
            "query",
            "query (inspect · outline · select)",
            "service",
            [
                "query/__init__.py",
                "query/inspect.py",
                "query/outline.py",
                "query/select.py",
            ],
        ),
        (
            "schemas",
            "schemas (style · filters · gradients)",
            "note",
            [
                "schemas/__init__.py",
                "schemas/filters.py",
                "schemas/gradients.py",
                "schemas/style.py",
            ],
        ),
        (
            "support",
            "support (geom · typeset · session · preview)",
            "note",
            [
                "config.py",
                "geom.py",
                "typeset.py",
                "session.py",
                "serialize/__init__.py",
                "preview.py",
                "widget.py",
            ],
        ),
        (
            "ops utilities",
            "ops utilities (modify · paths · layers · paint …)",
            "service",
            [
                "ops/geometry.py",
                "ops/layers.py",
                "ops/meta.py",
                "ops/modify.py",
                "ops/pages.py",
                "ops/paint.py",
                "ops/paths.py",
            ],
        ),
    ]

    symbols = {path: count for path, count, _, _ in modules}
    nodes = [
        GraphNode.model_validate(
            {"id": prefix + path, "symbols": count}
            | ({"kind": kind} if kind else {})
            | ({"label": label} if label else {})
        )
        for path, count, kind, label in modules
    ]
    edges = [
        GraphEdge.model_validate({"from": prefix + src, "to": prefix + dst, "weight": weight})
        for src, dst, weight in calls
    ]
    # A group's extent is its members' — so the collapsed boxes scale on the same ruler.
    groups = [
        GraphGroup(
            id=group_id,
            label=label,
            kind=kind,
            members=[prefix + member for member in members],
            size=float(sum(symbols[member] for member in members)),
        )
        for group_id, label, kind, members in grouping
    ]

    doc = DocumentStore().create(1400, 1000)[1]
    result = ops.add_diagram_graph(
        doc,
        nodes=nodes,
        edges=edges,
        collapse=groups,
        size_field="symbols",
        scale_width=(120.0, 260.0),
        scale_height=(40.0, 96.0),
        layout="layered",
        direction="TB",
    )
    print(
        f"  graph: {result.nodes_created} nodes ({result.nodes_collapsed} collapsed into "
        f"{result.groups_created}), {result.edges_created} edges, bounds={result.bounds}"
    )
    ops.resize_document(doc, mode="fit", margin=24)
    _emit(doc, "facade-architecture")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    facade_diagram()
    facade_charts()
    facade_annotate()
    facade_architecture()


if __name__ == "__main__":
    main()
