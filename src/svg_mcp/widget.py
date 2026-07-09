"""The ``show_widget`` UI — a self-contained custom-HTML MCP-App with review furniture.

This is the low-level (custom HTML) MCP-Apps pattern. ``show_widget`` returns the rendered PNG as
ordinary tool ``content`` (so Claude Code and the model's own render-and-see loop always get the
image), and this HTML resource — loaded by an MCP-Apps client into a sandboxed iframe — reads that
same image out of the pushed tool result via the ``@modelcontextprotocol/ext-apps`` bridge
(``app.ontoolresult``) and paints it, with a small toolbar: zoom, pan, and backdrop (client-side),
plus Refresh and "open live preview" via the ext-apps reverse channel. File *save* is deliberately
not offered — sandboxed iframes block downloads, so saving is done by asking the model to run
``export_render`` / ``export_svg``. For the *full* interactive view use ``start_preview`` (the
loopback browser page), which this module deliberately does not touch.

The ext-apps SDK is **vendored inline** (``_ext_apps_inline.js``), not loaded from a CDN: an earlier
version imported it from unpkg, which failed under the host's iframe CSP, so the widget never ran
and the client fell back to the bare image. Inlining makes the page a single self-contained module.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["WIDGET_URI", "build_widget_html"]

#: The ``ui://`` resource the ``show_widget`` tool points at (its ``AppConfig.resource_uri``).
WIDGET_URI = "ui://svg-mcp/widget"

# The ext-apps SDK, vendored as a single inline module that exposes ``App`` on ``globalThis`` (its
# trailing ``export{…}`` is rewritten to ``globalThis.__SVGMCP_APP = <App>`` at vendor time, so it
# runs inline with no import). Regenerate via ``scripts/vendor_ext_apps.py``. ~310 KB minified.
_SDK = (Path(__file__).parent / "_ext_apps_inline.js").read_text(encoding="utf-8")


def build_widget_html() -> str:
    """The (static) widget page. It reads the render + caption from each tool result at runtime, so
    the same HTML serves every document — no per-call data is baked in."""
    return _PAGE


# Static page: the inlined ext-apps `App` connects to the host; `ontoolresult` receives the tool
# result on each show_widget call and repaints from its content blocks (image + text caption). The
# toolbar (zoom/pan/backdrop/save) operates entirely client-side on that image. `%%SDK%%` is
# substituted (not an f-string) because the minified SDK is full of braces.
_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>svg-mcp</title>
<style>
  :root { --fg:#3a3f46; --bd:#00000022; --bg:#ffffffcc; }
  @media (prefers-color-scheme: dark) { :root { --fg:#c9ced6; --bd:#ffffff22; --bg:#00000033; } }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body { font: 12px/1.4 system-ui, sans-serif; color: var(--fg); background: transparent;
         display: flex; flex-direction: column; height: 100vh; }
  header { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 6px 8px; }
  .group { display: flex; gap: 3px; align-items: center; }
  button, .save a { font: inherit; color: var(--fg); background: var(--bg);
         border: 1px solid var(--bd); border-radius: 6px; padding: 3px 8px;
         cursor: pointer; text-decoration: none; }
  button.on { outline: 2px solid #4b6a96; outline-offset: 1px; }
  .swatch { width: 18px; height: 18px; padding: 0; }
  main { flex: 1; overflow: auto; display: grid; place-items: center; min-height: 140px;
         touch-action: none; }
  #stage { transform-origin: center; transition: transform .08s ease-out; }
  #stage img { display: block; max-width: none; border-radius: 8px;
               -webkit-user-drag: none; user-select: none; }
  figcaption { text-align: center; opacity: .7; padding: 4px; font-variant-numeric: tabular-nums; }
  #empty { opacity: .6; padding: 24px; }
</style>
</head>
<body>
<header>
  <div class="group">
    <button id="zout" title="zoom out">−</button>
    <button id="zfit" title="reset zoom">Fit</button>
    <button id="zin" title="zoom in">+</button>
  </div>
  <div class="group" id="bd" title="backdrop">
    <button data-bd="checker" class="on" title="checkerboard">▦</button>
    <button class="swatch" data-bd="#ffffff" style="background:#ffffff" title="white"></button>
    <button class="swatch" data-bd="#808080" style="background:#808080" title="grey"></button>
    <button class="swatch" data-bd="#111111" style="background:#111111" title="black"></button>
  </div>
  <div class="group" style="margin-left:auto">
    <button id="refresh" title="re-render the active document">↻</button>
    <button id="openprev" title="open the full interactive preview in a browser">↗ Preview</button>
  </div>
</header>
<main id="view"><div id="stage"><div id="empty">Waiting for the render…</div></div></main>
<figcaption id="cap"></figcaption>
<script type="module">
%%SDK%%
const App = globalThis.__SVGMCP_APP;
const app = new App({ name: "svg-mcp", version: "1.0.0" });

const view = document.getElementById("view");
const stage = document.getElementById("stage");
const cap = document.getElementById("cap");
let imgSrc = null, scale = 1;

function applyScale() { stage.style.transform = "scale(" + scale + ")"; }
function zoom(f) { scale = Math.min(Math.max(scale * f, 0.1), 16); applyScale(); }
document.getElementById("zin").onclick = () => zoom(1.25);
document.getElementById("zout").onclick = () => zoom(0.8);
document.getElementById("zfit").onclick = () => { scale = 1; applyScale(); };

const CHECKER = "repeating-conic-gradient(#00000012 0% 25%, transparent 0% 50%) 0 0 / 20px 20px";
function backdrop(bd) {
  view.style.background = bd === "checker" ? CHECKER : bd;
  document.querySelectorAll("#bd button").forEach(b =>
    b.classList.toggle("on", b.dataset.bd === bd));
}
document.querySelectorAll("#bd button").forEach(b => { b.onclick = () => backdrop(b.dataset.bd); });
backdrop("checker");

let drag = null;
view.addEventListener("pointerdown", e => {
  drag = { x: e.clientX, y: e.clientY, l: view.scrollLeft, t: view.scrollTop };
  view.setPointerCapture(e.pointerId);
});
view.addEventListener("pointermove", e => {
  if (!drag) return;
  view.scrollLeft = drag.l - (e.clientX - drag.x);
  view.scrollTop = drag.t - (e.clientY - drag.y);
});
view.addEventListener("pointerup", () => { drag = null; });
view.addEventListener("dragstart", e => e.preventDefault());

function paint(content) {
  const img = content?.find(c => c.type === "image");
  const txt = content?.find(c => c.type === "text");
  if (img) {
    imgSrc = "data:" + img.mimeType + ";base64," + img.data;
    scale = 1;
    const el = document.createElement("img");
    el.id = "img"; el.alt = "svg-mcp render"; el.draggable = false; el.src = imgSrc;
    el.onload = applyScale;
    stage.replaceChildren(el);
  }
  if (txt) cap.textContent = txt.text;
}
app.ontoolresult = ({ content }) => paint(content);

// Reverse-channel actions (widget -> server). These work on a direct connection; a proxy that
// doesn't forward ui/tools-call will make them no-ops (caught below), leaving the client-side
// toolbar (zoom/pan/backdrop) fully functional. In-widget file *save* is intentionally not
// offered: sandboxed iframes block downloads, so saving is done by asking the model to run
// export_render / export_svg (which write files directly).
async function refresh() {
  try {
    const r = await app.callServerTool({ name: "show_widget", arguments: {} });
    paint(r?.content);
  } catch (e) { cap.textContent = "refresh isn't available in this client"; }
}
async function openPreview() {
  try {
    const r = await app.callServerTool({ name: "start_preview", arguments: {} });
    const sc = r?.structuredContent;
    let url = sc?.url || sc?.result?.url;
    if (!url) {
      const t = r?.content?.find(c => c.type === "text")?.text;
      try { url = JSON.parse(t).url; } catch (_) { /* not json */ }
    }
    if (url) await app.openLink({ url });
  } catch (e) { cap.textContent = "live preview isn't available in this client"; }
}
document.getElementById("refresh").onclick = refresh;
document.getElementById("openprev").onclick = openPreview;

await app.connect();
</script>
</body>
</html>"""

_PAGE = _TEMPLATE.replace("%%SDK%%", _SDK)
