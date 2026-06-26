"""Variable-width stroke expansion (Power Stroke)."""

from __future__ import annotations

import math

import pytest

from svg_mcp import ops
from svg_mcp.geom import (
    offset_cubic_subpath,
    rounded_polygon_outline,
    squircle_outline,
    superellipse_outline,
    variable_width_outline,
)
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore


def _is_num(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


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


def test_round_cap_emits_outward_arcs() -> None:
    d = variable_width_outline([(0.0, 0.0), (50.0, 0.0)], [10.0, 10.0], cap="round")
    assert "A" in d  # arc commands for the semicircular caps
    # Sweep flag 0 → the cap bulges OUTWARD (a pill), not inward (a bite). Regression guard.
    assert "0 0 0" in d and "0 0 1" not in d


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


def test_squircle_is_a_closed_path_spanning_the_box() -> None:
    d = squircle_outline(10.0, 20.0, 120.0, 80.0, 24.0, smoothness=0.6)
    assert d.startswith("M") and d.strip().endswith("Z")
    # The outline touches each edge of the bounding box (corners are inset, edges are not).
    nums = [float(t) for t in d.replace(",", " ").split() if _is_num(t)]
    # M/L coordinates appear as absolute; the box edges 10/20/130/100 must be present.
    assert any(math.isclose(v, 10.0, abs_tol=0.5) for v in nums)  # left x
    assert any(math.isclose(v, 130.0, abs_tol=0.5) for v in nums)  # right x


def test_squircle_smoothing_changes_the_outline() -> None:
    plain = squircle_outline(0.0, 0.0, 100.0, 100.0, 30.0, smoothness=0.0)
    smooth = squircle_outline(0.0, 0.0, 100.0, 100.0, 30.0, smoothness=1.0)
    assert plain != smooth  # smoothing actually alters the geometry


def test_squircle_radius_clamped_to_box() -> None:
    # A huge radius can't overrun the box; the path must still be valid and finite.
    d = squircle_outline(0.0, 0.0, 40.0, 40.0, 1000.0, smoothness=0.6)
    assert d.startswith("M") and d.strip().endswith("Z")
    assert "nan" not in d.lower() and "inf" not in d.lower()


def test_squircle_rejects_nonpositive_size() -> None:
    with pytest.raises(ValueError):
        squircle_outline(0.0, 0.0, 0.0, 50.0, 10.0)


def test_rounded_polygon_is_closed_with_smoothed_corners() -> None:
    d = rounded_polygon_outline(60.0, 60.0, 50.0, 6, 14.0, smoothness=0.6)
    assert d.startswith("M") and d.strip().endswith("Z")
    assert "C" in d  # corners are cubic Béziers, not sharp


def test_rounded_polygon_sides_change_vertex_count() -> None:
    tri = rounded_polygon_outline(50.0, 50.0, 40.0, 3, 10.0)
    hexa = rounded_polygon_outline(50.0, 50.0, 40.0, 6, 10.0)
    assert hexa.count("C") == 2 * tri.count("C")  # one corner cubic per side


def test_rounded_polygon_rejects_too_few_sides() -> None:
    with pytest.raises(ValueError):
        rounded_polygon_outline(0.0, 0.0, 10.0, 2, 1.0)


def test_superellipse_exponent_morphs_silhouette() -> None:
    diamond = superellipse_outline(0.0, 0.0, 50.0, 50.0, 1.0, samples=64)
    squircle = superellipse_outline(0.0, 0.0, 50.0, 50.0, 4.0, samples=64)
    assert diamond.startswith("M") and diamond.strip().endswith("Z")
    assert diamond != squircle  # the exponent changes the curve


def test_superellipse_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        superellipse_outline(0.0, 0.0, 0.0, 50.0, 4.0)
    with pytest.raises(ValueError):
        superellipse_outline(0.0, 0.0, 50.0, 50.0, 0.0)


def _line_cubic(
    a: tuple[float, float], b: tuple[float, float]
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]:
    return (
        a,
        (a[0] + (b[0] - a[0]) / 3, a[1] + (b[1] - a[1]) / 3),
        (a[0] + 2 * (b[0] - a[0]) / 3, a[1] + 2 * (b[1] - a[1]) / 3),
        b,
    )


def _coords(d: str) -> list[float]:
    cleaned = d.replace("M", " ").replace("C", " ").replace("L", " ").replace("A", " ")
    cleaned = cleaned.replace("Z", " ").replace(",", " ")
    return [float(t) for t in cleaned.split() if _is_num(t)]


def test_offset_closed_square_grows_outward() -> None:
    sq = [
        _line_cubic((0.0, 0.0), (100.0, 0.0)),
        _line_cubic((100.0, 0.0), (100.0, 100.0)),
        _line_cubic((100.0, 100.0), (0.0, 100.0)),
        _line_cubic((0.0, 100.0), (0.0, 0.0)),
    ]
    d = offset_cubic_subpath(sq, 10.0, closed=True, join="miter")
    assert d.startswith("M") and d.strip().endswith("Z")
    nums = _coords(d)
    assert min(nums) < -5.0 and max(nums) > 105.0  # positive distance grows a closed shape


def test_offset_open_path_is_not_closed() -> None:
    seg = [_line_cubic((0.0, 0.0), (100.0, 0.0))]
    d = offset_cubic_subpath(seg, 10.0, closed=False)
    assert d.startswith("M") and "Z" not in d


def test_offset_drops_degenerate_seam_segment() -> None:
    # A zero-length closing segment (as a Z adds) must not leave an un-offset spur at the seam.
    sq = [
        _line_cubic((0.0, 0.0), (100.0, 0.0)),
        _line_cubic((100.0, 0.0), (0.0, 0.0)),
        ((0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)),  # degenerate
    ]
    d = offset_cubic_subpath(sq, 8.0, closed=True)
    # the original seam point (0,0) must not appear un-offset in the output
    assert " 0,0 " not in f" {d.replace('M', '').replace('L', '').replace('C', '')} "


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
