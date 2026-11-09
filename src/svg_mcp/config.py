"""Runtime configuration: ``SVG_MCP_*`` env vars, a ``.env`` file, then a TOML config file.

Precedence, highest first: explicit ``Settings(...)`` arguments, environment, ``.env``,
the config file (``$SVG_MCP_CONFIG``, else ``$XDG_CONFIG_HOME/svg-mcp/config.toml``,
default ``~/.config/svg-mcp/config.toml``), built-in defaults.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

CONFIG_ENV = "SVG_MCP_CONFIG"


def config_path() -> tuple[Path, bool]:
    """The config file to read, and whether it was named explicitly (``$SVG_MCP_CONFIG``).

    An explicitly named file must exist; the default location is optional."""
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser(), True
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "svg-mcp" / "config.toml", False


class _ConfigFileSource(TomlConfigSettingsSource):
    """The TOML config file, STRICT at the top level: an unknown key (a typo such as
    ``[webauth]``) is a startup error rather than a silently ignored table — the file can
    switch the preview login on, so a misspelling must not fail open. ``[web_auth]`` itself
    is already strict (``extra="forbid"``)."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        path, explicit = config_path()
        if explicit and not path.is_file():
            raise ValueError(f"{CONFIG_ENV}={path} does not exist")
        super().__init__(settings_cls, toml_file=path)
        unknown = sorted(set(self.toml_data) - set(settings_cls.model_fields))
        if unknown:
            raise ValueError(
                f"{path}: unknown setting(s) {', '.join(unknown)} — "
                f"known: {', '.join(sorted(settings_cls.model_fields))}"
            )
        #: the file's own ``web_auth.mode`` (for the no-silent-downgrade check).
        self.file_mode = (self.toml_data.get("web_auth") or {}).get("mode")
        self.path = path


class WebAuthConfig(BaseModel):
    """Browser login for the live preview (``[web_auth]`` in the config file, or
    ``SVG_MCP_WEB_AUTH`` as JSON; see svg_mcp.webauth).
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
    #: how many times a ticket id is redeemable (mcp-combiner parity: counted by
    #: ``j`` in memory until expiry). Default one-shot.
    ticket_uses: int = 1
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

    Every field can be set in the config file (``renderer = "cairo"``) and overridden by
    an env var (``SVG_MCP_RENDERER=cairo``, ``SVG_MCP_RESVG_BINARY=/opt/homebrew/bin/resvg``).
    """

    model_config = SettingsConfigDict(
        env_prefix="SVG_MCP_", env_file=".env", extra="ignore", env_nested_delimiter="__"
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        global _file_mode
        config_file = _ConfigFileSource(settings_cls)
        _file_mode = (str(config_file.path), config_file.file_mode)
        return init_settings, env_settings, dotenv_settings, config_file, file_secret_settings

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

    # --- server --------------------------------------------------------------
    transport: Literal["stdio", "http", "streamable-http", "sse"] = "stdio"
    """MCP transport (``--transport`` wins)."""

    host: str = "127.0.0.1"
    """Bind host for the http transports (``--host`` wins)."""

    port: int = 8000
    """Bind port for the http transports (``--port`` wins)."""

    shutdown_grace: int = 0
    """Seconds uvicorn waits for in-flight connections to drain on shutdown."""

    # --- live preview --------------------------------------------------------
    preview: bool = False
    """Start the live preview on boot (the model can also start it via ``start_preview``)."""

    preview_host: str = "127.0.0.1"
    """Bind host for the live preview."""

    preview_port: int | None = None
    """Bind port for the live preview; None picks the default (8808, or a free port)."""

    @field_validator("preview", mode="before")
    @classmethod
    def _empty_preview_is_off(cls, value: object) -> object:
        # SVG_MCP_PREVIEW= (set but empty) always meant off; keep it valid.
        return False if value == "" else value

    # --- live preview login ------------------------------------------------
    web_auth: WebAuthConfig = Field(default_factory=WebAuthConfig)
    """Browser login for the live preview: a ``[web_auth]`` table in the config file, or
    ``SVG_MCP_WEB_AUTH='{"mode": "password", ...}'`` (merged over the file's table)."""

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
        file_path, file_mode = _file_mode
        overrides: dict[str, Any] = {}
        raw_password = self.web_password or self.web_auth.password
        if self.web_password_hash:
            overrides["password_hash"] = self.web_password_hash
        elif self.web_auth.password_hash is None and raw_password:
            # hash-over-raw precedence: an explicitly provided hash (this env, or the nested
            # block) always wins; the raw password (SVG_MCP_WEB_PASSWORD, else
            # web_auth.password) is hashed HERE, at load, with a fresh salt — held in memory
            # only (same fail-open shape as toucan/mcp-combiner: neither set ⇒ auth off).
            from .webauth import hash_password  # noqa: PLC0415 — avoid a webauth→config cycle

            overrides["password_hash"] = hash_password(raw_password)
        if self.web_auth.password is not None:
            overrides["password"] = None  # consumed: never kept past load
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
        if file_mode == "password" and self.web_auth.mode != "password":
            # The same rule for the config file: a leftover env var must not silently
            # switch off a login the file turns on. Turn it off in the file instead.
            raise ValueError(
                f"{file_path} sets web_auth.mode 'password' but the environment resolved "
                "it to 'off' — remove the override, or set mode = \"off\" in the file"
            )
        if self.web_auth.mode == "password" and stored:
            from .webauth import validate_hash_artifact  # noqa: PLC0415 — validator-local

            validate_hash_artifact(stored)
        if self.web_auth.mode == "password" and not (stored and stored.startswith("scrypt$")):
            # Name the file when it is what turned the login on: the config file applies to
            # every launch, so a stdio client started without the password fails here.
            source = (
                f" (set by {file_path}, which every svg-mcp launch reads — keep [web_auth] "
                "in a file only the web server selects with SVG_MCP_CONFIG)"
                if file_mode == "password"
                else ""
            )
            raise ValueError(
                f"web_auth.mode 'password'{source} needs web_auth.password_hash, "
                "web_auth.password (SVG_MCP_WEB_PASSWORD) or SVG_MCP_WEB_PASSWORD_HASH "
                "— run `svg-mcp hash-password`"
            )
        return self


_settings: Settings | None = None
#: (path, ``web_auth.mode``) of the config file read by the latest Settings() build.
_file_mode: tuple[str, str | None] = ("", None)


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
