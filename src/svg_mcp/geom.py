"""Pure-Python path geometry: stroke expansion and parametric shape outlines.

No inkex dependency — each helper takes plain numbers and returns an SVG path ``d`` string.
Generators: ``variable_width_outline`` (Power Stroke), ``squircle_outline`` (corner-smoothed
rounded rect), ``rounded_polygon_outline`` (corner-smoothed N-gon), ``superellipse_outline``
(Lamé curve).

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


def rounded_polygon_outline(
    cx: float,
    cy: float,
    radius: float,
    sides: int,
    corner_radius: float,
    smoothness: float = 0.6,
    start_angle: float = -90.0,
) -> str:
    """Outline a regular N-gon with smoothed (rounded) corners — the squircle idea for N sides.

    A convex regular polygon (``sides`` ≥ 3) inscribed in ``radius``, with each vertex replaced by
    a rounded corner. At every vertex the edges are cut back to tangent points and joined by a cubic
    Bézier; ``smoothness`` in ``[0, 1]`` eases that join from a crisp circular-ish fillet (0) toward
    a softer, more continuous corner (1). ``corner_radius`` is the fillet radius, clamped so
    adjacent corners never collide. ``start_angle`` (degrees) orients the first vertex (−90° = up).

    Args:
        cx, cy: Polygon center.
        radius: Circumradius (center to each vertex).
        sides: Number of sides (≥ 3).
        corner_radius: Corner fillet radius (≥ 0); 0 yields a sharp polygon.
        smoothness: Corner-smoothing fraction in ``[0, 1]`` (clamped).
        start_angle: Angle of the first vertex in degrees (default −90, pointing up).

    Returns:
        An SVG path ``d`` string of the closed rounded polygon.
    """
    if sides < 3:
        raise ValueError("rounded polygon needs at least 3 sides")
    if radius <= 0:
        raise ValueError("rounded polygon radius must be positive")
    if corner_radius < 0:
        raise ValueError("rounded polygon corner_radius must be non-negative")
    smoothness = max(0.0, min(1.0, smoothness))
    verts = [
        (
            cx + radius * math.cos(math.radians(start_angle) + i * 2.0 * math.pi / sides),
            cy + radius * math.sin(math.radians(start_angle) + i * 2.0 * math.pi / sides),
        )
        for i in range(sides)
    ]
    interior = math.pi * (sides - 2) / sides  # interior angle at each vertex
    edge = math.hypot(verts[1][0] - verts[0][0], verts[1][1] - verts[0][1])
    # Largest fillet that fits without adjacent corners overrunning the shared edge.
    max_r = (edge / 2.0) * math.tan(interior / 2.0)
    r = max(0.0, min(corner_radius, max_r))
    inset = r / math.tan(interior / 2.0) if r > 0 else 0.0

    def _toward(a: Point, b: Point) -> Point:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy) or 1.0
        return dx / length, dy / length

    # Per corner: tangent points A (toward prev) and B (toward next), with cubic control points
    # retreating from the vertex by `k`. Smaller k (high smoothness) → softer/flatter corner.
    corners: list[tuple[Point, Point, Point, Point]] = []
    for i in range(sides):
        v = verts[i]
        ux_in, uy_in = _toward(v, verts[(i - 1) % sides])
        ux_out, uy_out = _toward(v, verts[(i + 1) % sides])
        a = (v[0] + ux_in * inset, v[1] + uy_in * inset)
        b = (v[0] + ux_out * inset, v[1] + uy_out * inset)
        k = inset * (1.0 - 0.45 * smoothness)
        c1 = (a[0] - ux_in * k, a[1] - uy_in * k)
        c2 = (b[0] - ux_out * k, b[1] - uy_out * k)
        corners.append((a, c1, c2, b))

    n = _num
    a0 = corners[0][0]
    parts = [f"M {n(a0[0])} {n(a0[1])}"]
    for i in range(sides):
        _a, c1, c2, b = corners[i]
        parts.append(f"C {n(c1[0])} {n(c1[1])} {n(c2[0])} {n(c2[1])} {n(b[0])} {n(b[1])}")
        nxt = corners[(i + 1) % sides][0]
        parts.append(f"L {n(nxt[0])} {n(nxt[1])}")
    parts.append("Z")
    return " ".join(parts)


def superellipse_outline(
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    exponent: float = 4.0,
    samples: int = 128,
) -> str:
    """Outline a Lamé superellipse ``|x/rx|^n + |y/ry|^n = 1`` as a dense closed polyline.

    Unlike the squircle (straight edges + smoothed corners), this is a single continuous curve with
    no edges or corners anywhere — the ``exponent`` n morphs the whole silhouette: ``n=1`` a
    diamond, ``n=2`` an ellipse, ``n≈4`` a classic squircle look, large n approaches a rectangle,
    ``n<1`` a four-pointed astroid. Sampled as ``samples`` line segments (resvg renders smoothly).

    Args:
        cx, cy: Center.
        rx, ry: Semi-axes (both > 0).
        exponent: Lamé exponent n (> 0).
        samples: Number of polyline segments around the curve (≥ 16).

    Returns:
        An SVG path ``d`` string of the closed superellipse.
    """
    if rx <= 0 or ry <= 0:
        raise ValueError("superellipse radii must be positive")
    if exponent <= 0:
        raise ValueError("superellipse exponent must be positive")
    count = max(16, samples)
    power = 2.0 / exponent
    n = _num
    pts: list[Point] = []
    for i in range(count):
        t = 2.0 * math.pi * i / count
        ct, st = math.cos(t), math.sin(t)
        x = cx + rx * math.copysign(abs(ct) ** power, ct)
        y = cy + ry * math.copysign(abs(st) ** power, st)
        pts.append((x, y))
    head = f"M {n(pts[0][0])} {n(pts[0][1])}"
    body = " ".join(f"L {n(x)} {n(y)}" for x, y in pts[1:])
    return f"{head} {body} Z"
