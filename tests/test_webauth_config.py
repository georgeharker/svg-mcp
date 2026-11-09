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


# A plausible-but-fake artifact: real round params, base64'd 8-byte salt and
# 32-byte digest (matching `svg-mcp hash-password`'s shape).
_FAKE_HASH = "scrypt$16384$8$1$AAAAAAAAAAA$ERERERERERERERERERERERERERERERERERERERERERE"


def test_explicit_hash_wins_over_raw_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVG_MCP_WEB_AUTH", '{"mode": "password"}')
    monkeypatch.setenv("SVG_MCP_WEB_PASSWORD", "raw-ignored")  # noqa: S105 — fixture
    monkeypatch.setenv("SVG_MCP_WEB_PASSWORD_HASH", _FAKE_HASH)  # noqa: S105
    settings = _settings()
    assert settings.web_auth.password_hash == _FAKE_HASH  # noqa: S105


def test_hash_override_env_wins_over_nested_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVG_MCP_WEB_AUTH__MODE", "password")
    monkeypatch.setenv("SVG_MCP_WEB_AUTH__PASSWORD_HASH", _FAKE_HASH)  # noqa: S105
    monkeypatch.setenv("SVG_MCP_WEB_PASSWORD", "raw-also-set")  # noqa: S105
    settings = _settings()
    assert settings.web_auth.password_hash == _FAKE_HASH  # noqa: S105


def test_mode_password_without_credential_is_a_load_error() -> None:
    with pytest.raises(ValueError, match="password_hash"):
        _settings(web_auth=WebAuthConfig(mode="password"))


def test_neither_credential_with_default_mode_stays_open() -> None:
    settings = _settings()
    assert settings.web_auth.mode == "off"
    assert settings.web_auth.password_hash is None


# --- the TOML config file ----------------------------------------------------


def _write_config(tmp_path: Any, text: str) -> Any:
    path = tmp_path / "xdg-config" / "svg-mcp" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_config_file_turns_password_mode_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    _write_config(tmp_path, '[web_auth]\nmode = "password"\nsession_days = 7\n')
    monkeypatch.setenv("SVG_MCP_WEB_PASSWORD", "from-the-env")  # noqa: S105 — fixture
    settings = _settings()
    assert settings.web_auth.mode == "password"
    assert settings.web_auth.session_days == 7
    assert verify_password("from-the-env", settings.web_auth.password_hash or "")


def test_config_file_raw_password_is_hashed_and_dropped(tmp_path: Any) -> None:
    _write_config(tmp_path, '[web_auth]\nmode = "password"\npassword = "in-the-file"\n')
    settings = _settings()
    assert verify_password("in-the-file", settings.web_auth.password_hash or "")
    assert settings.web_auth.password is None


def test_env_overrides_the_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    _write_config(tmp_path, 'renderer = "cairo"\n[web_auth]\nsession_days = 7\n')
    monkeypatch.setenv("SVG_MCP_RENDERER", "inkscape")
    monkeypatch.setenv("SVG_MCP_WEB_AUTH", '{"session_days": 2}')
    settings = _settings()
    assert settings.renderer == "inkscape"
    assert settings.web_auth.session_days == 2


def test_env_cannot_silently_switch_off_the_files_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _write_config(tmp_path, f'[web_auth]\nmode = "password"\npassword_hash = "{_FAKE_HASH}"\n')
    monkeypatch.setenv("SVG_MCP_WEB_AUTH", '{"mode": "off"}')
    with pytest.raises(ValueError, match="resolved it to 'off'"):
        _settings()


def test_unknown_top_level_key_is_a_startup_error(tmp_path: Any) -> None:
    _write_config(tmp_path, '[webauth]\nmode = "password"\n')
    with pytest.raises(ValueError, match="unknown setting"):
        _settings()


def test_unknown_web_auth_key_is_a_startup_error(tmp_path: Any) -> None:
    _write_config(tmp_path, '[web_auth]\nmod = "password"\n')
    with pytest.raises(ValueError, match="mod"):
        _settings()


def test_explicit_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    path = tmp_path / "elsewhere.toml"
    path.write_text('renderer = "cairo"\n')
    monkeypatch.setenv("SVG_MCP_CONFIG", str(path))
    assert _settings().renderer == "cairo"
    monkeypatch.setenv("SVG_MCP_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(ValueError, match="does not exist"):
        _settings()


def test_no_config_file_is_fine() -> None:
    assert _settings().renderer == "resvg"


def test_server_and_preview_settings_from_the_file(tmp_path: Any) -> None:
    _write_config(
        tmp_path,
        'transport = "streamable-http"\nport = 7731\npreview = true\npreview_port = 9001\n',
    )
    settings = _settings()
    assert (settings.transport, settings.port) == ("streamable-http", 7731)
    assert (settings.preview, settings.preview_port) == (True, 9001)


def test_empty_preview_env_still_means_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SVG_MCP_PREVIEW", "")
    assert _settings().preview is False


def test_main_takes_the_file_and_flags_win(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    import sys

    from svg_mcp import config, server

    _write_config(tmp_path, 'transport = "streamable-http"\nhost = "0.0.0.0"\nport = 7731\n')
    monkeypatch.setattr(config, "_settings", None)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: seen.update(kw))
    monkeypatch.setattr(server, "_apply_default_idle_bound", lambda: None)
    monkeypatch.setattr(sys, "argv", ["svg-mcp", "--port", "9999"])
    # main() installs process-wide shutdown hooks; keep them out of the test run.
    monkeypatch.setattr("signal.signal", lambda *a: None)
    monkeypatch.setattr("atexit.register", lambda *a: None)
    try:
        server.main()
    finally:
        config._settings = None
    assert (seen["transport"], seen["host"], seen["port"]) == ("streamable-http", "0.0.0.0", 9999)


def test_a_file_enabled_login_without_a_password_names_the_file(tmp_path: Any) -> None:
    path = _write_config(tmp_path, '[web_auth]\nmode = "password"\n')
    with pytest.raises(ValueError, match="every svg-mcp launch reads") as err:
        _settings()
    assert str(path) in str(err.value)
