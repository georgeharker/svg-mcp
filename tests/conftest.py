"""Suite-wide isolation."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Never read the developer's real ~/.config/svg-mcp/config.toml: point the
    # default location at an empty per-test directory.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("SVG_MCP_CONFIG", raising=False)
