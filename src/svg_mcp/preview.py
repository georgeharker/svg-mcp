"""Live preview server — a browser-facing mirror of the ``svg://`` resources.

The MCP server is a process on the user's machine, so it can show the work-in-progress
directly rather than pushing render bytes through the model's context (which costs tokens and
isn't drawn inline by terminal clients anyway). This module runs a tiny stdlib HTTP server,
bound to loopback, that serves an HTML page the user opens once and leaves open: it auto-refreshes
on every document change via Server-Sent Events.

The HTTP routes deliberately mirror the published resource refs so the two share one identity:

    svg://{document_id}/svg     <->  GET /{document_id}/svg
    svg://{document_id}/render  <->  GET /{document_id}/render
    svg://documents             <->  GET /documents

``document_id`` may be the literal ``active`` to always follow the active document.

The server never touches the live document tree: callers (running inside the MCP request, where
the document is consistent) hand it an immutable serialized SVG *string* via
:meth:`PreviewServer.publish`. Every HTTP request renders from that snapshot, so concurrent
mutations on the asyncio thread can
never tear a render.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .render import SUPPORTED_FORMATS, export_bytes, rsvg_available

_MIME: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "ps": "application/postscript",
    "eps": "application/postscript",
    "svg": "image/svg+xml",
}

_DEFAULT_PORT = 8808


_EMPTY_INDEX = '{"active": null, "documents": []}'


class _Bucket:
    """Per-session preview state — the snapshot one chat sees.

    Holds the serialized-SVG sources, which document is active, a monotonic generation counter,
    and the set of SSE client queues currently watching this session.
    """

    def __init__(self) -> None:
        self.gen = 0
        self.active: str | None = None
        self.sources: dict[str, str] = {}
        self.index_json = _EMPTY_INDEX
        self.backdrop = "checker"  # page backdrop: "checker" | "white" | "black" | any CSS color
        self.clients: set[queue.Queue[int]] = set()


class PreviewServer:
    """One shared HTTP+SSE server, partitioned by session token so each chat is isolated.

    svg-mcp documents are per-session (see ``_SESSION_STORES`` in the server module). A single
    global preview would let one chat's edits clobber another's, since every session's
    ``_emit_change`` publishes its active document. So state lives in a per-token :class:`_Bucket`
    and every URL carries the token as its first path segment (``/<token>/render`` …). The HTML
    page reads its own token from ``location.pathname``, so the page itself stays static.

    State is pushed in via :meth:`publish` from MCP-request context; HTTP handlers only read it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._httpd: ThreadingHTTPServer | None = None
        self._host = "127.0.0.1"
        self._port = 0

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def ensure_running(self, host: str = "127.0.0.1", port: int | None = None) -> tuple[str, int]:
        """Start the shared server if needed and return ``(base_url, port)``. Idempotent.

        Tries the requested port (default 8808); if it is taken, binds an ephemeral port and
        reports the one actually bound. A server already running is left as-is. The returned URL
        is the *base* (no token); use :meth:`url_for` for a session's page URL.
        """
        with self._lock:
            if self._httpd is None:
                handler = _make_handler(self)
                want = _DEFAULT_PORT if port is None else port
                try:
                    httpd = ThreadingHTTPServer((host, want), handler)
                except OSError:
                    httpd = ThreadingHTTPServer((host, 0), handler)  # port in use -> ephemeral
                httpd.daemon_threads = True
                # Don't join in-flight (daemon) request threads on close — SSE handlers block on
                # ``queue.get`` and would otherwise wedge ``server_close``.
                httpd.block_on_close = False
                self._httpd = httpd
                self._host = host
                self._port = httpd.server_address[1]
                threading.Thread(
                    target=httpd.serve_forever, name="svg-mcp-preview", daemon=True
                ).start()
            return f"http://{self._host}:{self._port}/", self._port

    def shutdown(self) -> None:
        """Stop the accept loop and close the listening socket. Idempotent.

        Must run off the ``serve_forever`` thread (``shutdown`` joins it); the atexit hook and any
        caller on the main thread satisfy that. In-flight request threads are daemons and are left
        to be reaped at interpreter exit (see ``block_on_close``).
        """
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            with contextlib.suppress(Exception):
                httpd.shutdown()
            with contextlib.suppress(Exception):
                httpd.server_close()

    def url_for(self, token: str) -> str:
        """The page URL a given session should open."""
        return f"http://{self._host}:{self._port}/{token}/"

    # -- snapshot publishing (one bucket per session) -----------------------

    def _bucket(self, token: str) -> _Bucket:  # caller holds the lock
        bucket = self._buckets.get(token)
        if bucket is None:
            bucket = _Bucket()
            self._buckets[token] = bucket
        return bucket

    def publish(
        self, token: str, *, active_id: str | None, sources: dict[str, str], index_json: str
    ) -> None:
        """Replace the snapshot for one session and nudge its browsers to refresh.

        ``sources`` maps document_id -> serialized SVG; it is merged into the session's bucket (so
        previously-captured documents stay renderable) and pruned to the ids in ``index_json``.
        """
        with self._lock:
            bucket = self._bucket(token)
            bucket.active = active_id
            bucket.sources.update(sources)
            bucket.index_json = index_json
            live = {d.get("id") for d in json.loads(index_json).get("documents", [])}
            bucket.sources = {k: v for k, v in bucket.sources.items() if k in live}
            bucket.gen += 1
            gen = bucket.gen
            clients = list(bucket.clients)
        for q in clients:
            with contextlib.suppress(queue.Full):
                q.put_nowait(gen)

    # -- handler-side reads (scoped to a token) -----------------------------

    def resolve(self, token: str, doc_id: str) -> str | None:
        with self._lock:
            bucket = self._buckets.get(token)
            if bucket is None:
                return None
            if doc_id == "active":
                doc_id = bucket.active or ""
            return bucket.sources.get(doc_id)

    def state(self, token: str) -> tuple[int, str | None, str, str]:
        with self._lock:
            bucket = self._buckets.get(token)
            if bucket is None:
                return 0, None, _EMPTY_INDEX, "checker"
            return bucket.gen, bucket.active, bucket.index_json, bucket.backdrop

    def set_backdrop(self, token: str, backdrop: str) -> None:
        """Set a session's preview backdrop and nudge its browsers (MCP-side control)."""
        with self._lock:
            bucket = self._bucket(token)
            bucket.backdrop = backdrop
            bucket.gen += 1
            gen = bucket.gen
            clients = list(bucket.clients)
        for q in clients:
            with contextlib.suppress(queue.Full):
                q.put_nowait(gen)

    def subscribe(self, token: str) -> queue.Queue[int]:
        q: queue.Queue[int] = queue.Queue(maxsize=8)
        with self._lock:
            self._bucket(token).clients.add(q)
        return q

    def unsubscribe(self, token: str, q: queue.Queue[int]) -> None:
        with self._lock:
            bucket = self._buckets.get(token)
            if bucket is not None:
                bucket.clients.discard(q)


server = PreviewServer()


def _make_handler(owner: PreviewServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        preview = owner
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:  # silence stderr access logs
            pass

        # -- response helpers --------------------------------------------

        def _bytes(self, body: bytes, mime: str, *, disposition: str | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if disposition is not None:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()
            with _suppress_broken_pipe():
                self.wfile.write(body)

        def _text(self, body: str, mime: str = "text/plain; charset=utf-8") -> None:
            self._bytes(body.encode("utf-8"), mime)

        def _status(self, code: int, message: str) -> None:
            data = message.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            with _suppress_broken_pipe():
                self.wfile.write(data)

        # -- routing ------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            path = urlparse(self.path).path
            parts = [p for p in path.split("/") if p]

            if not parts:  # bare / — no session token, nothing to show
                return self._text(_INDEX_PAGE, "text/html; charset=utf-8")

            token, sub = parts[0], parts[1:]
            if not sub:  # /<token> or /<token>/ — the page (it reads its own token from the URL)
                return self._text(_PAGE, "text/html; charset=utf-8")
            if sub == ["events"]:
                return self._sse(token)
            if sub == ["version"]:
                gen, active, _, backdrop = self.preview.state(token)
                return self._text(
                    json.dumps(
                        {
                            "gen": gen,
                            "active": active,
                            "vector": rsvg_available(),
                            "backdrop": backdrop,
                        }
                    ),
                    "application/json",
                )
            if sub == ["documents"]:
                index_json = self.preview.state(token)[2]
                return self._text(index_json, "application/json")

            # /<token>/{doc}/svg | /render | /export.{fmt}
            if len(sub) == 2:
                doc_id, leaf = sub
                source = self.preview.resolve(token, doc_id)
                if source is None:
                    return self._status(404, "no such document")
                if leaf == "svg":
                    return self._bytes(source.encode("utf-8"), _MIME["svg"])
                if leaf == "render":
                    return self._export(source, "png")
                if leaf.startswith("export."):
                    return self._export(source, leaf.split(".", 1)[1], download=True)
            return self._status(404, "not found")

        def _export(self, source: str, fmt: str, *, download: bool = False) -> None:
            fmt = fmt.lower()
            if fmt not in SUPPORTED_FORMATS:
                return self._status(404, f"unsupported format: {fmt}")
            try:
                body = export_bytes(source, fmt)
            except Exception as exc:  # render/convert failure -> let the page keep the old frame
                return self._status(503, f"render failed: {exc}")
            disposition = f'attachment; filename="drawing.{fmt}"' if download else None
            self._bytes(body, _MIME.get(fmt, "application/octet-stream"), disposition=disposition)

        def _sse(self, token: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = self.preview.subscribe(token)
            try:
                self._sse_send(self.preview.state(token)[0])  # prime with current generation
                while True:
                    try:
                        gen = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._sse_send(gen)
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                pass
            finally:
                self.preview.unsubscribe(token, q)

        def _sse_send(self, gen: int) -> None:
            self.wfile.write(f"event: change\ndata: {gen}\n\n".encode())
            self.wfile.flush()

    return Handler


class _suppress_broken_pipe:
    """Swallow client-disconnect errors raised while writing a response body."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_rest: object) -> bool:
        return exc_type is not None and issubclass(
            exc_type, (BrokenPipeError, ConnectionResetError, OSError)
        )


_INDEX_PAGE = (
    "<!doctype html><meta charset='utf-8'><title>svg-mcp preview</title>"
    "<body style='font:14px system-ui;background:#1a1b1e;color:#e6e6e6;padding:40px'>"
    "<h2>svg-mcp live preview</h2>"
    "<p>This URL needs a session token. Call the <code>start_preview</code> tool in your chat "
    "and open the URL it returns (<code>/&lt;token&gt;/</code>) — each chat gets its own.</p>"
)


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>svg-mcp preview</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body { display: flex; flex-direction: column; font: 13px/1.4 system-ui, sans-serif;
         background: #1a1b1e; color: #e6e6e6; }
  header { display: flex; align-items: center; gap: 14px; padding: 8px 14px;
           background: #232428; border-bottom: 1px solid #34363b; flex: none; }
  header .title { font-weight: 600; }
  header .meta { color: #9aa0a6; font-variant-numeric: tabular-nums; }
  header .spacer { flex: 1; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #f0883e;
         box-shadow: 0 0 6px #f0883e; transition: background .2s, box-shadow .2s; }
  .dot.live { background: #57ab5a; box-shadow: 0 0 6px #57ab5a; }
  .group { display: flex; align-items: center; gap: 4px; }
  button, .save a { background: #2f3136; color: #e6e6e6; border: 1px solid #44464c;
           border-radius: 6px; padding: 4px 9px; font: inherit; cursor: pointer;
           text-decoration: none; }
  button:hover, .save a:hover { background: #3a3d43; }
  button.on { background: #3b5070; border-color: #4b6a96; }
  .save { position: relative; }
  .save > button::after { content: " \\25BE"; color: #9aa0a6; }
  .save .menu { display: none; position: absolute; right: 0; top: 110%; z-index: 10;
                flex-direction: column; gap: 2px; padding: 6px; background: #232428;
                border: 1px solid #44464c; border-radius: 8px; min-width: 120px; }
  .save.open .menu { display: flex; }
  #bd-color { width: 24px; height: 26px; padding: 0; border: 1px solid #44464c;
              border-radius: 6px; background: #2f3136; cursor: pointer; }
  .swatch { width: 22px; height: 22px; padding: 0; border: 1px solid #44464c;
            border-radius: 5px; cursor: pointer; }
  .swatch.on, #backdrop button.on { outline: 2px solid #4b6a96; outline-offset: 1px; }
  main { flex: 1; overflow: auto; display: grid; place-items: center; padding: 24px;
         background-color: #141517;
         background-image: linear-gradient(45deg,#1d1e21 25%,transparent 25%),
                           linear-gradient(-45deg,#1d1e21 25%,transparent 25%),
                           linear-gradient(45deg,transparent 75%,#1d1e21 75%),
                           linear-gradient(-45deg,transparent 75%,#1d1e21 75%);
         background-size: 22px 22px; background-position: 0 0,0 11px,11px -11px,-11px 0; }
  main.pannable { cursor: grab; }
  main.dragging { cursor: grabbing; }
  main.dragging #stage { transition: none; }
  #stage { transform-origin: center; transition: transform .08s ease-out;
           box-shadow: 0 8px 40px rgba(0,0,0,.5); background: #fff; }
  #stage img, #stage svg { display: block; max-width: none; -webkit-user-drag: none;
           user-select: none; }
</style>
</head>
<body>
<header>
  <span class="dot" id="dot" title="connection"></span>
  <span class="title">svg-mcp</span>
  <span class="meta" id="meta">waiting for document…</span>
  <span class="spacer"></span>
  <div class="group">
    <button id="view-png" class="on">PNG</button>
    <button id="view-svg">SVG</button>
  </div>
  <div class="group">
    <button id="zoom-out">−</button>
    <button id="zoom-fit">Fit</button>
    <button id="zoom-in">+</button>
  </div>
  <div class="group" id="backdrop" title="preview backdrop">
    <button data-bd="checker" class="on" title="checkerboard">▦</button>
    <button class="swatch" data-bd="#ffffff" style="background:#ffffff" title="white"></button>
    <button class="swatch" data-bd="#d4d4d4" style="background:#d4d4d4" title="light grey"></button>
    <button class="swatch" data-bd="#808080" style="background:#808080" title="grey"></button>
    <button class="swatch" data-bd="#404040" style="background:#404040" title="dark grey"></button>
    <button class="swatch" data-bd="#111111" style="background:#111111" title="black"></button>
    <input type="color" id="bd-color" value="#1e293b" title="custom backdrop">
  </div>
  <div class="save" id="save">
    <button>Save</button>
    <div class="menu" id="save-menu"></div>
  </div>
</header>
<main><div id="stage"></div></main>
<script>
const stage = document.getElementById('stage');
const view = document.querySelector('main');
const dot = document.getElementById('dot');
const meta = document.getElementById('meta');
// This page is served at /<token>/ — every API call is scoped to that session token, so two
// chats sharing the server never see each other's documents.
const base = '/' + (location.pathname.split('/').filter(Boolean)[0] || '');
let mode = 'png';      // 'png' | 'svg'
let scale = 1;
let vector = false;

function pannable(){
  // Measure the stage's actual transformed box. getBoundingClientRect() forces a synchronous
  // layout, so it reflects the current scale immediately — unlike scrollWidth/scrollHeight, which
  // lag a transform change by a tick and would leave the class stale right after a zoom reset.
  const r = stage.getBoundingClientRect();
  return r.width > view.clientWidth + 1 || r.height > view.clientHeight + 1;
}
function applyScale(){
  stage.style.transform = 'scale(' + scale + ')';
  view.classList.toggle('pannable', pannable());
}
function zoom(f){ scale = Math.min(Math.max(scale*f, 0.05), 16); applyScale(); }
document.getElementById('zoom-in').onclick = () => zoom(1.25);
document.getElementById('zoom-out').onclick = () => zoom(0.8);
document.getElementById('zoom-fit').onclick = () => { scale = 1; applyScale(); };

// Backdrop behind the (possibly transparent) artwork — controllable here AND from MCP.
let backdrop = 'checker';
function applyBackdrop(bd){
  backdrop = bd || 'checker';
  const named = {white:'#ffffff', black:'#111111', grey:'#808080', gray:'#808080'};
  if (backdrop === 'checker') {
    view.style.backgroundImage = '';   // fall back to the CSS checkerboard
    view.style.backgroundColor = '';
  } else {
    view.style.backgroundImage = 'none';
    view.style.backgroundColor = named[backdrop] || backdrop;
  }
  document.querySelectorAll('#backdrop button').forEach(
    b => b.classList.toggle('on', b.dataset.bd === backdrop)
  );
}
document.querySelectorAll('#backdrop button').forEach(b => {
  b.onclick = () => applyBackdrop(b.dataset.bd);
});
document.getElementById('bd-color').oninput = (e) => applyBackdrop(e.target.value);

// Drag to pan when zoomed in (adjusts scroll, so it cooperates with centering).
let drag = null;
view.addEventListener('pointerdown', (e) => {
  if (!pannable() || e.button !== 0) return;
  drag = {x: e.clientX, y: e.clientY, left: view.scrollLeft, top: view.scrollTop};
  view.classList.add('dragging');
  view.setPointerCapture(e.pointerId);
  e.preventDefault();
});
view.addEventListener('pointermove', (e) => {
  if (!drag) return;
  view.scrollLeft = drag.left - (e.clientX - drag.x);
  view.scrollTop = drag.top - (e.clientY - drag.y);
});
function endDrag(e){
  if (!drag) return;
  drag = null;
  view.classList.remove('dragging');
  if (e.pointerId != null) view.releasePointerCapture(e.pointerId);
}
view.addEventListener('pointerup', endDrag);
view.addEventListener('pointercancel', endDrag);
view.addEventListener('dragstart', (e) => e.preventDefault());  // kill native image drag-ghost

const btnPng = document.getElementById('view-png');
const btnSvg = document.getElementById('view-svg');
function setMode(m){
  mode = m;
  btnPng.classList.toggle('on', m === 'png');
  btnSvg.classList.toggle('on', m === 'svg');
  refresh();
}
btnPng.onclick = () => setMode('png');
btnSvg.onclick = () => setMode('svg');

const save = document.getElementById('save');
save.querySelector('button').onclick = (e) => {
  e.stopPropagation();
  save.classList.toggle('open');
};
document.body.addEventListener('click', () => save.classList.remove('open'));

function buildSaveMenu(){
  const formats = [['png','PNG'],['svg','SVG'],['webp','WebP'],['jpeg','JPEG']];
  if (vector) formats.push(['pdf','PDF']);
  document.getElementById('save-menu').innerHTML = formats.map(([f,label]) =>
    '<a href="' + base + '/active/export.' + f + '" download="drawing.' + f + '">' + label + '</a>'
  ).join('');
}

let gen = -1;
let lastServerBackdrop = null;
async function refresh(){
  const r = await fetch(base + '/version', {cache:'no-store'});
  if (!r.ok) return;
  const v = await r.json();
  gen = v.gen;
  if (v.vector !== vector) { vector = v.vector; buildSaveMenu(); }
  // MCP-set backdrop wins when it actually changes; local picks persist between server changes.
  if (v.backdrop !== undefined && v.backdrop !== lastServerBackdrop) {
    lastServerBackdrop = v.backdrop;
    applyBackdrop(v.backdrop);
  }
  if (!v.active) { meta.textContent = 'no active document'; stage.replaceChildren(); return; }
  try {
    const idx = await (await fetch(base + '/documents', {cache:'no-store'})).json();
    const d = (idx.documents || []).find(x => x.active) || {};
    const size = d.width ? '  ·  ' + Math.round(d.width) + '×' + Math.round(d.height) : '';
    meta.textContent = (d.id || v.active) + size;
  } catch (_e) {}
  if (mode === 'png') {
    const img = new Image();
    img.draggable = false;
    img.onload = () => { stage.replaceChildren(img); applyScale(); };
    img.src = base + '/active/render?v=' + gen;
  } else {
    const svg = await (await fetch(base + '/active/svg?v=' + gen, {cache:'no-store'})).text();
    stage.innerHTML = svg;  // innerHTML does not execute scripts embedded in the SVG
    applyScale();
  }
}

function connect(){
  const es = new EventSource(base + '/events');
  es.onopen = () => dot.classList.add('live');
  es.addEventListener('change', refresh);
  es.onerror = () => { dot.classList.remove('live'); };
}
buildSaveMenu();
connect();
refresh();
</script>
</body>
</html>
"""
