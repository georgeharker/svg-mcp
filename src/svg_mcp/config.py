"""Runtime configuration, sourced from environment (prefix ``SVG_MCP_``) or a ``.env`` file."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebAuthConfig(BaseModel):
    """Browser login for the live preview (``SVG_MCP_WEB_AUTH``; see svg_mcp.webauth).
    Off by default. Same shape as mcp-combiner's ``web_auth``."""

    mode: Literal["off", "password"] = "off"
    #: ``svg-mcp hash-password`` output; ``$SVG_MCP_WEB_PASSWORD_HASH`` wins.
    password_hash: str | None = None
    session_days: float = 30.0
    #: preview URLs handed to MCP clients (``start_preview``) carry a login ticket.
    tickets: bool = True
    #: requests from this machine's own addresses skip the login.
    exempt_local: bool = True


class Settings(BaseSettings):
    """Process-wide settings.

    Every field can be overridden by an env var, e.g. ``SVG_MCP_RENDERER=cairo`` or
    ``SVG_MCP_RESVG_BINARY=/opt/homebrew/bin/resvg``.
    """

    model_config = SettingsConfigDict(
        env_prefix="SVG_MCP_", env_file=".env", extra="ignore", env_nested_delimiter="__"
    )

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

    # --- live preview login ------------------------------------------------
    web_auth: WebAuthConfig = Field(default_factory=WebAuthConfig)
    """Browser login for the live preview: ``SVG_MCP_WEB_AUTH='{"mode": "password", ...}'``
    or one field at a time (``SVG_MCP_WEB_AUTH__MODE=password``)."""

    web_password_hash: str | None = None
    """Overrides ``web_auth.password_hash`` (``SVG_MCP_WEB_PASSWORD_HASH``)."""

    @model_validator(mode="after")
    def _resolve_web_auth(self) -> Self:
        if self.web_password_hash:
            self.web_auth = self.web_auth.model_copy(
                update={"password_hash": self.web_password_hash}
            )
        stored = self.web_auth.password_hash
        if self.web_auth.mode == "password" and not (stored and stored.startswith("scrypt$")):
            raise ValueError(
                "web_auth.mode 'password' needs web_auth.password_hash "
                "(or SVG_MCP_WEB_PASSWORD_HASH) — run `svg-mcp hash-password`"
            )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
