"""Pure-Python path geometry: variable-width stroke expansion and the squircle outline.

No inkex dependency — each helper takes plain numbers and returns an SVG path ``d`` string.

Variable-width stroke expansion (a.k.a. Power Stroke):
SVG has no native variable ``stroke-width`` — it is constant per element. To draw a line that
swells and tapers (calligraphy, engraving, a brush stroke, a tapered arrow), you expand the
centerline into the FILLED outline of the swept ribbon and fill it. This module does that
purely (no inkex): given a polyline and a per-vertex width, it returns the ribbon's path ``d``.

The centerline is offset by ±half-width along the per-vertex normal. At interior vertices the
normal is the average of the two adjoining segment normals, scaled by a miter factor so the
perpendicular thickness is preserved through bends (clamped to an SVG-like miter limit).
"""

from __future__ import annotations

import math

Point = tuple[float, float]

_MITER_LIMIT = 4.0


def _unit(dx: float, dy: float) -> tuple[float, float]:
    length = math.hypot(dx, dy)
    if length == 0.0:
        return 0.0, 0.0
    return dx / length, dy / length


def _vertex_frames(points: list[Point], *, closed: bool) -> list[tuple[float, float, float]]:
    """Per-vertex (normal_x, normal_y, miter_scale)."""
    n = len(points)
    frames: list[tuple[float, float, float]] = []
    for i in range(n):
        if closed:
            prev_i, next_i = (i - 1) % n, (i + 1) % n
            d1 = _unit(points[i][0] - points[prev_i][0], points[i][1] - points[prev_i][1])
            d2 = _unit(points[next_i][0] - points[i][0], points[next_i][1] - points[i][1])
        elif i == 0:
            d2 = _unit(points[1][0] - points[0][0], points[1][1] - points[0][1])
            d1 = d2
        elif i == n - 1:
            d1 = _unit(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1])
            d2 = d1
        else:
            d1 = _unit(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1])
            d2 = _unit(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
        tx, ty = _unit(d1[0] + d2[0], d1[1] + d2[1])
        if tx == 0.0 and ty == 0.0:  # 180° reversal — fall back to the outgoing segment
            tx, ty = d2
        nx, ny = -ty, tx
        seg_nx, seg_ny = -d2[1], d2[0]  # a segment normal, to measure the bend
        cos_phi = abs(nx * seg_nx + ny * seg_ny)
        miter = 1.0 / cos_phi if cos_phi > 1.0 / _MITER_LIMIT else _MITER_LIMIT
        frames.append((nx, ny, miter))
    return frames


def _fmt(p: Point) -> str:
    return f"{p[0]:.2f},{p[1]:.2f}"


# One sample carries the centerline position and the width at that station: (x, y, w).
_Sample = tuple[float, float, float]


def _catmull_rom(p0: _Sample, p1: _Sample, p2: _Sample, p3: _Sample, t: float) -> _Sample:
    """Catmull-Rom interpolation of (x, y, w) between p1 and p2 at parameter t in [0, 1]."""
    t2, t3 = t * t, t * t * t
    out = []
    for a, b, c, d in zip(p0, p1, p2, p3, strict=True):
        out.append(
            0.5
            * (
                (2.0 * b)
                + (-a + c) * t
                + (2.0 * a - 5.0 * b + 4.0 * c - d) * t2
                + (-a + 3.0 * b - 3.0 * c + d) * t3
            )
        )
    return out[0], out[1], out[2]


def _smooth_centerline(
    points: list[Point], widths: list[float], *, closed: bool, samples: int
) -> tuple[list[Point], list[float]]:
    """Resample the centerline AND widths through a cubic (Catmull-Rom) spline.

    Both the position and the width are interpolated on the same parameterization, so the
    ribbon's swell follows the smoothed curve. Widths are clamped to ≥ 0 (cubic can undershoot).
    """
    stations: list[_Sample] = [(p[0], p[1], w) for p, w in zip(points, widths, strict=True)]
    n = len(stations)

    def station(i: int) -> _Sample:
        return stations[i % n] if closed else stations[min(max(i, 0), n - 1)]

    dense: list[_Sample] = []
    segments = n if closed else n - 1
    for i in range(segments):
        p0, p1, p2, p3 = station(i - 1), station(i), station(i + 1), station(i + 2)
        for s in range(samples):
            dense.append(_catmull_rom(p0, p1, p2, p3, s / samples))
    if not closed:
        dense.append(stations[-1])
    return [(x, y) for x, y, _w in dense], [max(0.0, w) for _x, _y, w in dense]


def variable_width_outline(
    points: list[Point],
    widths: list[float],
    *,
    closed: bool = False,
    cap: str = "butt",
    interpolation: str = "linear",
    samples: int = 8,
) -> str:
    """Expand a polyline ``points`` with per-vertex ``widths`` into a filled ribbon path ``d``.

    Args:
        points: Centerline vertices, length n ≥ 2.
        widths: Full stroke width at each vertex (same length as points).
        closed: Treat the centerline as a loop, producing an annular ribbon (fill-rule
            ``evenodd``); otherwise an open ribbon with end caps.
        cap: End cap for open ribbons — ``butt`` (flat) or ``round`` (semicircular).
        interpolation: ``linear`` (straight segments between vertices) or ``cubic`` (a
            Catmull-Rom spline through the vertices, smoothing both the path and the width).
        samples: Sub-segments per span when interpolation is ``cubic`` (higher = smoother).

    Returns:
        An SVG path ``d`` string of the ribbon outline, to be filled (not stroked).
    """
    if len(points) < 2:
        raise ValueError("variable_width_outline needs at least 2 points")
    if len(widths) != len(points):
        raise ValueError("widths must have the same length as points")
    if interpolation == "cubic":
        points, widths = _smooth_centerline(points, widths, closed=closed, samples=max(2, samples))
    elif interpolation != "linear":
        raise ValueError(f"interpolation must be 'linear' or 'cubic', got {interpolation!r}")
    n = len(points)
    frames = _vertex_frames(points, closed=closed)
    left: list[Point] = []
    right: list[Point] = []
    for i in range(n):
        nx, ny, miter = frames[i]
        h = (widths[i] / 2.0) * miter
        left.append((points[i][0] + nx * h, points[i][1] + ny * h))
        right.append((points[i][0] - nx * h, points[i][1] - ny * h))

    if closed:
        outer = "M" + " L".join(_fmt(p) for p in left) + " Z"
        inner = "M" + " L".join(_fmt(p) for p in right) + " Z"
        return outer + " " + inner

    half0, halfn = widths[0] / 2.0, widths[-1] / 2.0
    parts = ["M" + _fmt(left[0])]
    parts += ["L" + _fmt(p) for p in left[1:]]
    # Sweep flag 0 bulges the cap OUTWARD (a convex semicircle past the endpoint); the path
    # always walks the left edge forward then the right edge back, so this orientation is fixed.
    if cap == "round" and halfn > 0:
        parts.append(f"A{halfn:.2f},{halfn:.2f} 0 0 0 {_fmt(right[-1])}")
    else:
        parts.append("L" + _fmt(right[-1]))
    parts += ["L" + _fmt(p) for p in reversed(right[:-1])]
    if cap == "round" and half0 > 0:
        parts.append(f"A{half0:.2f},{half0:.2f} 0 0 0 {_fmt(left[0])}")
    parts.append("Z")
    return " ".join(parts)


def _num(value: float) -> str:
    """Compact fixed-point number for a path ``d`` (trailing zeros trimmed)."""
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def squircle_outline(
    x: float,
    y: float,
    width: float,
    height: float,
    radius: float,
    smoothness: float = 0.6,
) -> str:
    """Outline a SQUIRCLE — a rounded rectangle whose corners are smoothed superellipse fillets.

    This is the iOS / Figma "corner smoothing" rounded rectangle (Apple's continuous corners):
    straight edges joined by corners that ease into the arc with cubic Béziers instead of meeting
    the circular arc abruptly. ``smoothness`` is the corner-smoothing fraction in ``[0, 1]``:
    ``0`` is a plain circular-corner rounded rect, ``~0.6`` matches Apple's app-icon squircle, and
    ``1`` is maximally smooth. See https://www.figma.com/blog/desperately-seeking-squircles/.

    Args:
        x, y: Top-left corner of the bounding box.
        width, height: Box size (both > 0).
        radius: Corner radius (≥ 0); ``0`` yields a plain rectangle. Clamped per corner so a
            smoothed corner never overruns half the shorter side.
        smoothness: Corner-smoothing fraction in ``[0, 1]`` (clamped).

    Returns:
        An SVG path ``d`` string of the closed squircle outline.
    """
    if width <= 0 or height <= 0:
        raise ValueError("squircle width and height must be positive")
    if radius < 0:
        raise ValueError("squircle radius must be non-negative")
    smoothness = max(0.0, min(1.0, smoothness))
    budget = min(width, height) / 2.0
    # Shrink the radius (never the smoothing) if a smoothed corner would overrun the budget, so
    # the Bézier control distances below stay consistent and non-negative.
    r = max(0.0, min(radius, budget / (1.0 + smoothness)))

    # Corner construction, per Figma's "desperately seeking squircles": p is how far from the
    # corner (along each edge) the smoothing begins; a/b/c/d are the cubic control offsets that
    # ease the straight edge into the central circular arc of length `arc` × `arc`.
    p = (1.0 + smoothness) * r
    arc_measure = 90.0 * (1.0 - smoothness)  # central arc spans 90° at s=0, shrinks toward 0
    arc = math.sin(math.radians(arc_measure / 2.0)) * r * math.sqrt(2.0)
    angle_alpha = (90.0 - arc_measure) / 2.0
    p3_p4 = r * math.tan(math.radians(angle_alpha / 2.0))
    angle_beta = 45.0 * smoothness
    c = p3_p4 * math.cos(math.radians(angle_beta))
    d = c * math.tan(math.radians(angle_beta))
    b = (p - arc - c - d) / 3.0
    a = 2.0 * b
    n = _num

    # Four corners as relative segments, walking clockwise from the top edge. Each corner is
    # cubic-in → quarter arc → cubic-out; the arc sweep flag 1 curves outward (convex).
    top_right = (
        f"c {n(a)} 0 {n(a + b)} 0 {n(a + b + c)} {n(d)} "
        f"a {n(r)} {n(r)} 0 0 1 {n(arc)} {n(arc)} "
        f"c {n(d)} {n(c)} {n(d)} {n(b + c)} {n(d)} {n(a + b + c)}"
    )
    bottom_right = (
        f"c 0 {n(a)} 0 {n(a + b)} {n(-d)} {n(a + b + c)} "
        f"a {n(r)} {n(r)} 0 0 1 {n(-arc)} {n(arc)} "
        f"c {n(-c)} {n(d)} {n(-(b + c))} {n(d)} {n(-(a + b + c))} {n(d)}"
    )
    bottom_left = (
        f"c {n(-a)} 0 {n(-(a + b))} 0 {n(-(a + b + c))} {n(-d)} "
        f"a {n(r)} {n(r)} 0 0 1 {n(-arc)} {n(-arc)} "
        f"c {n(-d)} {n(-c)} {n(-d)} {n(-(b + c))} {n(-d)} {n(-(a + b + c))}"
    )
    top_left = (
        f"c 0 {n(-a)} 0 {n(-(a + b))} {n(d)} {n(-(a + b + c))} "
        f"a {n(r)} {n(r)} 0 0 1 {n(arc)} {n(-arc)} "
        f"c {n(c)} {n(-d)} {n(b + c)} {n(-d)} {n(a + b + c)} {n(-d)}"
    )
    return (
        f"M {n(x + width - p)} {n(y)} {top_right} "
        f"L {n(x + width)} {n(y + height - p)} {bottom_right} "
        f"L {n(x + p)} {n(y + height)} {bottom_left} "
        f"L {n(x)} {n(y + p)} {top_left} Z"
    )
