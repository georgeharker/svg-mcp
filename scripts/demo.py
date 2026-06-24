#!/usr/bin/env python
"""Build a small poster end-to-end and render it to PNG — a quick sanity/experimentation harness.

Run with the project venv:

    .venv/bin/python scripts/demo.py [output.png]

It exercises layers, gradients, named styles, a drop-shadow filter, a star, and text, then
renders via resvg and writes a PNG (default: demo_output.png in the repo root).
"""

from __future__ import annotations

import sys
from pathlib import Path

from svg_mcp import ops
from svg_mcp.render import build_feedback, get_renderer
from svg_mcp.render.base import RenderRequest
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore


def build() -> str:
    doc = DocumentStore().create(640, 360)[1]

    # Background gradient (referenced by name via the @ shorthand).
    ops.define_linear_gradient(
        doc,
        x1=0,
        y1=0,
        x2=1,
        y2=1,
        stops=[(0.0, "#0f172a", 1.0), (1.0, "#3b0764", 1.0)],
        name="bg",
    )
    ops.add_rect(doc, x=0, y=0, width=640, height=360, style={"fill": "@bg"})

    # A content layer with a shadowed card.
    layer = ops.create_layer(doc, name="content")
    card = ops.add_rect(
        doc,
        x=80,
        y=80,
        width=480,
        height=200,
        rx=20,
        parent=layer.id,
        name="card",
        style={"fill": "#f8fafc"},
    )
    ops.apply_drop_shadow(doc, card.id, dx=0, dy=8, blur=12, color="#000", opacity=0.45)

    # A decorative star.
    ops.add_star(
        doc,
        cx=520,
        cy=120,
        outer_radius=34,
        inner_radius=15,
        sides=5,
        parent=layer.id,
        style={"fill": "#f59e0b", "stroke": "#b45309", "stroke-width": "2"},
    )

    # Title + subtitle.
    ops.add_text(
        doc,
        x=120,
        y=170,
        content="svg-mcp",
        parent=layer.id,
        style={
            "fill": "#0f172a",
            "font-size": "56px",
            "font-family": "Helvetica",
            "font-weight": "bold",
        },
    )
    ops.add_text(
        doc,
        x=122,
        y=215,
        content="structured SVG, rendered to see",
        parent=layer.id,
        style={"fill": "#475569", "font-size": "22px", "font-family": "Helvetica"},
    )
    return export_svg(doc)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("demo_output.png")
    svg = build()
    renderer = get_renderer()
    if not renderer.available():
        print("resvg not installed — `brew install resvg`", file=sys.stderr)
        return 1
    result = renderer.render(RenderRequest(svg=svg, scale=2.0))
    feedback = build_feedback(result)
    out.write_bytes(result.png)
    print(f"{feedback.summary}\nwrote {out} ({len(result.png)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
