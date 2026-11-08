"""Browser login for the live preview (svg_mcp.preview); svg-mcp's face of the
shared web_auth scheme (toucan + mcp-combiner use the same password and formats).

Browsers can't send the MCP bearer, so with ``web_auth.mode = "password"`` the
live-preview pages (``/<token>/...``, see svg_mcp.preview) ask for a password
once per ``session_days`` per browser, then ride a session cookie. Three ways
through, checked in order:

- a request from this machine itself (from one of its own addresses —
  loopback, the interface addresses, the tailnet address — AND with a
  ``Host`` header naming it) — exempt;
- a valid session cookie (``svgmcp_session``);
- a valid ticket (``?t=``) on a preview URL svg-mcp handed an MCP client
  (``start_preview``): exchanged for the cookie, then a redirect to the clean
  URL — so links opened from a client never prompt.

Otherwise: a redirect to ``/auth/login`` (pages) or 401 (everything else).

KEEP IN DIFF with mcp-combiner: this module deliberately mirrors
``mcp-combiner``'s ``combiner/mcp_combiner/webauth.py`` (dev @ eb26663, "feat: /status page;
Host check for 'this machine'"): same names, signatures, ordering,
docstrings and section order — passwords (``hash_password`` /
``verify_password`` / ``adopt_password_env`` / ``env_password_hash``),
Signer (+ ``_read_key``), ``TicketLedger``, this machine
(``refresh_local_addresses`` / ``set_local_names`` / ``start_local_refresh`` /
``is_local`` / ``is_local_host`` / ``host_name`` / ``_canonical`` /
``_interface_addresses`` / ``parse_addresses``), and
``LoginLimiter`` — so a fix in one ports to the other by diff. The
NECESSARY differences are confined and commented per symbol:

- the cookie name (``svgmcp_session``), env var names (``SVG_MCP_*``), and the
  signing-key default path (combiner resolves it in its webgate); and
- the stdlib threaded-server specifics, which live in ``svg_mcp.webgate`` (the
  thread-based scrypt slots corresponding to combiner's ``asyncio.to_thread(2)``,
  and the ``X-Forwarded-For`` parsing — svg-mcp has no uvicorn ProxyHeaders).
- svg-mcp sessions carry the ticket's ``sub`` on ticket-exchanged cookies (one
  connected client can never access another's minted sessions) — a deliberate
  supersets-comma-noted in `Signer.session`.

Token format (shared with the toucan host and mcp-combiner):
``<payload_b64url>.<sig_b64url>``, payload compact JSON, sig HMAC-SHA256 over
the payload part. Session: ``{"v":1,"k":"s","iat":..,"exp":..}``; ticket:
``{"v":1,"k":"t","sub":<preview token>,"j":<random id>,"exp":..}`` —
redeemable ``web_auth.ticket_uses`` times (counted in memory by ``j``).
Passwords: ``scrypt$<n>$<r>$<p>$<salt>$<hash>`` (base64url, unpadded).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

logger = logging.getLogger("svg-mcp")

COOKIE = "svgmcp_session"  # combiner: "mcpc_session"
TICKET_PARAM = "t"
TICKET_TTL_S = 300
LOGIN_PATH = "/auth/login"
LOGOUT_PATH = "/auth/logout"

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

#: The raw password env (``op run`` style). Read once at serve startup and
#: removed from the environment (:func:`adopt_password_env`), so children —
#: external servers, stdio upstreams — never inherit it.
PASSWORD_ENV = "SVG_MCP_WEB_PASSWORD"  # combiner: "MCP_COMBINER_WEB_PASSWORD"


# ── passwords ──────────────────────────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """The stored form of *password* (``web_auth.password_hash``)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def validate_hash_artifact(stored: str) -> None:
    """svg-mcp-specific hardening (combiner validates in its config load): reject a
    malformed ``scrypt$`` artifact AT LOAD — numeric round parameters and a real
    32-byte digest — so a truncated paste fails the server start, not every login."""
    if stored.startswith("scrypt$") and stored.count("$") == 5:
        return
    raise ValueError(
        "malformed web_auth password hash artifact: expected `svg-mcp hash-password` "
        "output 'scrypt$<n>$<r>$<p>$<salt>$<hash>'"
    )


_adopted_hash: str | None = None


def adopt_password_env() -> None:
    """Take ``$SVG_MCP_WEB_PASSWORD`` out of the environment, keeping its hash
    (for config loads and reloads); children never inherit the raw secret."""
    global _adopted_hash
    raw = os.environ.pop(PASSWORD_ENV, None)
    if raw:
        _adopted_hash = hash_password(raw)


def env_password_hash() -> str | None:
    """A hash of the raw password from the environment, if one was given:
    still in the environment (tests), or adopted at serve startup."""
    raw = os.environ.get(PASSWORD_ENV)
    return hash_password(raw) if raw else _adopted_hash


def reset_adopted_password() -> None:
    global _adopted_hash
    _adopted_hash = None


# ── signed tokens (session cookie, tickets) ────────────────────────


class KeyFileError(Exception):
    """The signing key file exists but can't be trusted (owner, mode, symlink)."""


class Signer:
    """HMAC-SHA256 tokens under one persisted key (rotate it = log everyone out)."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    @classmethod
    def load_or_create(cls, path: Path) -> Signer:
        """The key at *path*, created (0600, atomically) when missing.

        An existing key must be a regular file owned by this user and not
        readable by anyone else — anyone who can read or plant it can mint
        sessions. Creation writes a temp file and hard-links it into place,
        so a reader never sees a half-written key and two first starts agree
        on one key.
        """
        try:
            return cls(_read_key(path))
        except FileNotFoundError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".web-auth-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(secrets.token_bytes(32))
            try:
                os.link(tmp, path)
                logger.info("web_auth: created a signing key at %s", path)
            except FileExistsError:
                pass  # another process won the race: use its key
        finally:
            os.unlink(tmp)
        return cls(_read_key(path))

    def sign(self, claims: dict[str, Any]) -> str:
        payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
        sig = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        return f"{payload}.{_b64(sig)}"

    def verify(self, token: str, kind: str) -> dict[str, Any] | None:
        """The claims of a valid, unexpired *token* of *kind*, else ``None``."""
        payload, _, sig = token.partition(".")
        if not payload or not sig:
            return None
        expected = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(_unb64(sig), expected):
                return None
            claims = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(claims, dict) or claims.get("v") != 1 or claims.get("k") != kind:
            return None
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or exp < time.time():
            return None
        return claims

    def session(self, days: float, subject: str | None = None) -> str:
        """A login-page cookie (``subject=None``: opens every preview) or a
        TICKET-EXCHANGED cookie (``subject`` set: the exchanged cookie stays bound
        to the ticket's preview — svg-mcp-specific, George's session-ownership
        rule; the combiner's cookie is sub-less)."""
        now = int(time.time())
        claims: dict[str, Any] = {"v": 1, "k": "s", "iat": now, "exp": now + int(days * 86400)}
        if subject is not None:
            claims["sub"] = subject
        return self.sign(claims)

    def ticket(self, subject: str, ttl_s: int = TICKET_TTL_S) -> str:
        return self.sign(
            {
                "v": 1,
                "k": "t",
                "sub": subject,
                "j": secrets.token_urlsafe(12),
                "exp": int(time.time()) + ttl_s,
            }
        )


def _read_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:  # ELOOP: a symlink
        raise KeyFileError(f"web_auth key {path} can't be opened safely: {exc}") from exc
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        getuid = getattr(os, "getuid", None)
        if getuid is not None and st.st_uid != getuid():
            raise KeyFileError(f"web_auth key {path} is owned by another user; delete it")
        if st.st_mode & 0o077:
            raise KeyFileError(
                f"web_auth key {path} is readable by others; `chmod 600 {path}` "
                "(or delete it, which signs every browser out)"
            )
        key = fh.read()
    if len(key) < 32:
        raise KeyFileError(f"web_auth key {path} is truncated; delete it to make a new one")
    return key


class TicketLedger:
    """How many times each ticket (by its ``j``) was redeemed, until it expires.

    svg-mcp-specific guard: a ticket past its OWN ``exp`` never redeems again —
    the mirror's ledger prunes expired entries (which would re-admit a replayed
    URL), and the TTL window must stay closed to a URL that leaks into a
    transcript."""

    def __init__(self) -> None:
        self._uses: OrderedDict[str, tuple[int, float]] = OrderedDict()

    def redeem(self, claims: dict[str, Any], allowed: int) -> bool:
        jti = claims.get("j")
        exp = claims.get("exp")
        if not isinstance(jti, str) or not jti or not isinstance(exp, (int, float)):
            return False
        now = time.time()
        if exp < now:  # svg-mcp-specific: the window stays closed for expired tickets
            return False
        for key in [k for k, (_, e) in self._uses.items() if e < now]:
            del self._uses[key]
        used, _ = self._uses.get(jti, (0, exp))
        if used >= allowed:
            return False
        self._uses[jti] = (used + 1, float(exp))
        return True

    def clear(self) -> None:
        self._uses.clear()


TICKETS = TicketLedger()


def default_key_path() -> Path:
    """svg-mcp-specific (combiner resolves its path in webgate): the signing key
    lives in ``$XDG_STATE_HOME/svg-mcp/web-auth.key``. Delete it to sign every
    browser out."""
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "svg-mcp" / "web-auth.key"


# ── "this machine" ─────────────────────────────────────────────────

#: This machine's non-loopback addresses (interfaces, tailnet). Read without a
#: lock on the request path; replaced whole by :func:`refresh_local_addresses`
#: off it. Empty until the first refresh: only loopback counts as local then.
_local: frozenset[str] = frozenset()
#: Names that mean this machine in a ``Host`` header besides ``localhost`` and
#: its addresses: the tailnet MagicDNS name (refreshed) and configured ones
#: (svg-mcp: the preview's bind host when it is a name — :func:`set_local_names`).
_local_names: frozenset[str] = frozenset()
_configured_names: frozenset[str] = frozenset()
_refresh_stop = threading.Event()
_REFRESH_INTERVAL_S = 60.0


def local_addresses() -> frozenset[str]:
    return _local


def refresh_local_addresses() -> frozenset[str]:
    """Look the addresses up now (blocking: runs ``ifconfig``/``ip`` and
    ``tailscale``) and publish them."""
    global _local, _local_names
    found = set(_interface_addresses())
    # combiner: ``from mcp_combiner.tailnet import fresh_tailnet_info``.
    name, ip = _tailnet_info()
    if ip:
        found.add(ip)
    _local = frozenset(_canonical(a) for a in found)
    _local_names = _configured_names | ({name.lower().rstrip(".")} if name else set())
    return _local


def set_local_names(names: set[str]) -> None:
    """Host names that are this machine (svg-mcp: the preview's bind host when
    it is a name; combiner: ``oauth.public_url``'s host, the widget origin's
    host). Kept across refreshes."""
    global _configured_names, _local_names
    _configured_names = frozenset(n.lower().rstrip(".") for n in names if n)
    _local_names = _local_names | _configured_names


def start_local_refresh(interval_s: float = _REFRESH_INTERVAL_S) -> None:
    """Refresh the addresses now and every *interval_s*, in a daemon thread —
    a laptop's addresses change as it roams."""
    _refresh_stop.clear()

    def loop() -> None:
        while True:
            try:
                refresh_local_addresses()
            except Exception:  # noqa: BLE001 — keep the last good set
                logger.warning("web_auth: address refresh failed; keeping the last good set")
            if _refresh_stop.wait(interval_s):
                return

    threading.Thread(target=loop, name="web-auth-addresses", daemon=True).start()


def stop_local_refresh() -> None:
    _refresh_stop.set()


def reset_local_addresses() -> None:
    """svg-mcp-specific (test isolation): clear the refreshed set AND stop the
    daemon, so a test's mocks re-run deterministically on its own refresh."""
    stop_local_refresh()
    global _local, _local_names, _configured_names
    _local = frozenset()
    _local_names = frozenset()
    _configured_names = frozenset()


#: Tailscale's reserved address block (Shared Address Space / CGNAT range).
_TAILSCALE_V4_NET = ipaddress.ip_network("100.64.0.0/10")


def is_local(client_host: str | None) -> bool:
    if not client_host:
        return False
    canonical = _canonical(client_host)
    try:
        if ipaddress.ip_address(canonical).is_loopback:
            return True
    except ValueError:
        return False
    return canonical in _local


def is_local_host(host_header: str | None) -> bool:
    """Whether a ``Host`` header names this machine.

    The other half of "this machine": a page that rebinds its own domain to
    127.0.0.1 connects from loopback, but its requests still say
    ``Host: attacker.example`` — they must not inherit the exemption.
    """
    hostname = host_name(host_header)
    if hostname is None:
        return False
    if hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(_canonical(hostname))
    except ValueError:
        return hostname in _local_names
    return ip.is_loopback or str(ip) in _local


def host_name(host_header: str | None) -> str | None:
    """The host part of a ``Host`` header, lower-cased (``[::1]:9741`` → ``::1``)."""
    if not host_header:
        return None
    host = host_header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else None
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".") or None


def _canonical(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:a.b.c.d is a.b.c.d
    return str(mapped or ip)


def _interface_addresses() -> set[str]:
    """``ifconfig -a``, else (Linux without net-tools) ``ip -o addr``;
    both tools' results accumulate when both exist."""
    for argv in (["ifconfig", "-a"], ["ip", "-o", "addr", "show"]):
        exe = shutil.which(argv[0])
        if exe is None:
            continue
        try:
            out = subprocess.run(
                [exe, *argv[1:]], capture_output=True, text=True, timeout=2.0, check=False
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        return parse_addresses(out)
    return set()


def parse_addresses(text: str) -> set[str]:
    """Addresses from ``ifconfig`` (``inet 1.2.3.4 …``, ``inet addr:1.2.3.4``)
    or ``ip -o addr`` (``2: enp3s0 inet 1.2.3.4/24 …``) output."""
    found: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        for i, word in enumerate(parts[:-1]):
            if word in ("inet", "inet6"):
                candidate = parts[i + 1].removeprefix("addr:").split("/", 1)[0]
                if candidate and not candidate.endswith("%"):
                    found.add(candidate)
    return found


def _tailnet_info() -> tuple[str | None, str | None]:
    """svg-mcp-specific stand-in for mcp-combiner's tailnet module
    (``fresh_tailnet_info`` lives in ``mcp_combiner.tailnet``): the machine's
    (MagicDNS name, tailnet address), fresh — ``tailscale status --json
    --peers=false`` → ``Self.DNSName`` / ``Self.TailscaleIPs``, the address
    falling back to ``tailscale ip -4`` — never any per-process cache."""
    if shutil.which("tailscale") is None:
        return None, None
    name: str | None = None
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json", "--peers=false"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        ).stdout
        parsed = json.loads(out or "{}")
        own = parsed.get("Self") or {}
        ips: list[str] = list(own.get("TailscaleIPs") or [])
        name = str(own.get("DNSName") or "") or None
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        ips = []
    for ip in ips:
        try:
            if ipaddress.ip_address(ip) not in _TAILSCALE_V4_NET and ":" not in ip:
                continue
            return name, ip
        except ValueError:
            continue
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=2.0, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return name, None
    for line in (out or "").splitlines():
        try:
            if ipaddress.ip_address(line.strip()) in _TAILSCALE_V4_NET:
                return name, line.strip()
        except ValueError:
            continue
    return name, None


# ── the browser's address behind a local proxy ─────────────────────


def client_address(peer: str | None, forwarded_for: list[str]) -> str | None:
    """svg-mcp-specific (combiner's XFF rule is uvicorn's ProxyHeadersMiddleware):
    the no-uvicorn equivalent — the browser's address is *peer* itself, unless
    *peer* is a trusted loopback proxy sending ``X-Forwarded-For`` — then the
    rightmost untrusted hop in it. Headers from any other peer are ignored.

    A loopback peer with NO ``X-Forwarded-For`` IS loopback — always local, per
    the combiner's trust model (a direct local browser, or a tunnel hop, both
    count as this machine)."""
    if peer is None or _canonical(peer) not in TRUSTED_PROXIES:
        return peer
    hops = [_host_of(h) for value in forwarded_for for h in value.split(",") if h.strip()]
    if not hops:
        return peer
    for hop in reversed(hops):
        if _canonical(hop) not in TRUSTED_PROXIES:
            return hop
    return hops[0]


def _host_of(hop: str) -> str:
    """A forwarded hop without its port (``1.2.3.4:5``, ``[::1]:5``)."""
    hop = hop.strip()
    if hop.startswith("["):
        return hop[1:].partition("]")[0]
    if hop.count(":") == 1:
        return hop.partition(":")[0]
    return hop


#: svg-mcp-specific (combiner's XFF trust set is the same, but the middleware
#: parsing differs — svg-mcp has no uvicorn): peers whose ``X-Forwarded-*``
#: headers are believed: a local reverse proxy (e.g. ``tailscale serve``).
TRUSTED_PROXIES = frozenset({"127.0.0.1", "::1"})


# ── login rate limit ───────────────────────────────────────────────


class LoginLimiter:
    """Failed-login budgets: per client address and across all of them.

    :meth:`begin` reserves an attempt synchronously — before the caller awaits
    anything — so concurrent attempts count against the budget immediately
    rather than after their (slow) password checks finish. The global budget
    stops guessing spread over many addresses, at the price of locking the
    password form for everyone during such an attack (this machine and ticket
    links still get in). Bounded: at most ``max_clients`` addresses tracked,
    empty windows dropped.
    """

    def __init__(
        self,
        per_client: int = 5,
        overall: int = 30,
        window_s: float = 60.0,
        max_clients: int = 10_000,
    ) -> None:
        self.per_client = per_client
        self.overall = overall
        self.window_s = window_s
        self.max_clients = max_clients
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._all: deque[float] = deque()
        self._in_flight: dict[str, int] = {}

    def begin(self, client: str) -> bool:
        """Reserve an attempt for *client*; ``False`` when over budget."""
        now = time.monotonic()
        self._prune(now)
        window = self._failures.get(client)
        if window is not None:
            cutoff = now - self.window_s
            while window and window[0] < cutoff:
                window.popleft()
        mine = len(window or ()) + self._in_flight.get(client, 0)
        everyone = len(self._all) + sum(self._in_flight.values())
        if mine >= self.per_client or everyone >= self.overall:
            return False
        self._in_flight[client] = self._in_flight.get(client, 0) + 1
        return True

    def end(self, client: str, *, failed: bool) -> None:
        left = self._in_flight.get(client, 0) - 1
        if left > 0:
            self._in_flight[client] = left
        else:
            self._in_flight.pop(client, None)
        if not failed:
            return
        now = time.monotonic()
        window = self._failures.pop(client, None) or deque()
        window.append(now)
        self._failures[client] = window  # most recent last
        self._all.append(now)
        while len(self._failures) > self.max_clients:
            self._failures.popitem(last=False)

    def tracked(self) -> int:
        return len(self._failures) + len(self._in_flight)

    def _prune(self, now: float) -> None:
        """Drop expired failures. Clients are ordered by their latest failure,
        so whole stale clients come off the front."""
        cutoff = now - self.window_s
        while self._all and self._all[0] < cutoff:
            self._all.popleft()
        while self._failures:
            client, window = next(iter(self._failures.items()))
            if window and window[-1] >= cutoff:
                break
            del self._failures[client]

    def reset(self) -> None:
        self._failures.clear()
        self._all.clear()
        self._in_flight.clear()


LIMITER = LoginLimiter()


#: svg-mcp-specific (threaded stdlib server — combiner runs scrypt via
#: asyncio.to_thread with the same cap): at most 2 concurrent scrypt
#: verifications; each is ~16 MB and ~30 ms of work, and a flood of login
#: threads must queue rather than multiply into a cheap DoS.
_LOGIN_SCRYPT_SLOTS = threading.BoundedSemaphore(2)


def too_many_failures(client_host: str) -> bool:
    """svg-mcp-specific convenience kept for the live-preview gate tests: the
    gate's own begin() (see `webgate.login`) consults :data:`LIMITER`."""
    return not LIMITER.begin(client_host)


def record_failure(client_host: str) -> None:
    """svg-mcp-specific convenience: record a failed login (the gate's
    `end(failed=True)` — mirrored as `LIMITER.end(client, failed=True)`)."""
    LIMITER.end(client_host, failed=True)


def reset_failures() -> None:
    """svg-mcp-specific (test isolation): clear every limiter budget."""
    LIMITER.reset()
