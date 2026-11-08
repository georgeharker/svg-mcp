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
from .webauth import LIMITER

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


#: Cap on concurrent scrypt verifications: each is ~16 MB and ~30 ms of work, so a
#: flood of parallel login threads must queue (2 at a time) rather than multiply
#: into a cheap DoS on a threaded stdlib server.
_LOGIN_SCRYPT_SLOTS = threading.BoundedSemaphore(2)


_lock = threading.Lock()
_state: _State | None = None


def configure(config: WebAuthConfig, signer: webauth.Signer | None = None) -> None:
    """Set the gate's config; a signer is loaded (or created) at the default key
    path when ``mode`` is ``password`` and none is given.

    Logs the resolved state once — on a fail-open control the operator must be
    able to tell from the logs whether the login is active (this is the exact
    gap the pre-0.10.1 review called out)."""
    global _state
    if config.mode == "password" and signer is None:
        signer = webauth.Signer.load_or_create(webauth.default_key_path())
    with _lock:
        _state = _State(config, signer if config.mode == "password" else None)
    if _state.config.mode == "password" and _state.signer is not None:
        logger.info(
            "web_auth: live-preview login gate is ON (%.0f-day cookies, exempt-local %s)",
            _state.config.session_days,
            _state.config.exempt_local,
        )
    else:
        logger.info("live-preview pages are open — the login gate is not configured")


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
    """A request carrying a valid, UNREDEEMED ticket for its preview: set the session
    (BOUND to that preview — the exchanged cookie opens no other chat's preview) and
    redirect to the same URL without the ticket."""
    signer = _current().signer
    state = _current()
    items = request.query_items()
    ticket = next((v for k, v in items if k == webauth.TICKET_PARAM), "")
    subject = _ticket_subject(request)
    if signer is None or not ticket or subject is None:
        return None
    claims = signer.verify(ticket, "t")
    if claims is None or claims.get("sub") != subject:
        return None
    if not webauth.TICKETS.redeem(claims, allowed=state.config.ticket_uses):
        return None  # consumed: no replay within the TTL window
    query = [(k, v) for k, v in items if k != webauth.TICKET_PARAM]
    clean = request.path + (f"?{urlencode(query)}" if query else "")
    return _redirect(clean, _session_cookie(request, subject=subject))


def check(request: GateRequest) -> Reply | None:
    """``None`` to serve *request*, else the reply that stands in for it."""
    if auth_path(request.path) or admitted(request):
        return None
    # Ticket-bound scopes: a cookie minted by a TICKET (its claims carry ``sub``) opens
    # only the preview that named it; a cookie minted by the LOGIN PAGE (sub-less) opens
    # every preview on this server. An out-of-scope signed-in request falls through to
    # the ticket/portal path (its ticket, if any, was already vetted for THIS subject).
    state = _current()
    if state.signer is not None:
        claims = state.signer.verify(request.cookie(webauth.COOKIE), "s")
        if claims is not None and claims.get("sub") is not None:
            subject = _ticket_subject(request)
            if subject is None or claims["sub"] == subject:
                return None
            return _redirect(f"{webauth.LOGIN_PATH}?next={quote(request.path, safe='')}")
    redirect = ticket_redirect(request)
    if redirect is not None:
        return redirect
    if request.method == "GET" and "text/html" in request.header("accept"):
        target = request.path + (f"?{request.query}" if request.query else "")
        return _redirect(f"{webauth.LOGIN_PATH}?next={quote(target, safe='')}")
    return _plain(401, "login required")


# ── login and logout ───────────────────────────────────────────────


def _safe_next(raw: str | None) -> str:
    """Only same-origin paths (no scheme, no //host), and only printable ASCII —
    a CRLF or non-Latin-1 byte in ``next`` would otherwise reach an HTTP header
    write (header injection) or crash the response encode."""
    if (
        not raw
        or not raw.startswith("/")
        or raw.startswith("//")
        or "\\" in raw
        or any(not (32 <= ord(c) <= 126) for c in raw)
    ):
        return "/"
    return raw


def reserve_login(request: GateRequest) -> Reply | None:
    """Reserve the login concurrency slot BEFORE the body is read (mirror-faithful
    ordering): per-address in-flight + recorded failures against the cap, plus the
    overall budget. ``None`` = reserved (release with `release_login`); a Reply =
    the 429 refusal."""
    state = _current()
    if state.config.mode != "password" or state.signer is None:
        return None
    if not LIMITER.begin(request.client or "?"):
        return authpages.login_page(
            "/", error="Too many attempts — wait a minute and try again.", status=429
        )
    return None


def release_login(request: GateRequest) -> None:
    """Release the reservation made by `reserve_login` (413/close path: login
    never verified, so it ends without a failure)."""
    LIMITER.end(request.client or "?", failed=False)


def login(
    request: GateRequest,
    form: dict[str, str] | None = None,
    *,
    admitted_already: bool = False,
) -> Reply:
    """``GET /auth/login`` shows the form; ``POST`` (with *form*) checks it.

    `admitted_already`: the caller (preview's `_auth_route`) reserved the
    concurrency slot BEFORE the body was read — the admission is not repeated
    here, and its release stays with the caller."""
    state = _current()
    if state.config.mode != "password" or state.signer is None:
        return _plain(404, "web login is not enabled")
    query_next = next((v for k, v in request.query_items() if k == "next"), None)
    target = _safe_next(query_next)
    if request.method != "POST" or form is None:
        return authpages.login_page(target)
    client = request.client or "?"
    # Concurrency + budget reservation before the body is hashed when the caller has
    # not already reserved (in-flight + failures share the per-address cap, the
    # overall budget applies) — direct/testing callers reserve here.
    if not admitted_already and not LIMITER.begin(client):
        return authpages.login_page(
            target, error="Too many attempts — wait a minute and try again.", status=429
        )
    # svg-mcp-specific: scrypt runs capped at 2 concurrent (mirror of
    # asyncio.to_thread(2)); each is ~16 MB / ~30 ms — bound the parallel work.
    with _LOGIN_SCRYPT_SLOTS:
        password = form.get("password")
        failed = not password or not webauth.verify_password(
            password, state.config.password_hash or ""
        )
    LIMITER.end(client, failed=failed)
    if failed:
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


def _session_cookie(request: GateRequest, subject: str | None = None) -> str:
    state = _current()
    if state.signer is None:
        return ""
    days = state.config.session_days
    cookie = (
        f"{webauth.COOKIE}={state.signer.session(days, subject=subject)}; "
        f"Max-Age={round(days * 86400)}; "
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
