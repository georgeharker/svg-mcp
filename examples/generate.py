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
    LineData,
    PointSeries,
    ScatterData,
    Series,
    Slice,
    SparklineData,
)
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
    doc = DocumentStore().create(720, 480)[1]
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
        width=340,
        height=210,
    )
    ops.add_chart(
        doc,
        kind="line",
        data=LineData(
            series=[
                PointSeries(
                    name="p50", points=[(0, 12), (1, 14), (2, 13), (3, 18), (4, 16), (5, 15)]
                ),
                PointSeries(
                    name="p99", points=[(0, 40), (1, 52), (2, 47), (3, 88), (4, 61), (5, 55)]
                ),
            ],
            points=True,
            area=True,
        ),
        title="Latency (ms)",
        x_label="hour",
        x=380,
        y=20,
        width=320,
        height=210,
    )
    ops.add_chart(
        doc,
        kind="donut",
        data=DonutData(
            slices=[
                Slice(label="API", value=46),
                Slice(label="Web", value=28),
                Slice(label="Jobs", value=17),
                Slice(label="Other", value=9),
            ]
        ),
        title="Traffic share",
        x=20,
        y=250,
        width=220,
        height=200,
    )
    ops.add_chart(
        doc,
        kind="scatter",
        data=ScatterData(
            series=[
                PointSeries(name="batch", points=[(1, 3), (2, 5), (3, 4), (5, 8), (6, 7), (8, 11)]),
                PointSeries(name="stream", points=[(1, 6), (3, 9), (4, 7), (6, 12), (7, 10)]),
            ]
        ),
        title="Cost vs size",
        x=300,
        y=250,
        width=280,
        height=200,
    )
    ops.add_chart(
        doc,
        kind="sparkline",
        data=SparklineData(values=[3, 5, 4, 8, 6, 9, 7, 12, 10, 14]),
        x=620,
        y=330,
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


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    facade_diagram()
    facade_charts()
    facade_annotate()


if __name__ == "__main__":
    main()
