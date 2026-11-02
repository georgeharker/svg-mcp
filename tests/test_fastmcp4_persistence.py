"""e2e tests for per-session document persistence on fastmcp 4 / mcp 2 (streamable HTTP).

The killer regression mcp 2 would have caused: its low-level ``ServerSession`` is constructed
fresh per inbound request, so a per-session ``DocumentStore`` keyed on the session object
would be empty on every call after the first and the weakref would die with the request —
silently, with no errors reported. ``svg_mcp.server._store()`` keys on the per-client
``Connection`` instead. These tests prove the fix over real HTTP, with legacy-mode clients
(handshake era — the stateful mode every client behind mcp-combiner uses):

- a document created in one request is still there for later requests (incl. resources);
- different client sessions get isolated stores;
- closing a client releases its store, so a fresh session starts empty;
- outside a request, direct/programmatic tool use resolves to the shared default store.
"""

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from types import TracebackType
from typing import Any, cast

import pytest
import uvicorn
from fastmcp import Client

from svg_mcp.server import _DEFAULT_STORE, _SESSION_STORES
from svg_mcp.server import mcp as _mcp_server

_TIMEOUT_S = 10.0


class _Server:
    """A real streamable-HTTP server over a random loopback port (uvicorn thread)."""

    def __init__(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port: int = s.getsockname()[1]
        asgi = _mcp_server.http_app()
        config = uvicorn.Config(asgi, host="127.0.0.1", port=self.port, log_level="error")
        self._uvicorn = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._uvicorn.run, daemon=True)

    def __enter__(self) -> "_Server":
        self._thread.start()
        deadline = time.monotonic() + _TIMEOUT_S
        while not self._uvicorn.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._uvicorn.should_exit = True
        self._thread.join(timeout=_TIMEOUT_S)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


@pytest.fixture()
def server() -> Iterator[_Server]:
    """A running streamable-HTTP svg-mcp for the duration of the test."""
    with _Server() as s:
        yield s


def _documents_index(text: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(text)
    return data


async def _legacy_client(url: str) -> Client[Any]:
    return Client(url, mode="legacy")


def _resource_text(contents: object) -> str:
    """First resource payload's text (mcp 1/2 differ in how the union is typed, not in runtime)."""
    text: str = cast("Any", contents).text
    return text


def test_cross_call_document_persistence(server: _Server) -> None:
    """A document created in one request survives every later request on the connection."""

    async def scenario() -> None:
        async with await _legacy_client(server.url) as client:
            created = await client.call_tool("create_document", {"width": 400, "height": 200})
            document_id = created.data["document_id"]

            # Same connection, a later HTTP request: the store must still have the document.
            index = _documents_index(
                _resource_text((await client.read_resource("svg://documents"))[0])
            )
            assert [d["id"] for d in index["documents"]] == [document_id]
            assert index["active"] == document_id

            # A mutating call against the persisted document works (no "unknown document").
            await client.call_tool("add_rect", {"x": 10, "y": 10, "width": 50, "height": 20})

    asyncio.run(scenario())


def test_sessions_are_isolated(server: _Server) -> None:
    """Two client connections never see each other's documents."""

    async def scenario() -> None:
        async with await _legacy_client(server.url) as alice:
            await alice.call_tool("create_document", {"width": 100, "height": 100})
        async with await _legacy_client(server.url) as bob:
            index = _documents_index(
                _resource_text((await bob.read_resource("svg://documents"))[0])
            )
            assert index["documents"] == []
            assert index["active"] is None
            # Bob's store is fresh: ids restart from the store-local counter.
            created = await bob.call_tool("create_document", {"width": 100, "height": 100})
            assert created.data["document_id"] == "doc1"

    asyncio.run(scenario())


def test_closing_a_client_releases_its_store(server: _Server) -> None:
    """After a client disconnects, its store is released from the per-session registry."""

    async def scenario() -> None:
        async with await _legacy_client(server.url) as client:
            await client.call_tool("create_document", {"width": 300, "height": 300})
            assert len(_SESSION_STORES) >= 1  # a store was created for the connection

    asyncio.run(scenario())
    # The release runs at connection teardown via Connection.exit_stack; give it a beat.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _SESSION_STORES:
        time.sleep(0.02)
    assert not _SESSION_STORES, "session store was not released after the client left"


def test_programmatic_use_uses_shared_default_store() -> None:
    """Direct (non-request) use — the fallback path — resolves to the in-process store."""

    from svg_mcp.server import _store

    assert _store() is _DEFAULT_STORE
    document_id, _ = _DEFAULT_STORE.create(10, 10)
    assert document_id in _DEFAULT_STORE.list_ids()
