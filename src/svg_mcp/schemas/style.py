"""Pydantic schemas for styling input — validated paint/colors and a presentation bundle.

These are the tool/wire contract: they validate at the boundary and convert to a plain
``dict[str, str]`` of SVG presentation properties for the ops layer (which stays
inkex-facing and schema-agnostic).
"""

from __future__ import annotations

from typing import Annotated, Literal

import inkex
from pydantic import AfterValidator, BaseModel, Field

_PAINT_KEYWORDS = {"none", "currentColor", "transparent"}


def _validate_color(value: str) -> str:
    """Accept a CSS color, the ``none``/``currentColor`` keywords, or a paint reference.

    References (``url(#id)`` or our ``@name`` shorthand) pass through unresolved; literal
    colors are validated by attempting to parse them with inkex's color parser.
    """
    if value in _PAINT_KEYWORDS or value.startswith(("url(", "@")):
        return value
    try:
        inkex.Color(value)
    except Exception as exc:  # inkex raises ColorError subclasses
        raise ValueError(f"invalid color {value!r}: {exc}") from exc
    return value


Color = Annotated[str, AfterValidator(_validate_color)]
"""A validated paint value: a CSS color, a keyword, or a ``url(#id)`` / ``@name`` reference."""


class ShapeStyle(BaseModel):
    """A presentation-attribute bundle for a shape, validated then flattened to a style dict."""

    fill: Color | None = None
    stroke: Color | None = None
    stroke_width: float | None = Field(default=None, ge=0)
    opacity: float | None = Field(default=None, ge=0, le=1)
    fill_opacity: float | None = Field(default=None, ge=0, le=1)
    stroke_opacity: float | None = Field(default=None, ge=0, le=1)
    stroke_dasharray: str | None = None
    stroke_linecap: Literal["butt", "round", "square"] | None = None
    stroke_linejoin: Literal["miter", "round", "bevel"] | None = None
    # Typography (apply to text/tspan/textPath; ignored by other shapes).
    font_family: str | None = None
    font_size: str | None = None  # e.g. "80px", "2em", "80"
    font_weight: str | None = None  # e.g. "bold", "400", "700"
    font_style: Literal["normal", "italic", "oblique"] | None = None
    text_anchor: Literal["start", "middle", "end"] | None = None
    letter_spacing: str | None = None
    word_spacing: str | None = None
    text_decoration: str | None = None
    # Vertical text alignment, e.g. "middle"/"central"/"hanging" (renderer support varies).
    dominant_baseline: str | None = None
    # Paint order, e.g. "stroke fill" to draw the stroke behind the fill.
    paint_order: str | None = None

    def to_style_dict(self) -> dict[str, str]:
        """Render to SVG presentation properties (omitting unset fields)."""
        mapping: dict[str, str | float | None] = {
            "fill": self.fill,
            "stroke": self.stroke,
            "stroke-width": self.stroke_width,
            "opacity": self.opacity,
            "fill-opacity": self.fill_opacity,
            "stroke-opacity": self.stroke_opacity,
            "stroke-dasharray": self.stroke_dasharray,
            "stroke-linecap": self.stroke_linecap,
            "stroke-linejoin": self.stroke_linejoin,
            "font-family": self.font_family,
            "font-size": self.font_size,
            "font-weight": self.font_weight,
            "font-style": self.font_style,
            "text-anchor": self.text_anchor,
            "letter-spacing": self.letter_spacing,
            "word-spacing": self.word_spacing,
            "text-decoration": self.text_decoration,
            "dominant-baseline": self.dominant_baseline,
            "paint-order": self.paint_order,
        }
        return {key: str(value) for key, value in mapping.items() if value is not None}
