"""resvg CLI render backend — the primary, default renderer.

Shells out to the ``resvg`` binary (deterministic, cross-platform, no system libs, native
text-on-path + broad filter support). Install on macOS via ``brew install resvg`` or
``cargo install resvg``; override the path with ``SVG_MCP_RESVG_BINARY``.

An in-process binding (the optional ``resvg-py`` extra) can be slotted in later behind the
same :class:`Renderer` protocol to avoid the subprocess.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from ..config import get_settings
from .base import RenderError, RenderRequest, RenderResult


class ResvgCliRenderer:
    """Render via the ``resvg`` command-line tool."""

    name = "resvg"

    def __init__(self, binary: str | None = None, *, timeout_s: float | None = None) -> None:
        settings = get_settings()
        self._binary = binary or settings.resvg_binary or shutil.which("resvg")
        self._timeout_s = timeout_s if timeout_s is not None else settings.render_timeout_s

    def available(self) -> bool:
        return bool(self._binary) and Path(self._binary).exists()  # type: ignore[arg-type]

    def _build_args(self, in_svg: Path, out_png: Path, request: RenderRequest) -> list[str]:
        assert self._binary is not None
        # resvg's --dpi takes an integer; --zoom takes a float; -w/-h take integer pixels.
        args = [self._binary, "--dpi", str(round(request.dpi))]
        # Explicit pixel dimensions win over zoom; resvg honors --width/--height directly.
        if request.width is not None:
            args += ["--width", str(request.width)]
        if request.height is not None:
            args += ["--height", str(request.height)]
        if request.width is None and request.height is None and request.scale != 1.0:
            args += ["--zoom", str(request.scale)]
        if request.background is not None:
            args += ["--background", request.background]
        args += [str(in_svg), str(out_png)]
        return args

    def render(self, request: RenderRequest) -> RenderResult:
        if not self.available():
            raise RenderError(
                "resvg binary not found. Install it (`brew install resvg` / "
                "`cargo install resvg`) or set SVG_MCP_RESVG_BINARY to its path."
            )

        start = time.perf_counter()
        with TemporaryDirectory(prefix="svg-mcp-") as tmp:
            in_svg = Path(tmp) / "in.svg"
            out_png = Path(tmp) / "out.png"
            in_svg.write_text(request.svg, encoding="utf-8")
            args = self._build_args(in_svg, out_png, request)
            try:
                proc = subprocess.run(
                    args,
                    capture_output=True,
                    timeout=self._timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RenderError(f"resvg timed out after {self._timeout_s}s") from exc

            if proc.returncode != 0:
                stderr = proc.stderr.decode("utf-8", "replace").strip()
                raise RenderError(f"resvg failed (exit {proc.returncode}): {stderr}")
            if not out_png.exists():
                raise RenderError("resvg reported success but wrote no output PNG")

            png = out_png.read_bytes()

        duration_ms = (time.perf_counter() - start) * 1000.0
        width, height = _png_dimensions(png)
        return RenderResult(
            png=png, width=width, height=height, backend=self.name, duration_ms=duration_ms
        )


def _png_dimensions(png: bytes) -> tuple[int, int]:
    """Read width/height from a PNG IHDR without decoding pixels."""
    # PNG signature (8) + length (4) + 'IHDR' (4) + width (4) + height (4)
    if len(png) >= 24 and png[12:16] == b"IHDR":
        width = int.from_bytes(png[16:20], "big")
        height = int.from_bytes(png[20:24], "big")
        return width, height
    return 0, 0
