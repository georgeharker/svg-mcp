"""The live preview's login page (svg_mcp.webgate), and the reply type the gate returns.

Opened on whatever device the preview link reached — often over a tailnet with no
internet — so the page is self-contained (inline CSS and SVG, no external fetches)
and sized for a mobile viewport. Styled after mcp-combiner's auth pages.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

#: The svg-mcp mark (docs/img/icon.png, simplified): nested hexagons around a
#: rounded square on the violet-to-blue tile. Inline, no external fetches.
LOGO = """<svg class="mark" viewBox="0 0 64 64" role="img" aria-label="svg-mcp">
  <defs><linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#7c3aed"/><stop offset="1" stop-color="#3b82f6"/>
  </linearGradient></defs>
  <rect x="2" y="2" width="60" height="60" rx="16" fill="url(#tile)"/>
  <path d="M32 9 L52 20.5 V43.5 L32 55 L12 43.5 V20.5 Z" fill="none" stroke="#67e8f9"
    stroke-width="1.5" stroke-linejoin="round"/>
  <path d="M32 13 L48.5 22.5 V41.5 L32 51 L15.5 41.5 V22.5 Z" fill="#ffffff"
    stroke-linejoin="round"/>
  <path d="M32 21 L41.5 26.5 V37.5 L32 43 L22.5 37.5 V26.5 Z" fill="#67e8f9"/>
  <rect x="25.5" y="25.5" width="13" height="13" rx="4" fill="#ffffff"/>
</svg>"""

_STYLE = """
:root {
  --bg: #f4f5f7; --card: #ffffff; --text: #1d2330; --muted: #5d6676;
  --accent: #5b5bd6; --on-accent: #ffffff; --line: #e3e6ec; --warn: #c2410c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #11141a; --card: #1b2029; --text: #e8ebf1; --muted: #9aa3b2;
    --accent: #8b8bff; --on-accent: #0f1320; --line: #2b313c; --warn: #ff922b;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 24px 16px; background: var(--bg); color: var(--text);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial,
    sans-serif;
}
.card {
  width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--line);
  border-radius: 20px; padding: 28px 24px; box-shadow: 0 8px 30px rgba(0,0,0,.08);
  overflow-wrap: anywhere;
}
.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 22px; }
.mark { width: 44px; height: 44px; flex: none; }
.wordmark { font-weight: 650; letter-spacing: -.01em; font-size: 17px; }
.wordmark span { color: var(--muted); font-weight: 500; }
h1 { font-size: 22px; line-height: 1.25; margin: 0 0 10px; letter-spacing: -.01em; }
p { margin: 0 0 14px; color: var(--muted); }
.button {
  display: block; width: 100%; margin: 22px 0 10px; padding: 14px 18px; border-radius: 12px;
  background: var(--accent); color: var(--on-accent); text-align: center;
  text-decoration: none; font-weight: 650; font-size: 17px;
}
button.button { border: 0; cursor: pointer; font: inherit; font-weight: 650; }
.field {
  display: block; width: 100%; margin-top: 18px; padding: 13px 14px; border-radius: 12px;
  border: 1px solid var(--line); background: var(--bg); color: var(--text); font: inherit;
}
.status {
  display: inline-flex; align-items: center; gap: 8px; font-weight: 600; margin-bottom: 10px;
}
.status.warn { color: var(--warn); }
.dot { width: 10px; height: 10px; border-radius: 50%; background: currentColor; }
"""


@dataclass
class Reply:
    """A response for the preview's stdlib handler to write (svg_mcp.preview)."""

    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""


def _page(title: str, body: str, status: int = 200) -> Reply:
    document = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name=robots content="noindex,nofollow">'
        f"<title>{html.escape(title)} · svg-mcp</title><style>{_STYLE}</style></head>"
        '<body><main class="card">'
        f'<div class="brand">{LOGO}<div class="wordmark">svg<span>-mcp</span></div></div>'
        f"{body}</main></body></html>"
    )
    # Not cached, not framed, and the form only posts back here.
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Cache-Control", "no-store"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Frame-Options", "DENY"),
        (
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src data:; form-action 'self'; frame-ancestors 'none'",
        ),
    ]
    return Reply(status, headers, document.encode("utf-8"))


def login_page(next_path: str, error: str | None = None, status: int = 200) -> Reply:
    """web_auth's login (svg_mcp.webgate): one password field."""
    problem = (
        f'<div class="status warn"><span class="dot"></span>{html.escape(error)}</div>'
        if error
        else ""
    )
    body = (
        f"{problem}<h1>Sign in</h1>"
        "<p>Enter svg-mcp's web password to watch live previews from this browser. "
        "You'll stay signed in here for a while.</p>"
        '<form method="post" action="/auth/login">'
        f'<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">'
        '<input class="field" type="password" name="password" autocomplete="current-password" '
        'placeholder="Password" required autofocus aria-label="Password">'
        '<button class="button" type="submit">Sign in</button>'
        "</form>"
    )
    return _page("Sign in", body, status=status)
