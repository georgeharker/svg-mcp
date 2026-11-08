"""Runtime configuration, sourced from environment (prefix ``SVG_MCP_``) or a ``.env`` file."""

from __future__ import annotations

import contextlib
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebAuthConfig(BaseModel):
    """Browser login for the live preview (``SVG_MCP_WEB_AUTH``; see svg_mcp.webauth).
    Off by default. Same shape as mcp-combiner's ``web_auth`` (incl. the raw-password
    mirror: ``SVG_MCP_WEB_PASSWORD``) — and STRICT: unknown keys fail startup (this blob
    is the credential source of record for a browser-facing gate; typos must not fail
    open, and a non-bool flag is a pydantic error as usual)."""

    model_config = ConfigDict(extra="forbid")

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

    @model_validator(mode="after")
    def _validate_session_days(self) -> Self:
        if self.session_days <= 0:
            raise ValueError(
                f"web_auth.session_days must be positive (got {self.session_days}) — "
                "a non-positive session is an unbounded or expired login, never a "
                "sensible default (the strict-config rule: non-positive values are "
                "startup errors, not silent defaults)"
            )
        return self


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
        # A leftover SVG_MCP_WEB_AUTH__MODE=off must never silently override a mode the
        # JSON block set to "password" (fail-open by design would then be invisible): if
        # the raw JSON env intended "password" and the resolved mode is off, fail the load.
        import json as _json  # noqa: PLC0415 — validator-local
        import os as _os  # noqa: PLC0415

        raw_mode = None
        with contextlib.suppress(Exception):
            raw_mode = (_json.loads(_os.environ["SVG_MCP_WEB_AUTH"]) or {}).get("mode")
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
            # The raw secret must not leak into child processes (the combiner's
            # release-review item): the hashed material lives in web_auth now.
            _os.environ.pop("SVG_MCP_WEB_PASSWORD", None)
        stored = self.web_auth.password_hash
        if raw_mode == "password" and self.web_auth.mode != "password":
            raise ValueError(
                "SVG_MCP_WEB_AUTH sets web_auth.mode 'password' but an SVG_MCP_WEB_AUTH__* "
                "override resolved the mode to 'off' — fix the leftover override"
            )
        if self.web_auth.mode == "password" and stored:
            from .webauth import validate_hash_artifact  # noqa: PLC0415 — validator-local

            validate_hash_artifact(stored)
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
