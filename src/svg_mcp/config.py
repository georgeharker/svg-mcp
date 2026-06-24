"""Runtime configuration, sourced from environment (prefix ``SVG_MCP_``) or a ``.env`` file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings.

    Every field can be overridden by an env var, e.g. ``SVG_MCP_RENDERER=cairo`` or
    ``SVG_MCP_RESVG_BINARY=/opt/homebrew/bin/resvg``.
    """

    model_config = SettingsConfigDict(env_prefix="SVG_MCP_", env_file=".env", extra="ignore")

    # --- rendering ---------------------------------------------------------
    renderer: str = "resvg"
    """Default render backend name: ``resvg`` (default), ``cairo``, or ``inkscape``."""

    resvg_binary: str | None = None
    """Explicit path to the ``resvg`` CLI. If None, we auto-detect on PATH."""

    inkscape_binary: str | None = None
    """Explicit path to the Inkscape CLI (macOS: Inkscape.app/Contents/MacOS/inkscape)."""

    # --- feedback loop -----------------------------------------------------
    feedback_max_edge: int | None = None
    """Optional long-edge cap (px) for the model-facing raster. None = hand the raw
    rasterized image back directly as base64 (no downscaling)."""

    default_background: str | None = None
    """Default render background as a CSS color, or None for transparent."""

    render_timeout_s: float = 30.0
    """Hard timeout for a single render subprocess."""


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
