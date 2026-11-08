"""The web_auth gate and login routes for the live preview (policy and tokens: svg_mcp.webauth).

The preview is a stdlib ``http.server`` (svg_mcp.preview), not a Starlette app,
so this is mcp-combiner's ``WebAuthGate`` without the framework: the handler
describes each request as a :class:`GateRequest` and writes back whatever
:class:`~svg_mcp.authpages.Reply` comes out. A request passes when web_auth is
off, comes from this machine, carries a valid session cookie, or carries a valid
ticket for its preview — exchanged for the cookie with a redirect to the clean
URL. Otherwise pages go to the login page and other requests get 401.

The ticket's subject is the preview's session token, the first path segment
(``/<token>/...``), so a ticket opens one chat's preview and no other.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qsl, quote, urlencode

from . import authpages, webauth
from .authpages import Reply
from .config import WebAuthConfig, get_settings

logger = logging.getLogger(__name__)

#: Cap on a login form body; the form is one password and a path.
MAX_FORM_BYTES = 16 * 1024


@dataclass
class GateRequest:
    """What the gate needs from one HTTP request."""

    method: str
    path: str
    query: str = ""
    #: header names lower-cased; repeated headers keep every value.
    headers: dict[str, list[str]] = field(default_factory=dict)
    #: the socket peer (a local reverse proxy, when one fronts the preview).
    peer: str | None = None

    def header(self, name: str) -> str:
        values = self.headers.get(name.lower())
        return values[0] if values else ""

    @property
    def client(self) -> str | None:
        """The browser's address (X-Forwarded-For only from a local proxy)."""
        return webauth.client_address(self.peer, self.headers.get("x-forwarded-for", []))

    def cookie(self, name: str) -> str:
        jar: SimpleCookie = SimpleCookie()
        for raw in self.headers.get("cookie", []):
            try:
                jar.load(raw)
            except CookieError:
                continue
        morsel = jar.get(name)
        return morsel.value if morsel is not None else ""

    def query_items(self) -> list[tuple[str, str]]:
        return parse_qsl(self.query, keep_blank_values=True)


# ── state: the config and signer, resolved once ────────────────────


@dataclass
class _State:
    config: WebAuthConfig
    signer: webauth.Signer | None


_lock = threading.Lock()
_state: _State | None = None


def configure(config: WebAuthConfig, signer: webauth.Signer | None = None) -> None:
    """Set the gate's config; a signer is loaded (or created) at the default key
    path when ``mode`` is ``password`` and none is given."""
    global _state
    if config.mode == "password" and signer is None:
        signer = webauth.Signer.load_or_create(webauth.default_key_path())
    with _lock:
        _state = _State(config, signer if config.mode == "password" else None)


def reset() -> None:
    """Forget the config: the next request re-reads settings."""
    global _state
    with _lock:
        _state = None


def _current() -> _State:
    with _lock:
        state = _state
    if state is None:
        configure(get_settings().web_auth)
        with _lock:
            state = _state
    return state if state is not None else _State(WebAuthConfig(), None)


def enabled() -> bool:
    state = _current()
    return state.config.mode == "password" and state.signer is not None


# ── the gate ───────────────────────────────────────────────────────


def auth_path(path: str) -> bool:
    """The login routes, never gated."""
    return path in (webauth.LOGIN_PATH, webauth.LOGOUT_PATH)


def admitted(request: GateRequest) -> bool:
    """Off, from this machine, or signed in (a valid session cookie)."""
    state = _current()
    if state.config.mode != "password" or state.signer is None:
        return True
    if state.config.exempt_local and webauth.is_local(request.client):
        return True
    return state.signer.verify(request.cookie(webauth.COOKIE), "s") is not None


def _ticket_subject(request: GateRequest) -> str | None:
    """The preview session token a ticket on *request* must name."""
    parts = [p for p in request.path.split("/") if p]
    return parts[0] if parts else None


def ticket_redirect(request: GateRequest) -> Reply | None:
    """A request carrying a valid ticket for its preview: set the session and
    redirect to the same URL without the ticket."""
    signer = _current().signer
    items = request.query_items()
    ticket = next((v for k, v in items if k == webauth.TICKET_PARAM), "")
    subject = _ticket_subject(request)
    if signer is None or not ticket or subject is None:
        return None
    claims = signer.verify(ticket, "t")
    if claims is None or claims.get("sub") != subject:
        return None
    query = [(k, v) for k, v in items if k != webauth.TICKET_PARAM]
    clean = request.path + (f"?{urlencode(query)}" if query else "")
    return _redirect(clean, _session_cookie(request))


def check(request: GateRequest) -> Reply | None:
    """``None`` to serve *request*, else the reply that stands in for it."""
    if auth_path(request.path) or admitted(request):
        return None
    redirect = ticket_redirect(request)
    if redirect is not None:
        return redirect
    if request.method == "GET" and "text/html" in request.header("accept"):
        target = request.path + (f"?{request.query}" if request.query else "")
        return _redirect(f"{webauth.LOGIN_PATH}?next={quote(target, safe='')}")
    return _plain(401, "login required")


# ── login and logout ───────────────────────────────────────────────


def _safe_next(raw: str | None) -> str:
    """Only same-origin paths (no scheme, no //host)."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return "/"
    return raw


def login(request: GateRequest, form: dict[str, str] | None = None) -> Reply:
    """``GET /auth/login`` shows the form; ``POST`` (with *form*) checks it."""
    state = _current()
    if state.config.mode != "password" or state.signer is None:
        return _plain(404, "web login is not enabled")
    query_next = next((v for k, v in request.query_items() if k == "next"), None)
    target = _safe_next(query_next)
    if request.method != "POST" or form is None:
        return authpages.login_page(target)
    client = request.client or "?"
    if webauth.too_many_failures(client):
        return authpages.login_page(
            target, error="Too many attempts — wait a minute and try again.", status=429
        )
    password = form.get("password")
    if not password or not webauth.verify_password(password, state.config.password_hash or ""):
        webauth.record_failure(client)
        logger.warning("web_auth: failed login from %s", client)
        return authpages.login_page(target, error="That password isn't right.", status=401)
    return _redirect(_safe_next(form.get("next") or target), _session_cookie(request))


def logout(_request: GateRequest) -> Reply:
    cleared = f"{webauth.COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
    return _redirect(webauth.LOGIN_PATH, cleared)


def parse_form(body: bytes) -> dict[str, str]:
    """An ``application/x-www-form-urlencoded`` body (first value per field)."""
    form: dict[str, str] = {}
    for key, value in parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True):
        form.setdefault(key, value)
    return form


# ── tickets on URLs handed to MCP clients ──────────────────────────


def with_ticket(url: str, subject: str) -> str:
    """*url* plus ``t=<ticket>`` for *subject* when web_auth and tickets are on."""
    state = _current()
    if state.config.mode != "password" or state.signer is None or not state.config.tickets:
        return url
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}{webauth.TICKET_PARAM}={state.signer.ticket(subject)}"


# ── replies ────────────────────────────────────────────────────────


def _secure(request: GateRequest) -> bool:
    return request.header("x-forwarded-proto").lower() == "https"


def _session_cookie(request: GateRequest) -> str:
    state = _current()
    if state.signer is None:
        return ""
    days = state.config.session_days
    cookie = (
        f"{webauth.COOKIE}={state.signer.session(days)}; Max-Age={int(days * 86400)}; "
        "Path=/; HttpOnly; SameSite=Lax"
    )
    return cookie + ("; Secure" if _secure(request) else "")


def _redirect(location: str, cookie: str = "") -> Reply:
    headers = [("Location", location), ("Cache-Control", "no-store")]
    if cookie:
        headers.append(("Set-Cookie", cookie))
    return Reply(303, headers)


def _plain(status: int, message: str) -> Reply:
    return Reply(status, [("Content-Type", "text/plain; charset=utf-8")], message.encode())
