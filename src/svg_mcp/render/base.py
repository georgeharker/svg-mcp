"""Renderer protocol and shared request/result types.

The render path is **serialize-then-rasterize**: the document model is serialized to an SVG
string (the same string we export) and handed to a backend. This guarantees *preview == export*
and keeps backends swappable behind one protocol. See DESIGN.md §11.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class RenderError(RuntimeError):
    """Raised when a backend cannot produce a raster (missing binary, bad SVG, timeout)."""


@dataclass(slots=True)
class RenderRequest:
    """A single rasterization request.

    Exactly one sizing strategy is honored, in priority order: explicit ``width``/``height``
    if given, else ``scale`` applied to the SVG's natural size.
    """

    svg: str
    scale: float = 1.0
    width: int | None = None
    height: int | None = None
    background: str | None = None  # CSS color, or None for transparent
    dpi: float = 96.0


@dataclass(slots=True)
class RenderResult:
    """The product of a render: PNG bytes plus what was actually produced."""

    png: bytes
    width: int
    height: int
    backend: str
    duration_ms: float


@runtime_checkable
class Renderer(Protocol):
    """A rasterization backend. Implementations: resvg (primary), cairo, inkscape."""

    name: str

    def available(self) -> bool:
        """True if this backend can run in the current environment (binary/libs present)."""
        ...

    def render(self, request: RenderRequest) -> RenderResult:
        """Rasterize ``request.svg`` to PNG, or raise :class:`RenderError`."""
        ...
