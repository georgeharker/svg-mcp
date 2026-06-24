"""Rendering subsystem: a swappable :class:`Renderer` protocol and its backends.

Render is serialize-then-rasterize (DESIGN.md §11): the model is serialized to SVG, then a
backend turns that SVG into PNG. resvg is the default; cairo and inkscape are declared stubs.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import get_settings
from .base import Renderer, RenderError, RenderRequest, RenderResult
from .cairo import CairoRenderer
from .export import SUPPORTED_FORMATS, export_bytes, rsvg_available
from .feedback import Feedback, build_feedback, downscale_png
from .inkscape import InkscapeRenderer
from .resvg import ResvgCliRenderer

# Map of backend name -> zero-arg factory. Typing the values as factories (rather than
# type[Renderer]) keeps it precise while sidestepping protocol-instantiation concerns.
_BACKENDS: dict[str, Callable[[], Renderer]] = {
    "resvg": ResvgCliRenderer,
    "cairo": CairoRenderer,
    "inkscape": InkscapeRenderer,
}


def get_renderer(name: str | None = None) -> Renderer:
    """Instantiate a render backend by name (default from settings)."""
    name = name or get_settings().renderer
    try:
        cls = _BACKENDS[name]
    except KeyError:
        raise RenderError(f"unknown renderer {name!r}; choices: {sorted(_BACKENDS)}") from None
    return cls()


def available_backends() -> dict[str, bool]:
    """Map each backend name to whether it can run in this environment."""
    return {name: cls().available() for name, cls in _BACKENDS.items()}


__all__ = [
    "Renderer",
    "RenderRequest",
    "RenderResult",
    "RenderError",
    "Feedback",
    "build_feedback",
    "downscale_png",
    "get_renderer",
    "available_backends",
    "export_bytes",
    "SUPPORTED_FORMATS",
    "rsvg_available",
]
