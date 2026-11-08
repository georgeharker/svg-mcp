"""Runtime configuration, sourced from environment (prefix ``SVG_MCP_``) or a ``.env`` file."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebAuthConfig(BaseModel):
    """Browser login for the live preview (``SVG_MCP_WEB_AUTH``; see svg_mcp.webauth).
    Off by default. Same shape as mcp-combiner's ``web_auth`` (incl. the raw-password
    mirror: ``SVG_MCP_WEB_PASSWORD``)."""

    mode: Literal["off", "password"] = "off"
    #: ``svg-mcp hash-password`` output; ``$SVG_MCP_WEB_PASSWORD_HASH`` wins.
    password_hash: str | None = None
    #: RAW password; ``$SVG_MCP_WEB_PASSWORD`` wins. Hashed AT LOAD with a fresh salt
    #: (in-memory only — never persisted; the cookies' signing key file is separate).
    #: Explicit hash wins over this (the hash-artifact path keeps working).
    password: str | None = None
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

    web_password: str | None = None
    """Raw password override (``SVG_MCP_WEB_PASSWORD``): hashed at load into
    ``web_auth.password_hash`` with a fresh salt; the explicit hash env wins."""

    @model_validator(mode="after")
    def _resolve_web_auth(self) -> Self:
        overrides: dict[str, str] = {}
        if self.web_password_hash:
            overrides["password_hash"] = self.web_password_hash
        elif self.web_auth.password_hash is None and self.web_password:
            # hash-over-raw precedence: an explicitly provided hash (this env, or the nested
            # JSON) always wins; the raw password is hashed HERE, at load, with a fresh salt
            # — held in memory only (same fail-open shape as toucan/mcp-combiner: neither
            # set ⇒ auth off).
            from .webauth import hash_password  # noqa: PLC0415 — avoid a webauth→config cycle

            overrides["password_hash"] = hash_password(self.web_password)
        if overrides:
            self.web_auth = self.web_auth.model_copy(update=overrides)
        stored = self.web_auth.password_hash
        if self.web_auth.mode == "password" and not (stored and stored.startswith("scrypt$")):
            raise ValueError(
                "web_auth.mode 'password' needs web_auth.password_hash, "
                "web_auth.password (SVG_MCP_WEB_PASSWORD) or SVG_MCP_WEB_PASSWORD_HASH "
                "— run `svg-mcp hash-password`"
            )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
