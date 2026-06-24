"""Pure-Python text-to-path: glyph outlines via fontTools, no external tools.

Finds a system font by family/weight (scanning the standard font directories and reading each
font's name table), shapes a string with simple per-character advances (cmap + hmtx — good for
Latin; no kerning/ligatures/complex scripts), and bakes the glyph outlines into a single SVG
path `d` in user units (font y-up flipped to SVG y-down, scaled by font-size, positioned at
the text's x/y and text-anchor).
"""

from __future__ import annotations

import contextlib
import functools
import glob
import os
from collections.abc import Callable

from fontTools.misc.transform import Transform
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTCollection, TTFont

# A path sampler: given an arc-length distance, return (x, y, tangent_radians) or None past the end.
PathSampler = Callable[[float], tuple[float, float, float] | None]

_FONT_DIRS = (
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
)

_BOLD_WEIGHTS = {"bold", "600", "700", "800", "900", "bolder"}


class FontNotFound(LookupError):
    """No system font matched the requested family."""


def _faces(path: str) -> list[TTFont]:
    if path.lower().endswith(".ttc"):
        return list(TTCollection(path).fonts)
    return [TTFont(path, fontNumber=0, lazy=True)]


# One scan record: (display_family, family_lower, bold, italic, path, font_number).
_Record = tuple[str, str, bool, bool, str, int]


@functools.lru_cache(maxsize=1)
def _scan_fonts() -> tuple[_Record, ...]:
    """Scan the system font directories once and return a record per font face."""
    records: list[_Record] = []
    for directory in _FONT_DIRS:
        if not os.path.isdir(directory):
            continue
        for ext in ("*.ttc", "*.ttf", "*.otf"):
            for path in glob.glob(os.path.join(directory, ext)):
                try:
                    faces = _faces(path)
                except Exception:
                    continue
                try:
                    for number, face in enumerate(faces):
                        try:
                            display = face["name"].getDebugName(1) or ""
                            sub = (face["name"].getDebugName(2) or "").lower()
                        except Exception:
                            continue
                        if not display:
                            continue
                        records.append(
                            (display, display.lower(), "bold" in sub,
                             "italic" in sub or "oblique" in sub, path, number)
                        )
                finally:
                    for face in faces:
                        with contextlib.suppress(Exception):
                            face.close()
    return tuple(records)


def _font_index() -> dict[tuple[str, bool, bool], tuple[str, int]]:
    """Map (family_lower, bold, italic) -> (font_path, font_number)."""
    index: dict[tuple[str, bool, bool], tuple[str, int]] = {}
    for _display, family, bold, italic, path, number in _scan_fonts():
        index.setdefault((family, bold, italic), (path, number))
    return index


def list_font_families() -> list[str]:
    """Return the sorted, de-duplicated proper-case family names available on this system."""
    return sorted({display for display, *_rest in _scan_fonts()}, key=str.lower)


def _load_font(family: str, bold: bool, italic: bool) -> TTFont:
    index = _font_index()
    family_lower = family.lower()
    for key in (
        (family_lower, bold, italic),
        (family_lower, bold, False),
        (family_lower, False, italic),
        (family_lower, False, False),
    ):
        match = index.get(key)
        if match is not None:
            path, number = match
            return TTFont(path, fontNumber=number)
    raise FontNotFound(f"font family {family!r} not found on this system")


def glyph_run(
    text: str,
    *,
    font_family: str,
    font_size: float,
    bold: bool = False,
    italic: bool = False,
    x: float = 0.0,
    y: float = 0.0,
) -> tuple[str, float]:
    """Outline one run starting (text-anchor 'start') at (x, y).

    Returns ``(path_d, advance_width)`` in user units — the baked SVG path data and how far the
    run advanced, so a caller can place subsequent runs.
    """
    font = _load_font(font_family, bold, italic)
    scale = font_size / font["head"].unitsPerEm
    cmap = font.getBestCmap()
    glyph_set = font.getGlyphSet()
    metrics = font["hmtx"].metrics

    pen = SVGPathPen(glyph_set)
    cursor = 0.0
    for char in text:
        gname = cmap.get(ord(char), ".notdef")
        # matrix: scale + flip Y (font is y-up), translate to the pen position and text origin
        glyph_set[gname].draw(TransformPen(pen, (scale, 0.0, 0.0, -scale, x + cursor * scale, y)))
        cursor += metrics[gname][0] if gname in metrics else 0.0
    return str(pen.getCommands()), cursor * scale


def text_to_path_d(
    text: str,
    *,
    font_family: str,
    font_size: float,
    bold: bool = False,
    italic: bool = False,
    x: float = 0.0,
    y: float = 0.0,
    text_anchor: str = "start",
) -> str:
    """Outline a single run honoring text-anchor; returns the baked path ``d`` (user units)."""
    _measure_d, width = glyph_run(
        text, font_family=font_family, font_size=font_size, bold=bold, italic=italic
    )
    shift = {"middle": -width / 2.0, "end": -width}.get(text_anchor, 0.0)
    path_d, _width = glyph_run(
        text,
        font_family=font_family,
        font_size=font_size,
        bold=bold,
        italic=italic,
        x=x + shift,
        y=y,
    )
    return path_d


def text_on_path_d(
    text: str,
    *,
    font_family: str,
    font_size: float,
    bold: bool = False,
    italic: bool = False,
    sampler: PathSampler,
    start_offset: float = 0.0,
) -> str:
    """Outline ``text`` following a path (arc-length glyph walk).

    Each glyph's horizontal midpoint is placed at its cumulative advance distance along the
    path (via ``sampler``) and rotated to the tangent there; glyphs past the path end are
    dropped. Ported from the resvg/usvg text-on-path layout (MIT).
    """
    font = _load_font(font_family, bold, italic)
    scale = font_size / font["head"].unitsPerEm
    cmap = font.getBestCmap()
    glyph_set = font.getGlyphSet()
    metrics = font["hmtx"].metrics

    pen = SVGPathPen(glyph_set)
    distance = start_offset
    for char in text:
        gname = cmap.get(ord(char), ".notdef")
        advance = (metrics[gname][0] if gname in metrics else 0.0) * scale
        sample = sampler(distance + advance / 2.0)  # map the glyph's center onto the curve
        if sample is not None:
            px, py, angle = sample
            # place: to curve point, rotate to tangent, center the glyph, then scale + flip Y
            matrix = (
                Transform()
                .translate(px, py)
                .rotate(angle)
                .translate(-advance / 2.0, 0.0)
                .scale(scale, -scale)
            )
            glyph_set[gname].draw(TransformPen(pen, matrix))
        distance += advance
    return str(pen.getCommands())


def parse_font_size(value: str | None, default: float = 16.0) -> float:
    """Parse an SVG font-size like '78px' or '24' into a pixel float."""
    if not value:
        return default
    text = value.strip().lower()
    for unit in ("px", "pt", "em", "%"):
        if text.endswith(unit):
            text = text[: -len(unit)].strip()
            break
    try:
        return float(text)
    except ValueError:
        return default


def is_bold(font_weight: str | None) -> bool:
    return (font_weight or "").strip().lower() in _BOLD_WEIGHTS
