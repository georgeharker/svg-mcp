"""Browser login for the live preview (passwords, signed tokens, "this machine").

Browsers can't send the MCP bearer, so with ``web_auth.mode = "password"`` the
live-preview pages (``/<token>/...``, see svg_mcp.preview) ask for a password
once per ``session_days`` per browser, then ride a session cookie. Three ways
through, checked in order (svg_mcp.webgate):

- a request from this machine itself (any of its own addresses: loopback, the
  interface addresses, the tailnet address) — exempt;
- a valid session cookie (``svgmcp_session``);
- a valid ticket (``?t=``) on a preview URL svg-mcp handed an MCP client
  (``start_preview``): exchanged for the cookie, then a redirect to the clean
  URL — so links opened from a client never prompt.

Otherwise: a redirect to ``/auth/login`` (pages) or 401 (everything else).

Formats are shared with mcp-combiner's web_auth (``mcp_combiner.webauth``), so
``mcp-combiner hash-password`` output works here as-is. Token:
``<payload_b64url>.<sig_b64url>``, payload compact JSON, sig HMAC-SHA256 over
the payload part. Session: ``{"v":1,"k":"s","iat":..,"exp":..}``; ticket:
``{"v":1,"k":"t","sub":<preview session token>,"exp":..}``. Passwords:
``scrypt$<n>$<r>$<p>$<salt>$<hash>`` (base64url, unpadded).
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
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COOKIE = "svgmcp_session"
TICKET_PARAM = "t"
TICKET_TTL_S = 300
LOGIN_PATH = "/auth/login"
LOGOUT_PATH = "/auth/logout"

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_FAILURES_PER_MINUTE = 5

#: Peers whose ``X-Forwarded-*`` headers are believed: a local reverse proxy
#: (e.g. ``tailscale serve``). Same set as uvicorn's ``forwarded_allow_ips``
#: in mcp-combiner.
TRUSTED_PROXIES = frozenset({"127.0.0.1", "::1"})


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


# ── signed tokens (session cookie, tickets) ────────────────────────


def default_key_path() -> Path:
    """Where the signing key lives: ``$XDG_STATE_HOME/svg-mcp/web-auth.key``
    (``~/.local/state`` when unset). Delete it to sign every browser out."""
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "svg-mcp" / "web-auth.key"


class Signer:
    """HMAC-SHA256 tokens under one persisted key (rotate it = log everyone out)."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    @classmethod
    def load_or_create(cls, path: Path) -> Signer:
        try:
            key = path.read_bytes()
            if len(key) >= 32:
                return cls(key)
        except OSError:
            pass
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        logger.info("web_auth: created a signing key at %s", path)
        return cls(key)

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

    def session(self, days: float) -> str:
        now = int(time.time())
        return self.sign({"v": 1, "k": "s", "iat": now, "exp": now + int(days * 86400)})

    def ticket(self, subject: str, ttl_s: int = TICKET_TTL_S) -> str:
        return self.sign({"v": 1, "k": "t", "sub": subject, "exp": int(time.time()) + ttl_s})


# ── "this machine" ─────────────────────────────────────────────────

_local_lock = threading.Lock()
_local: frozenset[str] | None = None

#: Tailscale's reserved address block (Shared Address Space / CGNAT range).
_TAILSCALE_V4_NET = ipaddress.ip_network("100.64.0.0/10")


def local_addresses() -> frozenset[str]:
    """Every address of this machine (loopback, interfaces, tailnet), cached."""
    global _local
    with _local_lock:
        if _local is None:
            found = {"127.0.0.1", "::1"}
            found.update(_interface_addresses())
            ip = _tailnet_ip()
            if ip:
                found.add(ip)
            _local = frozenset(_canonical(a) for a in found)
        return _local


def reset_local_addresses() -> None:
    global _local
    with _local_lock:
        _local = None


def is_local(client_host: str | None) -> bool:
    if not client_host:
        return False
    canonical = _canonical(client_host)
    try:
        if ipaddress.ip_address(canonical).is_loopback:
            return True
    except ValueError:
        return False
    return canonical in local_addresses()


def _canonical(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:a.b.c.d is a.b.c.d
    return str(mapped or ip)


def _interface_addresses() -> set[str]:
    ifc = shutil.which("ifconfig")
    if ifc is None:
        return set()
    try:
        out = subprocess.run(
            [ifc, "-a"], capture_output=True, text=True, timeout=2.0, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    found: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("inet", "inet6"):
            found.add(parts[1].removeprefix("addr:"))
    return found


def _tailnet_ip() -> str | None:
    """This machine's tailnet IPv4 (``tailscale ip -4``), or ``None``."""
    if shutil.which("tailscale") is None:
        return None
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=2.0, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (out or "").splitlines():
        try:
            if ipaddress.ip_address(line.strip()) in _TAILSCALE_V4_NET:
                return line.strip()
        except ValueError:
            continue
    return None


# ── the browser's address behind a local proxy ─────────────────────


def client_address(peer: str | None, forwarded_for: list[str]) -> str | None:
    """The browser's address: *peer* itself, unless *peer* is a trusted local
    proxy sending ``X-Forwarded-For`` — then the rightmost untrusted hop in
    it (uvicorn's ``ProxyHeadersMiddleware`` rule, which mcp-combiner relies
    on). Headers from any other peer are ignored."""
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


# ── login rate limit ───────────────────────────────────────────────

_failures: defaultdict[str, deque[float]] = defaultdict(deque)
_failures_lock = threading.Lock()


def too_many_failures(client_host: str) -> bool:
    with _failures_lock:
        window = _failures[client_host]
        now = time.monotonic()
        while window and now - window[0] > 60:
            window.popleft()
        return len(window) >= _FAILURES_PER_MINUTE


def record_failure(client_host: str) -> None:
    with _failures_lock:
        _failures[client_host].append(time.monotonic())


def reset_failures() -> None:
    with _failures_lock:
        _failures.clear()
