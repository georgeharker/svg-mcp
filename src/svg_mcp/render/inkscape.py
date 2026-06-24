"""Headless Inkscape render backend — optional power tier (reference fidelity + heavy ops).

Planned (DESIGN.md §11.4): drive a long-lived ``inkscape --shell`` process (D-Bus is
unavailable on macOS) for reference-quality rasterization and operations no pure-Python path
gives cheaply (boolean ops, path effects, trace). Heavy dependency; not the live-edit engine.

Stub for now — declares the backend so it's registered and discoverable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import get_settings
from .base import RenderError, RenderRequest, RenderResult

_MACOS_DEFAULT = "/Applications/Inkscape.app/Contents/MacOS/inkscape"


class InkscapeRenderer:
    """Render via the Inkscape CLI. Not yet implemented."""

    name = "inkscape"

    def __init__(self, binary: str | None = None) -> None:
        settings = get_settings()
        self._binary = (
            binary
            or settings.inkscape_binary
            or shutil.which("inkscape")
            or (_MACOS_DEFAULT if Path(_MACOS_DEFAULT).exists() else None)
        )

    def available(self) -> bool:
        return bool(self._binary) and Path(self._binary).exists()  # type: ignore[arg-type]

    def render(self, request: RenderRequest) -> RenderResult:  # pragma: no cover - stub
        raise RenderError(
            "inkscape backend not implemented yet (DESIGN.md §15 phase 4). Use the resvg backend."
        )
