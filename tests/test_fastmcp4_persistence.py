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
- outside a request, direct/programmatic tool use resolves to the shared default store;
- the combiner handover contract: a client that drops WITHOUT a DELETE (park), then
  reattaches with the same ``Mcp-Session-Id``, reaches the same store; a DELETE (forget)
  releases the store.
"""

import asyncio
import contextlib
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from types import TracebackType
from typing import Any, cast

import anyio
import pytest
import uvicorn
from fastmcp import Client
from mcp.client.session import ClientSession
from mcp.client.streamable_http import StreamableHTTPTransport
from mcp.shared._context_streams import create_context_streams
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.message import SessionMessage
from mcp.types import InitializedNotification
from mcp_types.version import LATEST_HANDSHAKE_VERSION

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
    """First resource payload's text (mcp 1/2 differ only in union typing, not runtime)."""
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


def test_http_idle_expiry_reaps_parked_session() -> None:
    """The idle bound reaps a session whose client VANISHED without a DELETE (the
    crashed-combiner / vanished-direct-client scenario): create a document, drop the
    connection, and after the idle bound the per-session store is released — pinning
    the bounded-reaping behavior (see `_DEFAULT_IDLE_TIMEOUT_S`)."""
    import socket as _socket  # noqa: PLC0415 — test-local

    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    asgi = _mcp_server.http_app(session_idle_timeout=1.0)
    uvicorn_config = uvicorn.Config(asgi, host="127.0.0.1", port=port, log_level="error")
    uvicorn_server = uvicorn.Server(uvicorn_config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + _TIMEOUT_S
    while not uvicorn_server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:

        async def scenario() -> None:
            # Park a session: raw client creates a document, then drops WITHOUT a DELETE.
            async with _raw_session(f"http://127.0.0.1:{port}/mcp", terminate_on_close=False) as (
                t,
                session,
            ):
                created = await session.call_tool("create_document", {"width": 100, "height": 100})
                assert not created.is_error
            assert len(_SESSION_STORES) == 1  # the parked session still holds its store
            # The vanished client's session idles past the 1.0s bound: reaped. (A CONNECTED
            # session never idles out — the GET stream counts as activity in the SDK.)
            gone_by = time.monotonic() + 10.0
            while _SESSION_STORES and time.monotonic() < gone_by:
                await asyncio.sleep(0.05)
            assert not _SESSION_STORES, "expired parked session was not reaped"

        asyncio.run(scenario())
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=_TIMEOUT_S)


# --- combiner handover contract --------------------------------------------
# The mcp-combiner parks upstream sessions across its own restarts and resumes them
# by reattaching with the SAME Mcp-Session-Id (skip initialize, re-send
# notifications/initialized). That contract only holds if svg-mcp keeps the per-client
# state (and its Connection identity) alive while the client is GONE, which requires:
# (a) the SDK/server not tearing the session down when the client's HTTP connection
#     / GET stream drops without a DELETE (combiner park = drop without DELETE), and
# (b) the idle timeout being off — parked sessions sit idle up to the 1h park TTL.
# These tests pin both sides of that contract over real HTTP with raw mcp 2 protocol
# calls (the combiner's client face), mirroring what its ResumableStreamableHttpTransport
# does. The combiner maintains ids across its own proper restarts; svg-mcp's side of the
# deal is reattach-stability, which is what is proven here.


@contextlib.asynccontextmanager
async def _raw_session(
    url: str,
    *,
    seeded_session_id: str | None = None,
    terminate_on_close: bool = True,
) -> AsyncIterator[tuple[StreamableHTTPTransport, ClientSession]]:
    """A minimal raw mcp 2 streamable-HTTP client — mirrors the SDK's own transport
    composition, plus the combiner resume path: connect with a pre-established
    Mcp-Session-Id (skip initialize, re-send notifications/initialized) and reattach
    to the server's live session.
    """
    transport = StreamableHTTPTransport(url)
    seeded = seeded_session_id is not None
    if seeded:
        transport.session_id = seeded_session_id
        transport._protocol_version_header = LATEST_HANDSHAKE_VERSION
    http_client = create_mcp_http_client()

    read_writer, read_stream = create_context_streams[SessionMessage | Exception](0)
    write_stream, write_reader = create_context_streams[SessionMessage](0)

    async with (
        http_client,
        read_writer,
        read_stream,
        write_stream,
        write_reader,
        anyio.create_task_group() as tg,
    ):

        def _start_get_stream() -> None:
            tg.start_soon(transport.handle_get_stream, http_client, read_writer)

        tg.start_soon(
            transport.post_writer,
            http_client,
            write_reader,
            read_writer,
            write_stream,
            _start_get_stream,
            tg,
        )

        async with ClientSession(read_stream, write_stream) as session:
            if seeded:
                await session.send_notification(InitializedNotification())
            else:
                await session.initialize()
            try:
                yield transport, session
            finally:
                if transport.session_id and terminate_on_close:
                    await transport.terminate_session(http_client)
                tg.cancel_scope.cancel()


def test_combiner_reattach_same_session_id_keeps_documents(server: _Server) -> None:
    """The combiner's park/resume: drop without DELETE, then reattach with the same
    Mcp-Session-Id (no initialize) — the SAME per-client store must be reached and
    the parked documents survive."""

    async def scenario() -> None:
        # Parked era: establish a session, create a document, then drop WITHOUT a DELETE.
        async with _raw_session(server.url, terminate_on_close=False) as (transport, session):
            created = await session.call_tool("create_document", {"width": 400, "height": 200})
            document_id = created.structured_content["document_id"]
            session_id = transport.session_id
            assert session_id is not None

        # Resumed era: the successor attaches with the SAME Mcp-Session-Id, no initialize.
        async with _raw_session(server.url, seeded_session_id=session_id) as (transport, session):
            assert transport.session_id == session_id
            # The original store counter continues (a fresh store would re-number "doc1").
            created = await session.call_tool("create_document", {"width": 50, "height": 50})
            assert created.structured_content["document_id"] != document_id
            # And the parked document is still there, via the resource layer too.
            contents = await session.read_resource("svg://documents")
            index = _documents_index(_resource_text(contents.contents[0]))
            assert document_id in [d["id"] for d in index["documents"]]

    asyncio.run(scenario())


def test_combiner_forget_delete_releases_store(server: _Server) -> None:
    """A DELETE (the combiner's forget path) tears the session down and releases its
    store from the per-session registry — nothing lingers after the forget."""

    async def scenario() -> None:
        async with _raw_session(server.url, terminate_on_close=True) as (transport, session):
            await session.call_tool("create_document", {"width": 300, "height": 300})
            assert transport.session_id is not None
            # The context exit below sends DELETE via terminate_session.

    asyncio.run(scenario())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _SESSION_STORES:
        time.sleep(0.02)
    assert not _SESSION_STORES, "DELETE did not release the session's store"
