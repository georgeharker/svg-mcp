"""web_auth credential resolution: raw password vs hash-artifact precedence.

Mirrors the toucan/mcp-combiner shape: an explicit hash wins over the raw
password; a raw password is hashed AT LOAD (fresh salt, in-memory only — never
persisted); when neither credential is set, mode "password" is a load error and
mode "off" stays open (fail-open, like both neighbors).
"""

from __future__ import annotations

from typing import Any

import pytest

from svg_mcp.config import Settings, WebAuthConfig
from svg_mcp.webauth import verify_password


def _settings(**kwargs: Any) -> Settings:
    return Settings(**kwargs)


def test_raw_password_is_hashed_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVG_MCP_WEB_AUTH", '{"mode": "password"}')
    monkeypatch.setenv("SVG_MCP_WEB_PASSWORD", "shared-hunter2")
    settings = _settings()
    stored = settings.web_auth.password_hash
    assert stored is not None and stored.startswith("scrypt$")
    # fresh-salted and verifiable by the login path:
    assert verify_password("shared-hunter2", stored)
    # the raw value never lingers anywhere:
    assert "shared-hunter2" not in (settings.web_auth.password_hash or "")
    assert settings.web_auth.password is None  # consumed (hashed) at load


def test_explicit_hash_wins_over_raw_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVG_MCP_WEB_AUTH", '{"mode": "password"}')
    monkeypatch.setenv("SVG_MCP_WEB_PASSWORD", "raw-ignored")  # noqa: S105 — fixture
    monkeypatch.setenv("SVG_MCP_WEB_PASSWORD_HASH", "scrypt$1$1$1$AAAA$BBBB")  # noqa: S105
    settings = _settings()
    assert settings.web_auth.password_hash == "scrypt$1$1$1$AAAA$BBBB"  # noqa: S105


def test_hash_override_env_wins_over_nested_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVG_MCP_WEB_AUTH__MODE", "password")
    monkeypatch.setenv(
        "SVG_MCP_WEB_AUTH__PASSWORD_HASH",
        "scrypt$1$1$1$CCCC$DDDD",  # noqa: S105
    )
    monkeypatch.setenv("SVG_MCP_WEB_PASSWORD", "raw-also-set")  # noqa: S105
    settings = _settings()
    assert settings.web_auth.password_hash == "scrypt$1$1$1$CCCC$DDDD"  # noqa: S105


def test_mode_password_without_credential_is_a_load_error() -> None:
    with pytest.raises(ValueError, match="password_hash"):
        _settings(web_auth=WebAuthConfig(mode="password"))


def test_neither_credential_with_default_mode_stays_open() -> None:
    settings = _settings()
    assert settings.web_auth.mode == "off"
    assert settings.web_auth.password_hash is None
