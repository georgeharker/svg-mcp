"""Variable-width stroke expansion (Power Stroke)."""

from __future__ import annotations

import math

import pytest

from svg_mcp import ops
from svg_mcp.geom import variable_width_outline
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore


def _bbox(d: str) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for token in d.replace("M", " ").replace("L", " ").replace("Z", " ").split():
        if "," in token:
            x, y = token.split(",")
            xs.append(float(x))
            ys.append(float(y))
    return min(xs), min(ys), max(xs), max(ys)


def test_constant_width_horizontal_line_is_a_band() -> None:
    # A flat line of constant width 10 should expand to a band ±5 around y=50.
    d = variable_width_outline([(0.0, 50.0), (100.0, 50.0)], [10.0, 10.0])
    x0, y0, x1, y1 = _bbox(d)
    assert (x0, x1) == (0.0, 100.0)
    assert math.isclose(y0, 45.0, abs_tol=0.01)
    assert math.isclose(y1, 55.0, abs_tol=0.01)
    assert d.startswith("M") and d.strip().endswith("Z")


def test_taper_widens_the_band_toward_the_thick_end() -> None:
    # Width 2 -> 20 along x: the ribbon's vertical extent grows toward the end.
    d = variable_width_outline([(0.0, 50.0), (100.0, 50.0)], [2.0, 20.0])
    _x0, y0, _x1, y1 = _bbox(d)
    assert math.isclose(y0, 40.0, abs_tol=0.01)  # half of 20
    assert math.isclose(y1, 60.0, abs_tol=0.01)


def test_round_cap_emits_arcs() -> None:
    d = variable_width_outline([(0.0, 0.0), (50.0, 0.0)], [10.0, 10.0], cap="round")
    assert "A" in d  # arc commands for the semicircular caps


def test_closed_ribbon_is_two_subpaths() -> None:
    square = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    d = variable_width_outline(square, [8.0, 8.0, 8.0, 8.0], closed=True)
    assert d.count("M") == 2  # outer ring + inner ring (annulus)


def test_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        variable_width_outline([(0.0, 0.0), (1.0, 1.0)], [4.0])


def test_cubic_interpolation_densifies_and_stays_in_hull() -> None:
    pts = [(0.0, 0.0), (50.0, 40.0), (100.0, 0.0), (150.0, 40.0)]
    w = [6.0, 6.0, 6.0, 6.0]
    linear = variable_width_outline(pts, w)
    cubic = variable_width_outline(pts, w, interpolation="cubic", samples=8)
    # Far more vertices once smoothed.
    assert cubic.count("L") > linear.count("L") * 4
    # Catmull-Rom passes through the control points, so x-extent ~ input span (± ribbon width).
    x0, _y0, x1, _y1 = _bbox(cubic)
    assert math.isclose(x0, 0.0, abs_tol=5.0) and math.isclose(x1, 150.0, abs_tol=5.0)


def test_bad_interpolation_raises() -> None:
    with pytest.raises(ValueError):
        variable_width_outline([(0.0, 0.0), (1.0, 0.0)], [2.0, 2.0], interpolation="bezier")


def test_op_cubic_interpolation_smooths() -> None:
    _, doc = DocumentStore().create(200, 100)
    ref = ops.add_variable_width_path(
        doc,
        points=[(10.0, 50.0), (70.0, 20.0), (130.0, 80.0), (190.0, 50.0)],
        widths=[2.0, 14.0, 14.0, 2.0],
        interpolation="cubic",
        style={"fill": "#222"},
    )
    assert ref.tag == "path"


def test_op_creates_filled_path_node() -> None:
    _, doc = DocumentStore().create(200, 100)
    ref = ops.add_variable_width_path(
        doc,
        points=[(10.0, 50.0), (100.0, 50.0), (190.0, 50.0)],
        widths=[2.0, 16.0, 2.0],
        name="brush",
        style={"fill": "#101010"},
    )
    assert ref.tag == "path" and ref.name == "brush"
    svg = export_svg(doc)
    assert "<path" in svg and "#101010" in svg


def test_op_closed_sets_evenodd_fill_rule() -> None:
    _, doc = DocumentStore().create(200, 200)
    ref = ops.add_variable_width_path(
        doc,
        points=[(50.0, 50.0), (150.0, 50.0), (150.0, 150.0), (50.0, 150.0)],
        widths=[10.0, 10.0, 10.0, 10.0],
        closed=True,
        style={"fill": "#000000"},
    )
    assert ref.tag == "path"
    assert "evenodd" in export_svg(doc)
