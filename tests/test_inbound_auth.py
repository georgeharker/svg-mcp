"""Optional inbound bearer auth for the svg-mcp server.

``src/svg_mcp/inbound_auth.py`` is vendored byte-identical from mcp-companion's
combiner. These tests pin the two axes that matter:

- **auth OFF** (``SVG_MCP_AUTH_TOKEN`` unset) → the server behaves exactly as
  before: ``/mcp`` is open, no header required.
- **auth ON** → ``/mcp`` requires the bearer (401 without / wrong, 200 with) and
  ``/health`` stays open.

The middleware is exercised over a trivial Starlette app (no server spin-up).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from svg_mcp.inbound_auth import BearerAuthMiddleware, resolve_auth_token

ENV = "SVG_MCP_AUTH_TOKEN"
TOKEN = "svg-secret-123"


def _gate(path: str) -> bool:
    # Plain FastMCP server: gate everything but /health.
    return path != "/health"


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an ambient SVG_MCP_AUTH_TOKEN (dev shell / secrets injection) out of
    tests; auth tests set it explicitly."""
    monkeypatch.delenv(ENV, raising=False)


def _app(token: str | None, is_protected: Callable[[str], bool] = _gate) -> Starlette:
    async def ok(_req: Any) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/mcp", ok, methods=["GET", "POST"]),
            Route("/mcp/{rest:path}", ok, methods=["GET", "POST"]),
            Route("/health", ok, methods=["GET"]),
        ]
    )
    if token:
        app.add_middleware(BearerAuthMiddleware, token=token, is_protected=is_protected)
    return app


# --- auth OFF: unchanged, open ------------------------------------------------


def test_open_when_unconfigured() -> None:
    client = TestClient(_app(None))
    assert client.post("/mcp").status_code == 200
    assert client.get("/health").status_code == 200


# --- auth ON: enforced --------------------------------------------------------


def test_mcp_requires_bearer() -> None:
    client = TestClient(_app(TOKEN))
    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_401_has_no_www_authenticate() -> None:
    r = TestClient(_app(TOKEN)).post("/mcp")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
    assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_health_open_even_with_auth() -> None:
    assert TestClient(_app(TOKEN)).get("/health").status_code == 200


def test_url_token_path_is_gated() -> None:
    client = TestClient(_app(TOKEN))
    assert client.post("/mcp/abc").status_code == 401
    assert client.post("/mcp/abc", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


# --- resolver -----------------------------------------------------------------


def test_resolve_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "from-env")
    assert resolve_auth_token(ENV) == "from-env"


def test_resolve_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert resolve_auth_token(ENV) is None


def test_resolve_blank_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "   ")
    assert resolve_auth_token(ENV) is None


def test_resolve_file_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "from-env")
    f = tmp_path / "tok"
    f.write_text(" from-file\n")
    assert resolve_auth_token(ENV, str(f)) == "from-file"
